[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# workqueue 机制

> **层级**：跨篇机制纵深专题——把 client-go 泛型 workqueue（基础队列 + delaying + rate-limiting 三层）的去重、延迟、限流重试语义讲透。它是**所有控制器/调度器/kubelet 配置源**共用的处理骨架。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上用 `grep -n`/`sed -n` 复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `q.Add+0`。一律用符号+偏移定位。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[11-informer与workqueue机制](../sourceReader/11-informer与workqueue机制.md) §4 是本专题的浓缩版；本专题纵向展开三层队列的完整语义。锚点一致，deepen 时同步。

---

## 0. 结论速览

### 0.1 workqueue 解决什么问题

控制器的事件回调（informer 的 OnAdd/OnUpdate/OnDelete）**不能直接做业务处理**——回调里做慢活会阻塞 informer 的事件分发。workqueue 是回调与业务处理之间的**缓冲层**：回调只做 `Add(key)`（极快），worker goroutine 从队列 `Get(key)` 后才做慢的 `syncHandler`。

### 0.2 三层队列，各解决一件事

| 层 | 类型 | 解决的问题 | 关键方法 |
|---|---|---|---|
| **基础队列** | `Typed[T]`（queue.go） | **去重 + 处理中脏标记** | `Add`/`Get`/`Done` |
| **延迟队列** | `delayingType[T]`（delaying_queue.go） | **延迟入队**（到点才进基础队列） | `AddAfter` |
| **限流队列** | `rateLimitingType[T]`（rate_limiting_queue.go） | **失败指数退避重试** | `AddRateLimited`/`Forget`/`NumRequeues` |

三层是**包装关系**：rate-limiting 包 delaying 包基础队列。控制器用的是最外层 `RateLimitingInterface`。

### 0.3 五条关键结论

1. **去重**：处理前任意次 `Add(key)` 合并为一次（dirty 集合去重）。
2. **处理中脏标记**：`Get` 取走后进 `processing`；处理期间再 `Add` 只标记 dirty 不 Push，`Done` 时若发现 dirty 命中则自动重入队一次——**保证不丢更新**。
3. **shutdown 排空**：`Get` 在队列排空且已 ShutDown 时返回 `shutdown=true`，worker 据此退出。
4. **延迟重试**：失败后 `AddRateLimited(key)` 按指数退避（默认 base 5ms、max 1000s）算延迟，到点经 `AddAfter` 进主队列。
5. **成功清除**：成功处理后 `Forget(key)` 清除失败计数，下次失败从 base 重新退避。

---

## 1. 生产视角：控制器怎么用

### 1.1 标准 controller 骨架

```go
queue := workqueue.NewRateLimitingQueue(workqueue.DefaultControllerRateLimiter())
informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
    AddFunc: func(obj interface{}) { queue.Add(keyFunc(obj)) },   // 回调只 Add
})
for i := 0; i < workers; i++ {
    go worker(queue)
}
func worker(q workqueue.RateLimitingInterface) {
    for {
        item, shutdown := q.Get()
        if shutdown { return }
        err := syncHandler(item)                  // 慢业务
        if err != nil {
            q.AddRateLimited(item)                // 失败 → 指数退避重试
        } else {
            q.Forget(item)                        // 成功 → 清失败计数
        }
        q.Done(item)
    }
}
```

### 1.2 三个对外行为

| 行为 | 生产表现 | 边界/异常 |
|---|---|---|
| 入队 | `Add` 幂等；处理中再 Add 保证处理完重入队一次 | shuttingDown 时 Add 被忽略 |
| 取走 | `Get` 阻塞直到有项；排空+关闭返回 `shutdown=true` | worker 据此退出 |
| 重试 | 失败 `AddRateLimited` 指数退避；成功 `Forget` 清计数 | 退避上限 max（默认 1000s） |

---

## 2. 开发视角：基础队列（`Typed[T]`）

### 2.1 三集合模型

`Typed[T]`（queue.go `Typed+2`，:208）核心是**三个集合**：
- `queue`：待处理 FIFO（有序）。
- `dirty`：已 Add 但未处理完（去重集合）。
- `processing`：正在被 worker 处理（脏标记集合）。

`TypedInterface`（queue.go `TypedInterface+2`，:30）定义 `Add`/`Len`/`Get`/`Done`/`Has`/`ShutDown`。`New`（queue.go `New+0`，:120）初始化三集合 + metrics + cond。

### 2.2 `Add`——去并入队（queue.go `q.Add+0`，:246）

```text
Add(item)
├── 加锁；shuttingDown → 忽略返回（+3）
├── item 已在 dirty:
│     ├── 已在 processing → 直接返回（处理中变脏，留待 Done 处理）
│     └── 不在 processing → queue.Touch（已排队，刷新）
├── 新 item:
│     ├── metrics.add
│     ├── dirty.Insert(item)
│     └── 若已在 processing → 只留 dirty，不 Push（处理中再变化）
│     └── 否则 queue.Push(item) + cond.Signal
```
**去重语义：** 处理前任意次 Add 合并为一次；处理中的 Add 保证处理完后再入队一次。

### 2.3 `Get`——阻塞取出（queue.go `q.Get+0`，:284）

```text
Get() → (item, shutdown)
├── queue 空且未关闭 → cond.Wait（+4）
├── queue.Pop → item
├── processing.Insert(item)（标记处理中）
├── dirty.Delete(item)（从待处理集合移除）
└── 返回 item
```
**shutdown 语义：** 队列排空后返回 `shutdown=true`，worker 据此退出。

### 2.4 `Done`——处理完成（queue.go `q.Done+0`，:309）

```text
Done(item)
├── processing.Delete(item)
├── item 仍在 dirty（处理期间再次变脏）→ queue.Push + cond.Signal（重新入队）
└── 否则若 processing 空且 shuttingDown → Signal 通知关闭等待
```
**关键：** 处理期间变脏的 item 在 `Done` 时自动重入队——**不丢更新**。

---

## 3. 开发视角：延迟队列（`delayingType[T]`）

### 3.1 `AddAfter`——延迟入队（delaying_queue.go `q.AddAfter+0`，:280）

```text
AddAfter(item, duration)
├── shuttingDown → 忽略（+2～+4）
├── duration ≤ 0 → 立即 q.Add(item)（+9～+12）
└── 否则：waitFor{item, readyAt} 放入最小堆 + 条件变量通知
```

### 3.2 `waitingLoop`——后台到点转主队列（delaying_queue.go `q.waitingLoop+0`，:307）

后台 goroutine 持有最小堆（按 `readyAt` 排序），到点的 `waitFor` 调 `q.Add(item)` 转入基础队列。控制器用它做**延迟重试**与**周期性兜底 resync**。

---

## 4. 开发视角：限流队列（`rateLimitingType[T]`）

### 4.1 三个方法（rate_limiting_queue.go）

| 方法 | 锚点 | 作用 |
|---|---|---|
| `AddRateLimited` | `q.AddRateLimited+0` (:157) | 按 rate limiter 算延迟 → `AddAfter`（+1～+2） |
| `NumRequeues` | `q.NumRequeues+0` (:161) | 该 item 累计重试次数 |
| `Forget` | `q.Forget+0` (:165) | 清除失败记录（+1～+2） |

### 4.2 默认 rate limiter——指数退避（default_rate_limiters.go）

`DefaultControllerRateLimiter`（default_rate_limiters.go `DefaultControllerRateLimiter+0`，:44）= **exponential + bucket** 组合：
- `TypedItemExponentialFailureRateLimiter.When`（:116）：按失败次数指数增长，base（默认 5ms）× 2^n，封顶 max（默认 1000s）。
- `NumRequeues`（:137）：返回失败次数。
- `Forget`（:144）：清零失败计数。
- 外层再包一个 token bucket（`TypedBucketRateLimiter.When`，:68）做整体限速。

**退避序列（默认）：** 5ms → 10ms → 20ms → 40ms → … → 1000s（封顶）。

---

## 5. 两条机制如何拼合（informer → workqueue → worker）

```text
informer 事件回调（OnAdd/OnUpdate/OnDelete）
  └── keyFunc(obj) → key
       └── queue.Add(key)                    基础队列去重
            └── [worker goroutine]
                 key, shutdown = queue.Get()
                      └── syncHandler(key)   慢业务（List/Update apiserver）
                           ├── 成功 → queue.Forget(key) + queue.Done(key)
                           └── 失败 → queue.AddRateLimited(key)（指数退避）+ queue.Done(key)
```

**为什么这样设计：** 回调快（只 Add）、worker 慢（syncHandler）、失败退避（AddRateLimited）——三者解耦，informer 不被慢业务阻塞，失败不会打爆 apiserver。

---

## 6. 关键保证汇总

| 保证 | 实现锚点 | 为什么必须保持 |
|---|---|---|
| 处理前去重 | `q.Add+0`（dirty 集合） | 同一 key 短时间多次事件只处理一次 |
| 处理中不丢更新 | `q.Add+0`（processing 分支）+ `q.Done+0`（dirty 命中重入队） | 处理期间对象又变，处理完重入队 |
| 优雅退出 | `q.Get+0`（shutdown=true）、`q.ShutDown+0`（:350） | worker 排空后退出，不悬挂 |
| 失败指数退避 | `q.AddRateLimited+0`、`When+0`（:116） | 失败不打爆 apiserver |
| 成功清计数 | `q.Forget+0`、`Forget+0`（:144） | 下次失败从 base 重新退避 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `TypedInterface+2`（queue.go:30）、`Typed+2`（:208）、`New+0`（:120）
- `q.Add+0`（:246）、`q.Len+0`（:275）、`q.Get+0`（:284）、`q.Done+0`（:309）、`q.ShutDown+0`（:350）
- `q.AddAfter+0`（delaying_queue.go:280）、`q.waitingLoop+0`（:307）
- `q.AddRateLimited+0`（rate_limiting_queue.go:157）、`q.NumRequeues+0`（:161）、`q.Forget+0`（:165）
- `DefaultControllerRateLimiter+0`（default_rate_limiters.go:44）、`When+0`（:116）、`NumRequeues+0`（:137）、`Forget+0`（:144）、`TypedBucketRateLimiter.When+0`（:68）

### 7.2 生产实测

**未实测** `[待运行验证]`。退避序列（5ms→1000s）、dirty/processing 交互、shutdown 排空均为代码推断，未在真实 controller 运行时观测重试间隔。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| pausable 队列 | 未深挖 | `pausableRateLimitingType`（rate_limiting_queue.go:172）的暂停语义未展开 |
| metrics | 未深挖 | `workqueue_metrics` 指标（depth/adds/latency）未逐行读 |
| parallelizer | 未深挖 | `parallelizer.go` 的并行 worker 协调未读 |

### 7.4 修改联动

- 改去重语义 → `q.Add`/`q.Done`，同步 [11-informer与workqueue机制](../sourceReader/11-informer与workqueue机制.md) §4。
- 改退避参数 → `DefaultControllerRateLimiter`/`When`，同步本文 §4.2。
- 改延迟语义 → `q.AddAfter`/`waitingLoop`。

**关联文档：** [11-informer与workqueue机制](../sourceReader/11-informer与workqueue机制.md)（informer 侧）、[08-controller副本控制](../sourceReader/08-controller副本控制.md)（ReplicaSet 用 workqueue 重试）、[informer-resync机制](informer-resync机制.md)（resync 侧）。

[总目录](../sourceReader/README.md)
