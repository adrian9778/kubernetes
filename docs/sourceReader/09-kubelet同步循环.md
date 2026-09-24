[上一篇](08-controller副本控制.md) · [总目录](README.md) · [下一篇](10-kubelet-CRI运行时.md)

# kubelet 同步循环
> **层级**：第四层——函数级源码解析。
> **范围**：Kubelet 启动序列、PodConfig 多源合并、syncLoop 主循环与事件分派、podWorkers 串行化、PLEG、statusManager 上报。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/kubelet`（main）、`cmd/kubelet/app`（NewKubeletCommand、Run、run、createAndInitKubelet、startKubelet） |
| 核心 | `pkg/kubelet/kubelet.go`（Kubelet、NewMainKubelet、Run、syncLoop）、`pkg/kubelet/config`（PodConfig 三源）、`pkg/kubelet/pod_workers.go` |
| 状态/事件 | `pkg/kubelet/pleg`（GenericPLEG/EventedPLEG）、`pkg/kubelet/status`（manager）、`pkg/kubelet/pod`（podManager） |
| 依赖 | `[staging] client-go`（reflector、cache）、`[staging] cri-client/cri-api` |
| 数据流 | 三源 PodUpdate → PodConfig 合并 → syncLoop select → podWorkers（每 Pod 串行）→ SyncPod；PLEG → 事件/缓存；statusManager → apiserver |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Kubelet` | `pkg/kubelet/kubelet.go` | 顶层协调者：持有 runtime、pleg、podWorkers、statusManager、podManager、allocationManager、volumeManager |
| `SyncHandler` | 同文件 | syncLoop 回调接口：HandlePodAdditions/Updates/Removes/Reconcile/Syncs/Cleanups |
| `PodConfig` | `pkg/kubelet/config/config.go` | 多源 mux：各源写入 podStorage，Merge 后产出 PodUpdate |
| `podWorkers` | `pkg/kubelet/pod_workers.go` | per-Pod worker 与终止状态机，持有 podSyncStatuses、podCache、podSyncer |
| `podSyncStatus` | 同文件 | 单个 Pod 的同步状态（syncedAt/terminatingAt/terminatedAt/finished） |
| `GenericPLEG` | `pkg/kubelet/pleg/generic.go` | 周期 Relist 比对运行时现状，生成 PodLifecycleEvent |
| `manager`（status） | `pkg/kubelet/status/status_manager.go` | PodStatus 缓存与批量 patch 上报 |

## 3. 函数级

### 函数：NewMainKubelet
**源码：** `pkg/kubelet/kubelet.go:469`
**职责：** 构造 Kubelet 及其全部子组件。
**关键动作：**
1. 构造 `config.NewPodConfig(...)`（config.go:61），随后向 `cfg.Channel(ctx, source)` 注册三源：
   - apiserver：`pkg/kubelet/config/apiserver.go#NewSourceApiserver`（:36，ListWatch `spec.nodeName=<本节点>` + Reflector）
   - file：静态 Pod 文件（fsnotify）
   - http：HTTP manifest 周期拉取
2. 建 podManager、containerRuntime（CRI）、PLEG、podWorkers（`newPodWorkers(klet,...)`，podSyncer 即 Kubelet 本体）、statusManager、volumeManager、allocationManager
**后置条件：** 返回可 Run 的 Kubelet；此时各源尚未开始推送。

### 函数：(*PodConfig).Channel / Updates
**源码：** `pkg/kubelet/config/config.go:75 / :102`
**职责：** Channel 按 source 名建输入通道并注册到 podStorage；Updates 返回合并后的 PodUpdate 输出通道。
**合并语义：** podStorage.Merge 把各源 Pod 按 UID 归并，产出 ADD/UPDATE/REMOVE/RECONCILE 的 PodUpdate（含 Source 标记）。

### 函数：(*Kubelet).Run
**源码：** `pkg/kubelet/kubelet.go:1898`
**职责：** 启动全部内部组件，最后进入 syncLoop。
**执行流程：**
1. 准备日志查询 server（+2 ～ +35）
2. `initializeModules`（+41 ～ +44；失败写事件并 os.Exit(1)）
3. `allocationManager.Run`（+51 ～ +53）、`go volumeManager.Run`（+56）
4. 有 kubeClient 时启动：node status 周期上报（+68 ～ +73）、fastStatusUpdateOnce（+77）、nodeLeaseController（+81）、fastStaticPodsRegistration（+88）
5. `go updateRuntimeUp` 每 5s（+90）
6. `statusManager.Start`（+98）、`pleg.Start`（+105；可选 EventedPLEG :107 ～ +109）
7. healthChecker 装配（+111 ～ +113）
8. `kl.syncLoop(ctx, updates, kl)` 阻塞（+115）
**状态变化：** 从构造态进入运行态；syncLoop 是最后且永不返回（直至节点关闭）的主循环。

### 函数：(*Kubelet).syncLoop
**源码：** `pkg/kubelet/kubelet.go:2684`
**职责：** 主循环外壳：定时器、runtime 健康检查、退避。
**执行流程：**
1. 三个时间/事件源：syncTicker（1s，+6）、housekeepingTicker（housekeepingPeriod，+9）、`pleg.Watch()` 事件通道（+12）
2. resolv.conf 检查（+17 ～ +20）
3. 循环：runtime 有错误时指数退避（base 100ms、factor 2、max 5s，+26 ～ +33）；健康则重置退避（+36）
4. 更新 syncLoopMonitor 时间戳，调 `syncLoopIteration(...)`（+40）；返回 false 退出（+41）
**设计意图：** runtime 不健康时跳过 Pod 同步并退避，避免对故障运行时高频重试。

### 函数：(*Kubelet).syncLoopIteration
**源码：** `pkg/kubelet/kubelet.go:2759`
**职责：** 多路 select，一次处理一个事件。
**事件源与动作：**
| 来源 | 动作 |
|---|---|
| configCh | 按 Op 分派：ADD→HandlePodAdditions（+18）、UPDATE→Updates、REMOVE→Removes、RECONCILE→Reconcile、DELETE→Updates（优雅删除视同更新）；末尾 AddSource 标记 source 已上报 |
| plegCh | isSyncPodWorthy → HandlePodSyncs（+40 ～ +48）；ContainerDied → cleanUpContainersInPod（+50 ～ +53） |
| syncCh | getPodsToSync → HandlePodSyncs（+55 ～ +62） |
| liveness/readiness/startup | 失败/就绪 → 探针同步与 readiness/status 写入（+64 ～ +82） |
| containerManager | 设备分配变化 → HandlePodSyncs（+84 ～ end） |
**返回值：** configCh 关闭时 false（主循环退出）。
**关键约束（注释 :2740 ～ +2748）：** select 分支随机序，事件处理无固定优先级。

### 函数：(*Kubelet).HandlePodAdditions
**源码：** `pkg/kubelet/kubelet.go:2896`
**职责：** 新 Pod 登记、本地准入、下发 PodWorker。
**执行流程：**
1. 按创建时间排序（+3）
2. 每个 Pod `podManager.AddPod`（+10，期望状态真相源）
3. mirror pod 走 Update 路径（+15 ～ +24）
4. 非终止 Pod：`allocationManager.AddPod` 本地准入（+37），失败 `rejectPod` 并跳过（+38 ～ +42）
5. 通过后 `podCertificateManager.TrackPod`（+45）；（feature gate）in-place resize 检查（+47 ～ +55）
6. `podWorkers.UpdatePod(UpdatePodOptions{UpdateType: SyncPodCreate})`（+60 ～ +65）

### 函数：(*podWorkers).UpdatePod
**源码：** `pkg/kubelet/pod_workers.go:761`
**职责：** 接收一次 Pod 更新，维护 podSyncStatus 并唤醒/创建该 Pod 的 worker。
**执行流程：**
1. 解析 uid/ns/name；RunningPod 孤儿场景特殊处理（+8 ～ +25）
2. 加 `podLock`（+28 ～ +29）
3. 查 podSyncStatuses：首见建 status（firstTime，+34 ～ +42）；终态 + kubelet 重启场景从 podCache 判定后补 terminated 状态（+43 ～ +end 段）
4. 更新 status 后向 per-Pod `podUpdates` channel 发信号；首见时 `go podWorkerLoop(...)`
**并发：** 一把全局 podLock 保护 status map；实际 Pod 操作在各 worker goroutine。

### 函数：(*podWorkers).podWorkerLoop
**源码：** `pkg/kubelet/pod_workers.go:1245`
**职责：** 单个 Pod 的串行执行循环。
**执行流程：**
```text
podWorkerLoop (+0)
├── for range podUpdates:
│   ├── startPodSync 取本次更新与启动门控 (+3)
│   ├── canEverStart=false → return（Pod 已终止）(+14 ～ +16)
│   ├── canStart=false → 等待下次更新 (+18 ～ +20)
│   ├── podCache.GetNewerThan 取最新运行时状态 (+43 ～ +46，最多约 2s 延迟)
│   └── 按 WorkType 分派 (+56 ～ end):
│        ├── TerminatedPod → SyncTerminatedPod
│        ├── TerminatingPod → SyncTerminatingPod/RuntimePod（带 gracePeriod）
│        └── 默认 → podSyncer.SyncPod（即 Kubelet.SyncPod）
└── （错误处理/退避在 loop 外层）
```
**串行化语义：** 同一 Pod 的下一次更新必须等本次 Sync 返回；UPDATE 会覆盖 pending 更新（每 Pod 只保留最新期望）。
**错误处理：** podCache 错误写 FailedSync 事件并返回，由重试机制再次唤醒。

### 函数：(*GenericPLEG).Start / Relist
**源码：** `pkg/kubelet/pleg/generic.go:158 / :289`
**职责：** Start 周期（RelistPeriod）调 Relist；Relist 列出运行时全部 Pod/容器，与上次快照对比，向 channel 发 PodLifecycleEvent 并更新 podCache。
**消费方：** syncLoopIteration 的 plegCh。
**健康检查：** `Healthy()` 依据 Relist 是否在阈值内完成，作为 node health 的组成部分。

### 函数：(*manager).Start / syncPod
**源码：** `pkg/kubelet/status/status_manager.go:273 / :1244`
**职责：** Start 起后台循环批量消费待上报状态；syncPod 调 `pkg/util/pod/pod.go#PatchPodStatus`（:34）发 PATCH。
**冲突处理：** PATCH 返回冲突时重读 Pod 后重试；网络/服务端错误保留状态下轮再报。

## 4. 并发模型总览
```text
PodConfig 合并 goroutine（1）
  ↓ updates channel
syncLoop goroutine（1，主循环）
  ↓ UpdatePod（持锁写 status，发信号）
per-Pod podWorkerLoop goroutine（N，每 Pod 1 个）
  ↓
PLEG goroutine（1，Relist）→ podCache + plegCh
statusManager goroutine（1+批量）→ PATCH
```

## 5. 关键错误路径
| 错误 | 行为 |
|---|---|
| initializeModules 失败 | 写 KubeletSetupFailed 事件，os.Exit(1) |
| runtime 不健康 | syncLoop 退避，跳过同步 |
| 本地准入失败 | rejectPod，不交给 PodWorker |
| podCache 取状态失败 | FailedSync 事件，重试该 Pod |
| status PATCH 失败/冲突 | 保留待上报，重读或下轮重试 |

下一篇展开 SyncPod 内部到 CRI 的执行细节。

[上一篇](08-controller副本控制.md) · [总目录](README.md) · [下一篇](10-kubelet-CRI运行时.md)
