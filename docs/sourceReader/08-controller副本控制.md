[上一篇](07-scheduler调度与绑定.md) · [总目录](README.md) · [下一篇](09-kubelet同步循环.md)

# controller 副本控制
> **层级**：第四层——函数级源码解析。
> **范围**：以 ReplicaSet 控制器为样本，完整解析 informer 事件入队、worker 调谐、期望计数（expectations）、慢启动批量创建、并发删除与状态更新；末尾给出 Deployment→ReplicaSet 层级定位。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 控制器目录 | `pkg/controller/replicaset`（本样本），其余控制器同模式位于 `pkg/controller/<name>` |
| 组件入口 | `cmd/kube-controller-manager/app`（控制器注册在 core.go/apps.go/batch.go） |
| 依赖 | `[staging] client-go`（informer/lister、workqueue）、controller 工具包（expectations、pod control） |
| 对外行为 | 对 apiserver：创建/删除 Pod、更新 ReplicaSet status |
| 数据流 | RS/Pod informer 事件 → key 去重入队 → worker 调谐 → 比对 Pod 现状与 Replicas → create/delete → 更新 status |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `ReplicaSetController` | `pkg/controller/replicaset/replica_set.go:97` | 持有 RS/Pod lister、indexer、`queue workqueue.RateLimitingInterface`、`syncHandler`、expectations、podControl、burstReplicas |
| `ControlleeExpectations`（expectations） | client-go controller 工具包 | 记录"已下发但 informer 尚未观测到"的 create/delete 数量，抑制过早调谐 |
| `RealPodControl` | `pkg/controller/replicaset/ 同目录 pod control | `CreatePods` / `DeletePod` 的实际 apiserver 调用实现 |

## 3. 函数级

### 函数：NewReplicaSetController
**源码：** `pkg/controller/replicaset/replica_set.go:155`
**职责：** 构造控制器并绑定 informer 事件：RS informer 的 Add/Update/Delete 与 Pod informer 的 addPod/updatePod/deletePod；事件处理最终汇入 `enqueueRS`/`enqueueRSAfter`。
**关键赋值：** `rsc.syncHandler = rsc.syncReplicaSet`（:273）。

### 函数：(*ReplicaSetController).Run
**源码：** `pkg/controller/replicaset/replica_set.go:279`
**执行流程：**
1. 启动事件广播与 sink（+2 ～ +5）
2. `WaitForNamedCacheSyncWithContext`：等待 RS/Pod informer 首次 list 完成（+18 ～ +20）
3. 启动 N 个 worker：`wait.UntilWithContext(ctx, rsc.worker, time.Second)`（+22 ～ +26）
4. 阻塞至 ctx 取消；退出时 `queue.ShutDown()` 并等 worker 收尾（+11 ～ +16、+27）
**前置条件：** 缓存必须先同步；未同步直接返回，不启动 worker。

### 函数：(*ReplicaSetController).enqueueRS / enqueueRSAfter
**源码：** `pkg/controller/replicaset/replica_set.go:371 / :380`
**职责：** 计算对象 key（`namespace/name`）并入队；延迟版用 AddAfter。
**错误处理：** key 计算失败仅记录，不 panic。

### 函数：worker / processNextWorkItem
**源码：** `pkg/controller/replicaset/replica_set.go:631`
**执行流程：**
1. `queue.Get()` 阻塞取 key（+1 ～ +3）
2. `defer queue.Done(key)`（+5）
3. `syncHandler(ctx, key)` 即 syncReplicaSet（+7）
4. 成功：`queue.Forget(key)` 清退避记录（+9 ～ +10）
5. 失败：记录日志 + `queue.AddRateLimited(key)` 指数退避重入（+14 ～ +15）
**返回值：** bool（queue 关闭时 false，结束 worker）。

### 函数：(*ReplicaSetController).syncReplicaSet
**源码：** `pkg/controller/replicaset/replica_set.go:759`
**职责：** 单个 RS 的完整调谐。
**参数：** `ctx`、`key string`（namespace/name）。
**返回值：** `error`（非零导致 rate-limited 重入）。
**执行流程：**
```text
syncReplicaSet (+0)
├── 1. SplitMetaNamespaceKey (+6 ～ +8)
├── 2. consistencyStore.EnsureReady（一致性检查）(+11 ～ +21)
├── 3. rsLister.Get；NotFound → 清理 expectations 后正常返回 (+22 ～ +32)
├── 4. expectations.SatisfiedExpectations → rsNeedsSync (+35)
├── 5. LabelSelectorAsSelector (+36 ～ +39)
├── 6. FilterPodsByOwner（含孤儿 Pod）(+42 ～ +44)
├── 7. FilterActivePods → claimPods 认领导主 (+49 ～ +52)
├── 8.（feature gate）统计 terminatingPods (+54 ～ +58)
├── 9. rsNeedsSync 且未删除 → manageReplicas (+63)
├── 10. calculateStatus（Replicas/Ready/Available 计数）(+68)
├── 11. updateReplicaSetStatus 写 status (+70 ～ +74)
├── 12. consistencyStore.WroteAt (+77 ～ +82)
└── 13. manageReplicasErr 返回 / 规划 MinReady 下次检查 (+83 ～ end)
```
**关键设计：** NotFound 是正常事件（已删除），返回 nil 不重试；status 每轮无条件更新（:829 注释"Pod 起来或死亡都更新 status"）。

### 函数：(*ReplicaSetController).manageReplicas
**源码：** `pkg/controller/replicaset/replica_set.go:653`
**职责：** 比对活跃 Pod 数与期望 Replicas，执行增删；明确不修改入参 activePods（:650 注释）。
**参数：** `activePods []*v1.Pod`、`rs *apps.ReplicaSet`。
**返回值：** `error`。
**执行流程：**
```text
manageReplicas (+0)
├── diff = len(activePods) - *rs.Spec.Replicas (+1)
├── diff < 0（Pod 不足）:
│   ├── 取绝对值并受 burstReplicas 限速 (+8 ～ +10)
│   ├── expectations.ExpectCreations(rsKey, diff) (+13)
│   ├── slowStartBatch 慢启动创建 (+24 ～ +38)
│   │    初始批量 SlowStartInitialBatchSize，逐轮翻倍
│   │    podControl.CreatePods（带 ControllerRef）(+25 ～ +27)
│   └── 跳过的 Pod 逐个 CreationObserved 回补 expectations (+44 ～ +51)
└── diff > 0（Pod 过多）:
    ├── burstReplicas 限速 (+55 ～ +57)
    ├── getIndirectlyRelatedPods (+63)
    ├── getPodsToDelete 选 Pod（优先未启动完成的）(+66)
    ├── expectations.ExpectDeletions（快照 Pod key）(+73)
    ├── 并发 DeletePod（goroutine + WaitGroup）(+77 ～ +90)
    │    失败且非 NotFound：DeletionObserved + errCh
    └── 返回 errCh 中第一个错误 (+94 ～ +102)
```
**慢启动意图（源码注释 :74 ～ +82）：** 大批量 Pod 若因同一原因（如配额不足）失败，慢启动在早期失败后停止放大，避免 Pod create 与事件洪泛。
**并发：** 删除并发执行；创建经 slowStartBatch 内部串行分批；`errCh` 带缓冲，wg.Wait 后只取首个错误。
**副作用：** expectations 状态、apiserver 写操作。

### 函数：calculateStatus / updateReplicaSetStatus
**源码：** 同包 `replica_set.go`（calculatePos）/ update 函数
**职责：** 依据 active/terminating Pod 与 manageReplicas 错误计算 Conditions 与各计数字段；以 client 做 status update（可能产生冲突，冲突即返回错误触发重试）。

## 4. expectations 机制要点
- 下发 create/delete 前先 Expect；informer 观测到对应 Pod 事件后 Observed 抵消。
- expectations 未满足期间 `SatisfiedExpectations=false`，syncReplicaSet 跳过 manageReplicas，只更新 status。
- 兜底：expectations 有超时，Pod 事件丢失不会永久卡住（MinReady 检查队列也作兜底，:846 ～ +848 注释）。

## 5. Deployment → ReplicaSet 层级
- `kubectl create deployment`（或 `kubectl run` 不带 `--restart=Never`）创建的是 Deployment（`pkg/controller/deployment`）。
- Deployment 控制器调谐时按 pod-template-hash 维护 ReplicaSet 集合（滚动更新即新旧 RS 之间按比例切 Replicas）。
- ReplicaSet 再按本文件所述维护 Pod。三层同属 informer→queue→sync 的调谐模式；本文档库只对 ReplicaSet 层做函数级展开。

## 6. 错误处理汇总
| 来源 | 处理 |
|---|---|
| syncHandler 任意错误 | AddRateLimited 退避重试 |
| Create Pod 时 namespace terminating | 视为成功（nil），不重试 |
| 慢启动中部分失败 | 回补 expectations，等下轮 resync |
| Delete Pod 返回 NotFound | 忽略（已达目的） |
| RS status 更新失败 | 返回错误触发 requeue（注释说明可避免热循环） |
| selector 转换失败 | 记录日志并返回 nil（等待事件再次唤醒） |

[上一篇](07-scheduler调度与绑定.md) · [总目录](README.md) · [下一篇](09-kubelet同步循环.md)
