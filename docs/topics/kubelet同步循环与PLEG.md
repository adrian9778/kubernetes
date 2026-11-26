[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# kubelet 同步循环与 PLEG

> **层级**：跨篇机制纵深专题——把 kubelet 的**主循环（syncLoop）、per-Pod 串行 worker（podWorkers）、PLEG 容器状态探测**讲透：三源 PodConfig 合并、syncLoop 事件分派、per-Pod 串行化、PLEG Relist 生成容器事件。
>
> **代码基准**：`adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `kl.syncLoop+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[09-kubelet同步循环](../sourceReader/09-kubelet同步循环.md) §3 是浓缩版；本专题纵向展开 syncLoop→podWorkers→PLEG 的并发与事件语义。

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

### 3.1 `Run` → `syncLoop`（kubelet.go `kl.Run+0` → `kl.syncLoop+0`）

`Run` 启动全部内部组件（runtime、pleg、podWorkers、statusManager、volumeManager），最后进 `syncLoop`。`syncLoop` 是 for+select 循环，消费 `PodConfig.Updates()`；每轮先查 `runtimeState.runtimeErrors()`，runtime 报错则指数退避跳过本轮同步。

### 3.2 `syncLoopIteration`——事件分派（kubelet.go `kl.syncLoopIteration+0`）

```text
syncLoopIteration(ctx, configCh, syncCh, housekeepingCh, plegCh, ...)
├── select configCh（期望 Pod 变化）：
│   ├── ADD      → HandlePodAdditions
│   ├── UPDATE   → HandlePodUpdates
│   ├── REMOVE   → HandlePodRemoves
│   ├── RECONCILE→ HandlePodReconcile
│   └── DELETE   → HandlePodUpdates（因优雅删除，DELETE 视同 UPDATE）
├── select plegCh（PLEG 容器事件）→ isSyncPodWorthy 过滤 → HandlePodSyncs；
│   └── 事件为 ContainerDied 时额外 cleanUpContainersInPod
├── select syncCh（周期 resync）→ getPodsToSync → HandlePodSyncs
├── select liveness/readiness/startupManager.Updates() → handleProbeSync（探针失败/就绪/启动）
├── select containerManager.Updates()（设备等）→ HandlePodSyncs
├── select volumeSyncCh（卷状态就绪，新增）→ drainVolumePodSyncs → HandlePodSyncs
└── select housekeepingCh → HandlePodCleanups（清理孤儿容器/镜像，sources 未就绪则跳过）
```

**两种事件源：** 期望变化（configCh，来自 apiserver/静态文件）+ 容器实际变化（plegCh，来自 PLEG 探测）——后者是 kubelet 主动发现容器挂掉的关键。其余 channel（探针、containerManager、volumeSyncCh）是把「非 config 的同步诉求」统一汇到 `HandlePodSyncs`。

---

## 4. 开发视角：per-Pod 串行 worker（podWorkers）

### 4.1 `UpdatePod`——分派入口（pod_workers.go `p.UpdatePod+0`）

`syncLoopIteration` 经 `SyncHandler` 最终调 `podWorkers.UpdatePod`：
```text
UpdatePod(ctx, options)
├── 写/合并 podSyncStatuses（记录最新期望）
├── 取分配态：allocationManager.GetAllocatedPod → 写回 pendingUpdate.Pod 与 .AllocatedGeneration
├── 该 Pod 已有 worker → 唤醒（向 podUpdates channel 投递）
└── 无 worker → 创建 goroutine 跑 podWorkerLoop
```

### 4.2 `podWorkerLoop`——每 Pod 串行（pod_workers.go `p.podWorkerLoop+0`）

每个 Pod 一个 goroutine 跑 `podWorkerLoop`，签名含一个空复合字面量类型的 channel：
`func (p *podWorkers) podWorkerLoop(parentCtx context.Context, podUID types.UID, podUpdates <-chan struct{})`
```text
podWorkerLoop(parentCtx, podUID, podUpdates)
├── for range podUpdates（串行处理该 Pod 的同步请求）:
│   ├── startPodSync 取出 pendingUpdate（无则 continue）
│   ├── 按 update.WorkType 分派：
│   │   ├── TerminatedPod  → podSyncer.SyncTerminatedPod
│   │   ├── TerminatingPod → SyncTerminatingPod / SyncTerminatingRuntimePod
│   │   └── 默认（Sync）   → relistSuspender.SuspendRelist → podSyncer.SyncPod（= Kubelet.SyncPod）
│   ├── 同步返回后 ResumeRelist（先于 lastSyncTime 更新）→ 决定是否补一次 Relist
│   └── completeWork 决定是否 requeue（处理期间又来新更新）
```

**串行语义：** 同一 Pod 任意时刻只有一个同步在跑——避免并发同步同一 Pod 导致卷/容器状态竞争。不同 Pod 各自 goroutine，互不阻塞。

### 4.3 `SyncPod` / `SyncTerminatedPod`——真正同步（kubelet.go `kl.SyncPod+0` / `kl.SyncTerminatedPod+0`）

`SyncPod` 是 kubelet 的核心：计算 Pod 与容器现状的差集，创建/重启/杀死容器，挂载卷，跑探针（详见 [kubelet-CRI运行时](../sourceReader/10-kubelet-CRI运行时.md)、[kubelet探针机制](kubelet探针机制.md)）。当前基线两处要点：

- `SyncPod` 接收 `allocatedGeneration`，用它设置/清除 `PodResizeInProgressCondition`，并把 `AllocatedGeneration` 透传到运行时（Pod 原地 resize 的代际对齐）。
- `SyncTerminatedPod` 在卷路径清理后，额外从 `kl.actuatedState` 反注册该 Pod 的卷（`GetPodVolumes`/`RemovePodVolume`），再通知 secret/configmap manager。

### 4.4 workQueue 背压（带 jitter）

per-Pod workQueue 的重入队带随机抖动：
- 正常 resync：`wait.Jitter(p.resyncInterval, ...)`（pod_workers.go `p.completeWork+4`）。
- 瞬时网络错误退避：`wait.Jitter(backOffOnTransientErrorPeriod, ...)`（`p.completeWork+7`）。
- 失败/启动前退避：`wait.Jitter(p.backOffPeriod, ...)`（pod_workers.go `p.allowPodStart+12`）。
- **目的：** 避免大量 Pod 同步风暴打爆 runtime/apiserver。

---

## 5. 开发视角：PLEG（容器状态探测）

### 5.1 两种 PLEG

| 类型 | 实现 | 触发方式 |
|---|---|---|
| **GenericPLEG** | generic.go `NewGenericPLEG+0` | 周期 Relist（默认 1s） |
| **EventedPLEG** | evented.go `NewEventedPLEG+0` | 监听 CRI 容器事件（低延迟，feature gate） |

### 5.2 `GenericPLEG.Relist`——周期探测（generic.go `g.Relist+0`）

```text
Relist(ctx)                                    g.Relist+0
├── podList = g.runtime.GetPods(ctx, true)     g.Relist+17（列全部容器现状）
├── g.podRecords.setCurrent(pods)
├── for pid ∈ podRecords:
│    └── reconcilePodRecord(ctx, pid)          （g.reconcilePodRecord+0）
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
└── GenericPLEG.Relist (generic.go g.Relist+0) 周期探测
     └── reconcilePodRecord (g.reconcilePodRecord+0) 发现容器 Died → PodLifecycleEvent → plegCh
          └── syncLoopIteration (kubelet.go kl.syncLoopIteration+0) 收 plegCh → HandlePodSyncs
               └── podWorkers.UpdatePod (pod_workers.go p.UpdatePod+0) 唤醒 worker
                    └── podWorkerLoop (pod_workers.go p.podWorkerLoop+0) 串行取 work
                         └── Kubelet.SyncPod (kubelet.go kl.SyncPod+0)
                              └── 按 restartPolicy 重启容器 → 上报 status
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 上复核（读者用 `offset` 子命令取实时行号）：
- `kl.Run+0`、`kl.syncLoop+0`、`kl.syncLoopIteration+0`、`kl.SyncPod+0`、`kl.SyncTerminatedPod+0`（均 kubelet.go）
- `p.UpdatePod+0`、`p.podWorkerLoop+0`、`p.completeWork+0`、`p.allowPodStart+0`（均 pod_workers.go，签名 `podWorkerLoop` 含 `podUpdates <-chan struct{}`）
- `g.Relist+0`、`g.Relist+17`（GetPods）、`g.reconcilePodRecord+0`、`NewGenericPLEG+0`（均 generic.go）
- `NewEventedPLEG+0`（evented.go）

**本次基准漂移（`782c049`→`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`）核对结论：**
- `syncLoopIteration` 新增 `volumeSyncCh` 分支（卷状态就绪触发 `HandlePodSyncs`），已补入 §3.2；并订正 DELETE 实际走 `HandlePodUpdates`（非 Removes）。
- `podWorkerLoop` 分派按 `WorkType` 走 Sync/Terminating/Terminated 三态，`UpdatePod` 现取分配态 `GetAllocatedPod` 并透传 `AllocatedGeneration`；`SyncPod`/`SyncTerminatedPod` 相应变化见 §4.3。已补入 §4。
- `status_manager.go` 漂移集中在 Pod resize condition 的 `ObservedGeneration` 语义与 `DynamicNodeLocalEphemeralVolumes`，属 §7.3 既有「statusManager 未深挖」缺口，本篇不展开。
- `kubelet_pleg.go`/`generic.go`/`evented.go` 的 PLEG 符号定义行与语义未变，无需改写。

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

**关联文档：** [09-kubelet同步循环](../sourceReader/09-kubelet同步循环.md)（kubelet 全貌）、[10-kubelet-CRI运行时](../sourceReader/10-kubelet-CRI运行时.md)（SyncPod CRI 细节）、[kubelet探针机制](kubelet探针机制.md)（探针）、[workqueue机制](workqueue机制.md)（背压队列范式）。

[总目录](../sourceReader/README.md)
