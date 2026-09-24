# podWorkers、PLEG、CRI 与 Pod 生命周期

## 1. 四层模型

```text
Pod source/API 对象
  -> podManager + podWorkers（期望与生命周期）
  -> Kubelet Sync*（节点业务编排）
  -> kubeGenericRuntimeManager（差异计算）
  -> CRI RuntimeService/ImageService（执行）
  <- PLEG/runtime cache（重新观察现实）
```

[`pkg/kubelet/container/runtime.go`](../../../pkg/kubelet/container/runtime.go) 定义 kubelet 内部 `Runtime` 接口和 `Pod`、`Container`、`PodStatus`、`Status`。这是 kubelet 与具体 CRI protobuf 客户端之间的防腐层。`SyncPod` 和 `KillPod` 是主写操作；`GetPods/GetPodStatus/Status` 是主观察操作。

## 2. `podWorkers` 的并发约束

[`pkg/kubelet/pod_workers.go`](../../../pkg/kubelet/pod_workers.go) 的核心保证是：不同 UID 可并发，同一 UID 串行；多个到达中的更新合并为一个 `pendingUpdate`。关键对象：

- `podWorkers`：全局 map、每 UID channel、work queue、backoff 参数。
- `podSyncStatus`：单 Pod 状态真相，包含 `working`、`pendingUpdate`、`activeUpdate`、`terminatingAt`、`startedTerminating`、`terminatedAt`、`finished`、`evicted`、`deleted` 等。
- `UpdatePodOptions`：Create/Update/Sync/Kill 等工作及 mirror/running pod 信息。
- `PodWorkerState`：`SyncPod`、`TerminatingPod`、`TerminatedPod`。

状态机可简化为：

```text
SyncPod
  | deletion/eviction/terminal result
  v
TerminatingPod -- SyncTerminatingPod 成功 --> TerminatedPod
  ^                                           |
  | 更短 grace period                         | SyncTerminatedPod 成功
  +-------------------------------------------+--> Finished
```

状态只能向终止方向前进。进入 terminating 后，普通 spec update 被折叠为 kill；grace period 只能缩短不能延长。`calculateEffectiveGracePeriod` 依次考虑已有值、显式 override、Pod deletion grace、spec 默认值，最终至少为 1 秒。

`UpdatePod` 首次看到 UID 时创建一个 goroutine `podWorkerLoop`。channel 只承担“有工作”通知，真实最新数据保存在受锁保护的 status 中，因此突发更新不会无限堆积。worker 调 `startPodSync` 取走 pending update 并标记 working，动作结束后 `completeWork` 根据结果安排：成功按 resync interval；transient error 短 backoff；普通错误按 backoff；phase transition 立即继续。

静态 Pod 还有 fullname 仲裁：相同 namespace/name 只允许一个 UID 启动，旧 UID 必须先退出，防止 manifest 快速替换时两个实例并存。

## 3. 三个执行阶段

`SyncPod` 面向活跃 Pod，可多次执行。`SyncTerminatingPod` 停止 liveness/startup probes、执行 preStop 和容器终止、持续更新终止状态。`SyncTerminatedPod` 在运行时容器已经退出后做不可逆清理：等待卷卸载、清理资源分配、Pod 目录等，并完成最终状态。

`killPodNow` 把驱逐或显式 kill 包装为 `UpdatePod(Kill)`，然后等待 completion channel。等待超时大致为 grace period 的 1.5 倍并有最低保障；超时只说明 kubelet 到 runtime 的终止没有按期完成，worker 的收敛仍可能继续。

## 4. PLEG：现实变化通知器

[`pkg/kubelet/pleg/pleg.go`](../../../pkg/kubelet/pleg/pleg.go) 定义 `PodLifecycleEvent` 和 PLEG 接口。事件包括 ContainerStarted、ContainerDied、ContainerRemoved、PodSync、ConditionMet 等。PLEG 事件不是完整状态，只是“请重新观察”的提示，channel 丢失或合并不能破坏最终正确性。

[`generic.go`](../../../pkg/kubelet/pleg/generic.go) 的 GenericPLEG 周期 relist：

1. 调 CRI 列举全部 Pod/Container。
2. 与上次 podRecords 比较。
3. 生成 start/die/remove 等事件。
4. 对有变化 Pod 更新 runtime cache。
5. 将事件写入 kubelet 的 PLEG channel。

它天然可恢复：kubelet 重启后第一次 relist 会从运行时重建现实快照；某次事件消费失败，后续 relist 仍可再次暴露差异。

[`evented.go`](../../../pkg/kubelet/pleg/evented.go) 使用 CRI container event stream 降低轮询延迟和成本，但不能盲信事件流。连接失败、事件丢失或 runtime 不支持时仍需 relist/回退机制。EventedPLEG 的事件可直接生成 PodStatus，并更新 cache。

## 5. runtime cache

[`pkg/kubelet/container/runtime_cache.go`](../../../pkg/kubelet/container/runtime_cache.go) 缓存 `GetPods` 结果，按时间戳判断是否足够新。PLEG 事件会推动缓存刷新或失效。缓存的目的不是成为事实源，而是避免一次 sync 内重复昂贵 CRI RPC。

重建时必须遵守：缓存只保存可重新获取的数据；写动作完成后不能仅修改缓存宣称成功；状态不确定时宁可重新 List/Status。

## 6. `kubeGenericRuntimeManager.SyncPod`

[`pkg/kubelet/kuberuntime/kuberuntime_manager.go`](../../../pkg/kubelet/kuberuntime/kuberuntime_manager.go) 的 `computePodActions` 把 Pod spec 与 `kubecontainer.PodStatus` 比较，生成 `podActions`：是否 KillPod、是否重建 sandbox、要启动的 init/regular/ephemeral containers、要 kill/remove/reset 的容器、资源和 volume resize 动作。

执行顺序不是随意的：

1. 必要时 kill 整个 Pod 并重建 sandbox。
2. prune 已退出 init containers。
3. 创建 sandbox，网络 namespace 也在此建立。
4. 执行 Pod/container 资源 resize，并在失败时回滚。
5. 按 init container 语义启动 init/restartable init。
6. 启动普通与 ephemeral container。
7. 每个容器启动前生成挂载、环境、设备、security context，拉镜像，再调用 CRI Create/Start。

`doBackOff` 使用 Pod UID + container name 等构成稳定 key，并以最近退出时间驱动指数退避，产生用户熟悉的 CrashLoopBackOff。成功运行足够久后 backoff 可重置。

## 7. sandbox 与容器

[`kuberuntime_sandbox.go`](../../../pkg/kubelet/kuberuntime/kuberuntime_sandbox.go) 负责 `PodSandboxConfig`、DNS、端口、namespace、runtime handler 和 `RunPodSandbox`。sandbox 改变通常意味着 Pod IP 和网络命名空间改变，因此需要重启整个 Pod，而非只重启一个业务容器。

[`kuberuntime_container.go`](../../../pkg/kubelet/kuberuntime/kuberuntime_container.go) 负责拉镜像、CreateContainer、StartContainer、生命周期 hook、日志路径和 kill。终止顺序由 [`kuberuntime_termination_order.go`](../../../pkg/kubelet/kuberuntime/kuberuntime_termination_order.go) 协调，restartable init sidecar 与普通容器存在依赖顺序。

容器 kill 应是幂等的：容器已不存在可视为完成；preStop 失败不能阻止最终发送 StopContainer；剩余 grace 要扣除 hook 已消耗时间；超过期限由 runtime 负责强制停止。

## 8. 重启恢复

kubelet 进程状态丢失后，恢复依赖四类外部事实：CRI 中仍运行的 sandbox/container；磁盘上的 Pod/volume 目录；CPU/内存/设备/DRA checkpoint；apiserver 中期望 Pod。启动后先重建 cache 和 manager state，再由 `SyncKnownPods` 识别 orphan runtime pods，最终将“不再期望但仍运行”的对象送入终止状态机。

