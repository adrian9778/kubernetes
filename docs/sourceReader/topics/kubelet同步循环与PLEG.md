[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# kubelet 同步循环与 PLEG

> **层级**：跨篇机制纵深专题——把 kubelet 的**主循环（syncLoop）、per-Pod 串行 worker（podWorkers）、PLEG 容器状态探测**讲透：三源 PodConfig 合并、syncLoop 事件分派、per-Pod 串行化、PLEG Relist 生成容器事件。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `kl.syncLoop+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[09-kubelet同步循环](../09-kubelet同步循环.md) §3 是浓缩版；本专题纵向展开 syncLoop→podWorkers→PLEG 的并发与事件语义。

---

## 0. 结论速览

### 0.1 kubelet 的唯一收敛链路

所有 Pod 状态推进都经一条链路：
```text
三源 PodConfig → syncLoop（select）→ podWorkers（每 Pod 串行）→ SyncPod → CRI/卷/探针
```

### 0.2 关键结论

1. **三源合并**：期望 Pod 来自 apiserver、静态 Pod 文件、HTTP manifest 三源，`PodConfig` 归并成统一 `PodUpdate` 流。
2. **没有独立分发循环**：全仓库无 `dispatchWork`——对 Pod 的「分派」由 `podWorkers.UpdatePod` 承担（写 status + 唤醒/创建 worker）。
3. **per-Pod 串行**：每个 Pod 一个 worker goroutine（`podWorkerLoop`），同一 Pod 的同步**串行**，不同 Pod 并行。
4. **PLEG 是状态探测器**：`GenericPLEG` 周期 Relist 容器现状，比对生成 `PodLifecycleEvent`，驱动 kubelet 对容器级变化做反应（即使 apiserver 没推事件）。
5. **worker 背压 + 抖动**：per-Pod workQueue 用 `wait.Jitter` 加随机抖动，避免同步风暴。

---

## 1. 生产视角：kubelet 对外行为

### 1.1 可见结果

Pod 的 `status.phase`/`containerStatuses` 被逐步推进到期望；异常容器按 restartPolicy 重启；静态 Pod 镜像为 apiserver Pod；节点 `Ready` 反映 runtime 健康。

### 1.2 可观测信号

- 容器状态与重启计数变化。
- 事件：`Pulling`/`Pulled`/`Failed`/`Started`/`Killing`。
- PLEG 相关：`PLEG is not healthy`（Relist 超时/卡死时节点 NotReady）。

---

## 2. 开发视角：三源合并（PodConfig）

### 2.1 三个独立源

| 源 | 说明 |
|---|---|
| **apiserver** | `spec.nodeName=<本节点>` 的 Pod（watch） |
| **静态 Pod 文件** | fsnotify 监听 manifest 目录 |
| **HTTP manifest** | 周期拉取 URL |

### 2.2 PodConfig 多路复用

`PodConfig.Channel`/`Updates`（config）：各源往自己 channel 写，`podStorage.Merge` 按 source 归并成一份期望集合，产出统一 `<-chan PodUpdate` 给 syncLoop。

**关键抽象：** 三源产出同构的 ADD/UPDATE/REMOVE/RECONCILE，syncLoop 不关心来源；每个 Pod 打 `ConfigSourceAnnotationKey` 区分静态/动态。

---

## 3. 开发视角：主循环（syncLoop）

### 3.1 `Run` → `syncLoop`（kubelet.go `kl.Run+0`，:1988 → `kl.syncLoop+0`，:2814）

`Run` 启动全部内部组件（runtime、pleg、podWorkers、statusManager、volumeManager），最后进 `syncLoop`。`syncLoop` 是 select 循环，消费 `PodConfig.Updates()`。

### 3.2 `syncLoopIteration`——事件分派（kubelet.go `kl.syncLoopIteration+0`，:2889）

```text
syncLoopIteration(ctx, configCh, handler, ...)
├── select configCh（期望 Pod 变化）：
│   ├── ADD      → HandlePodAdditions
│   ├── UPDATE   → HandlePodUpdates
│   ├── REMOVE   → HandlePodRemoves
│   ├── RECONCILE→ HandlePodReconcile
│   └── DELETE   → HandlePodRemoves
├── select plegCh（PLEG 容器事件）→ HandlePodSyncs
├── select housekeepingTicker → housekeeping（清理孤儿容器/镜像）
└── select livenessManager.Updates() → 处理 liveness 失败
```

**两种事件源：** 期望变化（configCh，来自 apiserver/静态文件）+ 容器实际变化（plegCh，来自 PLEG 探测）——后者是 kubelet 主动发现容器挂掉的关键。

---

## 4. 开发视角：per-Pod 串行 worker（podWorkers）

### 4.1 `UpdatePod`——分派入口（pod_workers.go `p.UpdatePod+0`，:784）

`syncLoopIteration` 经 `SyncHandler` 最终调 `podWorkers.UpdatePod`：
```text
UpdatePod(options)
├── 写/合并 podSyncStatuses（记录最新期望）
├── 该 Pod 已有 worker → 唤醒（workQueue.Enqueue）
└── 无 worker → 创建 goroutine 跑 podWorkerLoop
```

### 4.2 `podWorkerLoop`——每 Pod 串行（pod_workers.go `p.podWorkerLoop+0`，:1266）

每个 Pod 一个 goroutine 跑 `podWorkerLoop`：
```text
podWorkerLoop(ctx, podUID)
├── for（串行处理该 Pod 的同步请求）:
│   ├── 从 workQueue 取一个 work item（带 jitter 背压）
│   ├── completeWork 前先检查是否要 requeue（处理期间又来新更新）
│   └── 调 syncPodFn（= Kubelet.SyncPod）执行真正同步
```

**串行语义：** 同一 Pod 任意时刻只有一个 SyncPod 在跑——避免并发同步同一 Pod 导致卷/容器状态竞争。不同 Pod 各自 goroutine，互不阻塞。

### 4.3 `SyncPod`——真正同步（kubelet.go `kl.SyncPod+0`，:2158）

`SyncPod` 是 kubelet 的核心：计算 Pod 与容器现状的差集，创建/重启/杀死容器，挂载卷，跑探针（详见 [kubelet-CRI运行时](../10-kubelet-CRI运行时.md)、[kubelet探针机制](kubelet探针机制.md)）。

### 4.4 workQueue 背压（带 jitter）

per-Pod workQueue 的重入队带随机抖动：
- 正常 resync：`wait.Jitter(resyncInterval, ...)`（pod_workers.go:1572）。
- 失败退避：`wait.Jitter(backOffPeriod, ...)`（:1082）、`backOffOnTransientErrorPeriod`（:1575）。
- **目的：** 避免大量 Pod 同步风暴打爆 runtime/apiserver。

---

## 5. 开发视角：PLEG（容器状态探测）

### 5.1 两种 PLEG

| 类型 | 实现 | 触发方式 |
|---|---|---|
| **GenericPLEG** | generic.go `NewGenericPLEG+0`（:139） | 周期 Relist（默认 1s） |
| **EventedPLEG** | evented.go `NewEventedPLEG+0`（:52） | 监听 CRI 容器事件（低延迟，feature gate） |

### 5.2 `GenericPLEG.Relist`——周期探测（generic.go `g.Relist+0`，:296）

```text
Relist(ctx)                                    g.Relist+0
├── podList = g.runtime.GetPods(ctx, true)     g.Relist+17（列全部容器现状）
├── g.podRecords.setCurrent(pods)
├── for pid ∈ podRecords:
│    └── reconcilePodRecord(ctx, pid)          （g.reconcilePodRecord+0，:339）
│          ├── oldPod vs currentPod 比对
│          └── 容器状态变化 → 生成 PodLifecycleEvent（Started/Died/...）→ eventChannel
└── g.cache.UpdateTime(timestamp)              更新缓存时间戳
```

**PLEG 的意义：** 即使 apiserver 没推任何事件（如容器 OOMKilled、节点网络分区），PLEG 周期 Relist 也能发现容器实际状态变化，触发 kubelet 重新 SyncPod。这是 kubelet **自愈**的基础。

### 5.3 PLEG 健康与节点 NotReady

`plegCh` 长时间无事件（Relist 卡住，如 runtime 不响应）→ kubelet 判 `PLEG is not healthy` → 节点 `Ready` 条件转 False → Pod 被驱逐。这是 runtime 故障的典型表现。

---

## 6. 全链路时序（容器挂掉的自愈）

```text
容器 OOMKilled（apiserver 不知道）
└── GenericPLEG.Relist (generic.go:296) 周期探测
     └── reconcilePodRecord (:339) 发现容器 Died → PodLifecycleEvent → plegCh
          └── syncLoopIteration (kubelet.go:2889) 收 plegCh → HandlePodSyncs
               └── podWorkers.UpdatePod (pod_workers.go:784) 唤醒 worker
                    └── podWorkerLoop (pod_workers.go:1266) 串行取 work
                         └── Kubelet.SyncPod (kubelet.go:2158)
                              └── 按 restartPolicy 重启容器 → 上报 status
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `kl.Run+0`（kubelet.go:1988）、`kl.syncLoop+0`（:2814）、`kl.syncLoopIteration+0`（:2889）、`kl.SyncPod+0`（:2158）
- `p.UpdatePod+0`（pod_workers.go:784）、`p.podWorkerLoop+0`（:1266）；workQueue 重入队（:1082/:1572/:1575，带 Jitter）
- `g.Relist+0`（generic.go:296）、`g.Relist+17`（GetPods）、`g.reconcilePodRecord+0`（:339）、`NewGenericPLEG+0`（:139）
- `NewEventedPLEG+0`（evented.go:52）

### 7.2 生产实测

**未实测** `[待运行验证]`。PLEG Relist 周期、worker jitter 背压实际间隔、PLEG not healthy 判定阈值均为代码推断，未在真实节点观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| EventedPLEG 细节 | 未深挖 | CRI 事件订阅与去重未逐行读 |
| housekeeping | 未深挖 | 孤儿容器/镜像 GC 周期未展开 |
| podWorker 终止状态机 | 未深挖 | `podSyncStatus` 各 Is* 状态转移未逐行读 |
| statusManager 上报 | 未深挖 | PATCH apiserver 的批处理/节流未展开 |

### 7.4 修改联动

- 改事件分派 → `syncLoopIteration`，同步本文 §3.2。
- 改 per-Pod 串行 → `podWorkerLoop`/`UpdatePod`，同步 §4。
- 改 PLEG 周期/健康判定 → `GenericPLEG.Relist`，同步 §5。

**关联文档：** [09-kubelet同步循环](../09-kubelet同步循环.md)（kubelet 全貌）、[10-kubelet-CRI运行时](../10-kubelet-CRI运行时.md)（SyncPod CRI 细节）、[kubelet探针机制](kubelet探针机制.md)（探针）、[workqueue机制](workqueue机制.md)（背压队列范式）。

[总目录](../README.md)
