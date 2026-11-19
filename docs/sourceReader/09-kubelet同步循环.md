[上一篇](08-controller副本控制.md) · [总目录](README.md) · [下一篇](10-kubelet-CRI运行时.md)

# kubelet 同步循环
> **层级**：第四层——函数级源码解析。
> **范围**：Kubelet 启动序列、PodConfig 多源合并、syncLoop 主循环与事件分派、podWorkers 串行化、PLEG、statusManager 上报。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **锚点记法**：`符号+偏移`，函数声明行（含 `func` 的行）= +0，逐行递增；跨文件引用写作 `文件#符号+偏移`。

## 0. 生产视角（kubelet 对外行为与可见结果）

kubelet 是运行在**每个节点**上的节点级 agent。从外部看，它做四件事：

1. **认领并观测期望态**：以节点名 `spec.nodeName=<本节点>` 从 apiserver 用 ListWatch（Reflector）订阅本节点的 Pod，另从静态 Pod 文件（fsnotify）与 HTTP manifest 两路输入；三源经 `PodConfig` 合并成一份期望 Pod 集合。
2. **收敛实际态**：把节点上真实运行的容器（经 CRI 查询 runtime）向期望态收敛——创建缺失容器、重启异常容器、删除多余容器、挂载卷、注入探针。
3. **回写状态与事件**：把每个 Pod 的实际状态经 `statusManager` 批量 PATCH 回 apiserver；生命周期/健康变化写 Event。
4. **维持节点健康**：周期上报节点状态与 lease、注册/注销、上报 RuntimeUp 就绪、处理静态 Pod。

**可见结果**：Pod 的 `status.phase`/`containerStatuses` 被逐步推进到期望；异常容器按 restartPolicy 被重启；静态 Pod 被镜像为 apiserver Pod；节点 `Ready` 条件反映 runtime 健康。以上全部经由「syncLoop 主循环 → podWorkers 每 Pod 串行 worker → SyncPod → CRI/卷/探针」这条唯一收敛链路完成。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/kubelet`（main）、`cmd/kubelet/app`（NewKubeletCommand、Run、run、createAndInitKubelet、startKubelet） |
| 核心 | `pkg/kubelet/kubelet.go`（Kubelet、NewMainKubelet、Run、syncLoop）、`pkg/kubelet/config`（PodConfig 三源）、`pkg/kubelet/pod_workers.go` |
| 状态/事件 | `pkg/kubelet/pleg`（GenericPLEG/EventedPLEG）、`pkg/kubelet/status`（manager）、`pkg/kubelet/pod`（podManager） |
| 依赖 | `[staging] client-go`（reflector、cache）、`[staging] cri-client/cri-api` |
| 数据流 | 三源 PodUpdate → PodConfig 合并 → syncLoop select → podWorkers（每 Pod 串行）→ SyncPod；PLEG → 事件/缓存；statusManager → apiserver |

**本模块不存在的机制**：全仓库 grep `dispatchWork` 为 0 命中——kubelet 没有独立的分发循环；对 Pod 的「分派」职责由 `podWorkers.UpdatePod`（写 status + 唤醒/创建 worker）承担。

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Kubelet` | `pkg/kubelet/kubelet.go` | 顶层协调者：持有 runtime、pleg、podWorkers、statusManager、podManager、allocationManager、volumeManager |
| `SyncHandler` | 同文件 | syncLoop 回调接口：HandlePodAdditions/Updates/Removes/Reconcile/Syncs/Cleanups |
| `PodConfig` | `pkg/kubelet/config/config.go` | 多源 mux：各源写入 podStorage，Merge 后产出 PodUpdate |
| `podWorkers` | `pkg/kubelet/pod_workers.go` | per-Pod worker 与终止状态机，持有 podSyncStatuses、podCache、podSyncer、relistSuspender |
| `podSyncStatus` | 同文件 | 单个 Pod 的同步状态（syncedAt/terminatingAt/terminatedAt/finished） |
| `podRelistSuspender` | 同文件 | `SuspendRelist`/`ResumeRelist`/`RequestPodRelist` 三方法接口，同步期间挂起 PLEG 对该 Pod 的 relist 观察 |
| `GenericPLEG` | `pkg/kubelet/pleg/generic.go` | 周期 Relist 比对运行时现状，生成 PodLifecycleEvent |
| `EventedPLEG` | `pkg/kubelet/pleg/evented.go` | 监听 CRI 容器事件，按需触发低延迟的单 Pod relist |
| `manager`（status） | `pkg/kubelet/status/status_manager.go` | PodStatus 缓存与批量 patch 上报 |

## 3. 函数级

### 函数：NewMainKubelet
**源码：** `pkg/kubelet/kubelet.go#NewMainKubelet+0`（定义行 :481）
**职责：** 构造 Kubelet 及其全部子组件。
**关键动作：**
1. 构造 `config.NewPodConfig(...)`（config.go:61），随后向 `cfg.Channel(ctx, source)` 注册三源：
   - apiserver：`pkg/kubelet/config/apiserver.go#NewSourceApiserver`（:36，ListWatch `spec.nodeName=<本节点>` + Reflector）
   - file：静态 Pod 文件（fsnotify）
   - http：HTTP manifest 周期拉取
2. 建 podManager、containerRuntime（CRI）、PLEG（按 `features.EventedPLEG` 门控可选创建 EventedPLEG 并注入 relister）、podWorkers（`newPodWorkers(klet,...)`，podSyncer 即 Kubelet 本体）、statusManager、volumeManager、allocationManager
**后置条件：** 返回可 Run 的 Kubelet；此时各源尚未开始推送。

### 函数：(*PodConfig).Channel / Updates
**源码：** `pkg/kubelet/config/config.go#(*PodConfig).Channel+0`（:75）/ `#(*PodConfig).Updates+0`（:102）
**职责：** Channel 按 source 名建输入通道并注册到 podStorage；Updates 返回合并后的 PodUpdate 输出通道。
**合并语义：** podStorage.Merge 把各源 Pod 按 UID 归并，产出 ADD/UPDATE/REMOVE/RECONCILE 的 PodUpdate（含 Source 标记）。

### 函数：(*Kubelet).Run
**源码：** `pkg/kubelet/kubelet.go#(*Kubelet).Run+0`（定义行 :1988）
**职责：** 启动全部内部组件，最后进入 syncLoop。
**执行流程：**
1. 准备日志查询 server（`Run+2` ～ `Run+35`）
2. `initializeModules`（`Run+41`；失败写事件并 os.Exit(1)）
3. `allocationManager.Run`（`Run+53`）、`go volumeManager.Run`（`Run+57`）
4. 有 kubeClient 时启动：node status 周期上报（`Run+69`）、fastStatusUpdateOnce（`Run+78`）、nodeLeaseController（`Run+83`）、fastStaticPodsRegistration（`Run+90`）
5. `go updateRuntimeUp` 每 5s（`Run+92`）
6. `statusManager.Start`（`Run+100`）、`pleg.Start`（`Run+108`；可选 EventedPLEG `Run+110` ～ `Run+112`，`if kl.eventedPlegWatcher != nil`）
7. healthChecker 装配（`Run+114`）
8. `kl.syncLoop(ctx, updates, kl)` 阻塞（`Run+118`）
**状态变化：** 从构造态进入运行态；syncLoop 是最后且永不返回（直至节点关闭）的主循环。

### 函数：(*Kubelet).syncLoop
**源码：** `pkg/kubelet/kubelet.go#(*Kubelet).syncLoop+0`（定义行 :2814）
**职责：** 主循环外壳：定时器、runtime 健康检查、退避。
**执行流程：**
1. 三个时间/事件源：syncTicker（1s，`syncLoop+6`）、housekeepingTicker（housekeepingPeriod，`syncLoop+8`）、`pleg.Watch()` 事件通道（`syncLoop+10`）
2. resolv.conf 检查（`syncLoop+20`）
3. 循环：runtime 有错误时指数退避（base 100ms、factor 2、max 5s，`syncLoop+28` ～ `syncLoop+29`）；健康则重置退避（`syncLoop+33`）
4. 更新 syncLoopMonitor 时间戳，调 `syncLoopIteration(...)`（`syncLoop+36`）；返回 false 退出（`syncLoop+37`）
**设计意图：** runtime 不健康时跳过 Pod 同步并退避，避免对故障运行时高频重试。

### 函数：(*Kubelet).syncLoopIteration
**源码：** `pkg/kubelet/kubelet.go#(*Kubelet).syncLoopIteration+0`（定义行 :2889）
**职责：** 多路 select，一次处理一个事件。
**事件源与动作：**
| 来源 | 动作 |
|---|---|
| configCh | 按 Op 分派：ADD→HandlePodAdditions（`syncLoopIteration+19`）、UPDATE→Updates、REMOVE→Removes、RECONCILE→Reconcile、DELETE→Updates（优雅删除视同更新）；末尾 AddSource 标记 source 已上报 |
| plegCh | isSyncPodWorthy → HandlePodSyncs（`syncLoopIteration+38` ～ `syncLoopIteration+52`，HandlePodSyncs `syncLoopIteration+43`）；ContainerDied → cleanUpContainersInPod（`syncLoopIteration+50` ～ `syncLoopIteration+53`） |
| syncCh | getPodsToSync → HandlePodSyncs（`syncLoopIteration+55` ～ `syncLoopIteration+62`） |
| liveness/readiness/startup | 失败/就绪 → 探针同步与 readiness/status 写入（`syncLoopIteration+63` ～ `syncLoopIteration+85`） |
| containerManager | 设备分配变化 → HandlePodSyncs（`syncLoopIteration+85` ～ 末尾） |
**返回值：** configCh 关闭时 false（主循环退出，`syncLoopIteration+119`）。
**关键约束（注释 `syncLoopIteration-16` ～ `syncLoopIteration-13`，kubelet.go:2873-2876）：** select 分支随机序，事件处理无固定优先级。

### 函数：(*Kubelet).HandlePodAdditions
**源码：** `pkg/kubelet/kubelet.go#(*Kubelet).HandlePodAdditions+0`（定义行 :3047）
**职责：** 新 Pod 登记、本地准入、下发 PodWorker。
**执行流程：**
1. 按创建时间排序（`HandlePodAdditions+3`，sortPodsForAdmission）
2. 每个 Pod `podManager.AddPod`（`HandlePodAdditions+10`，期望状态真相源）
3. mirror pod 走 Update 路径（`HandlePodAdditions+12` ～ `HandlePodAdditions+24`，mirror 走 UpdatePod）
4. 准入门检查 `IsPodTerminationRequested`（`HandlePodAdditions+34`）；非终止 Pod：`allocationManager.AddPod` 本地准入（`HandlePodAdditions+38`），失败 `rejectPod` 并跳过（`HandlePodAdditions+39`）
5. 通过后 `podCertificateManager.TrackPod`（`HandlePodAdditions+49`）；（feature gate）in-place resize 检查（`HandlePodAdditions+54`）
6. `podWorkers.UpdatePod(UpdatePodOptions{UpdateType: SyncPodCreate})`（`HandlePodAdditions+59`）；`BackfillPodResizeConditions`（`HandlePodAdditions+66`）

### 函数：(*podWorkers).UpdatePod
**源码：** `pkg/kubelet/pod_workers.go#(*podWorkers).UpdatePod+0`（定义行 :784）
**职责：** 接收一次 Pod 更新，维护 podSyncStatus 并唤醒/创建该 Pod 的 worker。
**执行流程：**
1. 解析 uid/ns/name；RunningPod 孤儿场景特殊处理（`UpdatePod+5` ～ `UpdatePod+24`）
2. 加 `podLock`（`UpdatePod+27` ～ `UpdatePod+28`）
3. 查 podSyncStatuses：首见建 status（firstTime，`UpdatePod+33` ～ `UpdatePod+45`，`status=&podSyncStatus{}` `UpdatePod+37`；kubelet 重启终态补 terminated 分支 `UpdatePod+42` ～ `UpdatePod+64`）
4. 更新 status 后向 per-Pod `podUpdates` channel 发信号；首见时 `go podWorkerLoop(...)`（`UpdatePod+73` 起；含 restartRequested `UpdatePod+97`、IsFinished 早退 `UpdatePod+106`）
**并发：** 一把全局 podLock 保护 status map；实际 Pod 操作在各 worker goroutine。
**分发职责：** 本函数是 kubelet 唯一的「按 Pod 分派」点（仓库无 dispatchWork），每个 Pod 一条更新通道 + 一个 worker goroutine。

### 函数：(*podWorkers).podWorkerLoop
**源码：** `pkg/kubelet/pod_workers.go#(*podWorkers).podWorkerLoop+0`（定义行 :1266）
**职责：** 单个 Pod 的串行执行循环。
**执行流程：**
```text
podWorkerLoop (+0)
├── for range podUpdates:
│   ├── startPodSync 取本次更新与启动门控 (+3)
│   ├── canEverStart=false → return（Pod 已终止）(+10~+11)
│   ├── canStart=false → 等待下次更新 (+14~+15)
│   ├── podCache.GetNewerThan 取最新运行时状态 (+43，最多约 2s 延迟)
│   └── 按 WorkType 分派 (+56~):
│        ├── TerminatedPod → SyncTerminatedPod (+57)
│        ├── TerminatingPod → SyncTerminatingPod/RuntimePod（带 gracePeriod）(+60)
│        └── 默认 → podSyncer.SyncPod（即 Kubelet.SyncPod）(+79)
│             └── relistSuspender.SuspendRelist(podUID) (+76) → SyncPod → ResumeRelist (+90)
└── （错误处理/退避在 loop 外层）
```
**串行化语义：** 同一 Pod 的下一次更新必须等本次 Sync 返回；UPDATE 会覆盖 pending 更新（每 Pod 只保留最新期望）。
**错误处理：** podCache 错误写 FailedSync 事件并返回，由重试机制再次唤醒。
**relistSuspender 协作：** 默认（SyncPod）分支进入前 `SuspendRelist(podUID)`（pod_workers.go :1342，`podWorkerLoop+76`），Sync 返回后 `ResumeRelist(podUID)`（:1356，`podWorkerLoop+90`）；若 relist 被挂起且有后续未观察变更，则 `RequestPodRelist(logger, podUID)`（:1362）触发一次针对该 Pod 的低延迟 relist——同步自身引起的运行时变化只被观察一次，而非每个中间运行时事件一次。接口定义在 `podRelistSuspender`（pod_workers.go:271-275）。

### 函数：(*GenericPLEG).Start / Relist
**源码：** `pkg/kubelet/pleg/generic.go#(*GenericPLEG).Start+0`（定义行 :165）/ `#(*GenericPLEG).Relist+0`（定义行 :296）
**职责：** Start 周期（RelistPeriod）调 Relist；Relist 列出运行时全部 Pod/容器，与上次快照对比，向 channel 发 PodLifecycleEvent 并更新 podCache。
**单 Pod relist 队列：** 在周期全量 Relist 之外，GenericPLEG 引入针对单个 Pod 的 relist 队列（`workerLoopIteration` :194 起消费 `requestPodRelist` 通道），供 `podRelistSuspender.RequestPodRelist` 触发低延迟局部刷新；EventedPLEG 也经此请求单 Pod relist。
**消费方：** syncLoopIteration 的 plegCh。
**健康检查：** `Healthy()` 依据 Relist 是否在阈值内完成，作为 node health 的组成部分。

#### PLEG 事件生成与 reconcile 深度拆解

PLEG 的核心是「两次快照对比 → 生成事件 → 更新缓存 → 投递」。全流程在 `pkg/kubelet/pleg/generic.go`。

**Relist 主流程（`(*GenericPLEG).Relist` 偏移：+0，定义行 :296）：**
1. `relistLock` 加锁（+0 ～ +2），记录上次 relist 间隔到 `PLEGRelistInterval` 指标（+6 ～ +8）。
2. **`g.runtime.GetPods(ctx, true)`**（偏移：+19）：向 CRI 拉取运行时全部 Pod（含已退出容器）；失败仅记日志并**放弃本轮**（不更新 relistTime，下轮重试）。
3. `updateRelistTime(timestamp)`（+24）→ `updateRunningPodAndContainerMetrics`（+28）刷新 running Pod/容器计数指标。
4. `podRecords.setCurrent(pods)`（+30）：把本轮快照存为 current，old 为上一轮。
5. 遍历 `g.podRecords` 逐 Pod 调 **`reconcilePodRecord`**（+33）。
6. 全部 Pod 更新完才 `cache.UpdateTime(timestamp)`（+38，注释强调顺序）。

**单 Pod 对账（`reconcilePodRecord` 偏移：+0，定义行 :339）：**
1. `getOld`/`getCurrent` 取新旧 Pod 记录，`getContainersFromPods`（定义 :468）合并两版全部容器（定义 :468）。
2. 逐容器 **`computeEvents`**（定义 :494）：`getContainerState`（:618）分别取 old/new 容器状态 → **`generateEvents`**（:256）按状态迁移产出事件。
3. 无事件且无需重检（`podsToReinspect` 未标记）→ 直接返回（+12 ～ +15）。
4. **`deferIfSuspended`**（+21，定义 :596）：该 Pod 正被 SyncPod 同步（`SuspendRelist` 挂起）则本轮跳过、**故意不更新 podRecord**，使变更在下轮 relist 重新被检测（配合 relistSuspender，见 podWorkerLoop）。
5. **`updateCache`**（+34，定义 :531）：检查 Pod 取最新 PodStatus 写入 podCache；失败则把 Pod 存入 `podsToReinspect`（+41），**同样不更新 podRecord**，下轮重试。
6. 无事件但完成全量重检 → 补发一个 `PodSync` 事件（+46 ～ +48），确保重检后仍触发一次 PodSync。
7. `podRecords.update(pid)`（+51）提交本轮变更；逐事件投递。

**事件生成规则（`generateEvents` 偏移：+0，定义行 :256）：** 状态未变返回 nil；按 newState 迁移——

| newState | 产出事件 |
|---|---|
| `ContainerRunning` | `ContainerStarted` |
| `ContainerExited` | `ContainerDied` |
| `ContainerUnknown` | `ContainerChanged` |
| `ContainerNonExistent`（旧态 Exited） | `ContainerRemoved` |
| `ContainerNonExistent`（其它旧态） | `ContainerDied` + `ContainerRemoved` |
| 其它 | panic（不可达） |

**投递语义（reconcilePodRecord +52 ～ +72）：**
- `ContainerChanged` 事件被**过滤不投递**（+55 ～ +57，不稳定、无消费者使用）。
- 事件经 `select` 写 `g.eventChannel`；**channel 满则丢弃并递增 `PLEGDiscardEvents`**（+60 ～ +62）——PLEG 是「最新状态对账」模型，丢事件不影响正确性，下轮 relist 会重新对账。
- `ContainerDied` 事件附带容器 exitCode 日志（+63 ～ +71，从 `status.ContainerStatuses` 建 exitCode 临时缓存）。

**设计要点（源码可证）：**
1. **对账模型而非事件流**：PLEG 不依赖 CRI 推送，靠周期全量 Relist 比对快照生成事件，因此**丢事件天然自愈**——下轮快照仍能检测出未处理的变更。
2. **podRecord 的「不更新即重试」**：更新缓存失败或 Pod 被挂起时，故意保留 old 记录，使变更在下轮 relist 再次被发现（reconcilePodRecord +34 ～ +43）。
3. **单 Pod 低延迟 relist**：除周期全量 Relist 外，`workerLoopIteration`（:194 起）消费 `requestPodRelist` 通道做局部刷新，供 `RequestRelist`（:606）/`RequestPodRelist` 触发，EventedPLEG 也复用此通道降低事件延迟。
4. **缓存时序**：`cache.UpdateTime` 必须在所有 Pod 更新后调用（Relist +36 ～ +38 注释），保证 `Healthy()` 判据反映完整一轮对账。

### 函数：(*manager).Start / syncPod
**源码：** `pkg/kubelet/status/status_manager.go#(*manager).Start+0`（定义行 :285）/ `#(*manager).syncPod+0`（定义行 :1352）
**职责：** Start 起后台循环批量消费待上报状态；syncPod 调 `statusutil.PatchPodStatus` 发 PATCH。
**调用链：** `syncPod` 经 `statusutil` 别名（import `statusutil "k8s.io/kubernetes/pkg/util/pod"`，status_manager.go:48）调用 `pkg/util/pod/pod.go#PatchPodStatus`（:34，调用点 :1384）。
**Start 与 syncPod 之间的新增：** 两者定义行间漂移差 96 行（Start +12 vs syncPod +108），为 `updateStatus`/`updateStatusInternal` 等批量上报路径扩展所致。
**冲突处理：** PATCH 返回冲突时重读 Pod 后重试；网络/服务端错误保留状态下轮再报。

## 4. 并发模型总览
```text
PodConfig 合并 goroutine（1）
  ↓ updates channel
syncLoop goroutine（1，主循环）
  ↓ UpdatePod（持锁写 status，发信号）
per-Pod podWorkerLoop goroutine（N，每 Pod 1 个）
  ↓ SyncPod（默认分支 SuspendRelist → SyncPod → ResumeRelist）
PLEG goroutine（1，周期 Relist）+ 单 Pod relist 队列（1 消费 goroutine）
  ↓ podCache + plegCh；RequestPodRelist 回注单 Pod relist
statusManager goroutine（1+批量）→ PATCH
EventedPLEG goroutine（可选，监听 CRI 事件 → 触发单 Pod relist）
```

## 5. 关键错误路径
| 错误 | 行为 |
|---|---|
| initializeModules 失败 | 写 KubeletSetupFailed 事件，os.Exit(1) |
| runtime 不健康 | syncLoop 退避，跳过同步 |
| 本地准入失败 | rejectPod，不交给 PodWorker |
| podCache 取状态失败 | FailedSync 事件，重试该 Pod |
| status PATCH 失败/冲突 | 保留待上报，重读或下轮重试 |

## 6. 设计意图与验证

**设计意图：**
1. **单点收敛**：所有期望态→实际态的驱动都收敛到 `podWorkerLoop` 的 `SyncPod`，kubelet 没有旁路；即使 mirror/静态 Pod 也走同一 worker。
2. **每 Pod 串行**：一把全局 `podLock` 只保护状态表；真正对容器/卷/探针的操作按 Pod 隔离在独立 goroutine，串行执行避免同一 Pod 内并发竞态。
3. **观察-收敛解耦**：PLEG 只负责「观察运行时并产出事件」，不直接改状态；syncLoop 是唯一的事件消费者。EventedPLEG 与 relistSuspender 都是「降低观察延迟/减少重复观察」的优化，不改变收敛语义。
4. **状态回写收敛**：statusManager 把最新状态以 PATCH 批量回写，冲突/失败保留待重试，避免状态丢失。

**验证要点（代码基准 master@`8893923`，工作区干净）：**
- 函数声明行号全部按证据索引核对：NewMainKubelet :481、Run :1988、syncLoop :2814、syncLoopIteration :2889、HandlePodAdditions :3047、UpdatePod :784、podWorkerLoop :1266、GenericPLEG.Start :165/Relist :296、manager.Start :285/syncPod :1352、PatchPodStatus :34、PodConfig.Channel :75/Updates :102。
- 相对偏移按证据核对（见各函数执行流程中的 `+N` 值）；与旧版唯一显著偏差为 `manager.syncPod` 漂移 +108（旧 :1244），由 Start 与 syncPod 间新增约 96 行批量上报路径所致。
- 新增观察：`podWorkers.relistSuspender`（`SuspendRelist`/`ResumeRelist`/`RequestPodRelist`，接口 pod_workers.go:271-275；挂起 :1342、恢复 :1356）；PLEG 单 Pod relist 队列；`manager.syncPod` 经 `statusutil` 别名调 `PatchPodStatus`。
- 确认项：kubelet 无 `dispatchWork`（全仓库 grep 0 命中）；`EventedPLEG` 存在（evented.go:43、`NewEventedPLEG` :52、`Start` :63）。

下一篇展开 SyncPod 内部到 CRI 的执行细节。

[上一篇](08-controller副本控制.md) · [总目录](README.md) · [下一篇](10-kubelet-CRI运行时.md)
