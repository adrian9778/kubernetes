# Kubelet 启动、依赖装配与总控制环

## 1. 入口链

入口是 [`cmd/kubelet/kubelet.go`](../../../cmd/kubelet/kubelet.go)，真正的命令构造在 [`cmd/kubelet/app/server.go`](../../../cmd/kubelet/app/server.go)。主链如下：

```text
main
  -> app.NewKubeletCommand(ctx)
  -> Run(ctx, KubeletServer, Dependencies, featureGate)
  -> run(...)
  -> RunKubelet(...)
  -> createAndInitKubelet(...)
  -> kubelet.NewMainKubelet(...)
  -> startKubelet(...)
       -> go Kubelet.Run(ctx, podCfg.Updates())
       -> 启动只读/安全 HTTP 服务
```

`NewKubeletCommand` 负责 flag、配置文件和 drop-in 配置的优先级。`mergeKubeletConfigurations` 合并 drop-in；`kubeletConfigFlagPrecedence` 再让命令行覆盖配置。理解这里的关键不是背 flag，而是记住最终传入 `NewMainKubelet` 的已经是内部版本 `KubeletConfiguration`。

`run` 做平台检查、feature gate、日志、metrics、client、cAdvisor、容器运行时端点等前置工作。`buildKubeletClientConfig` 处理三种证书来源：现有 kubeconfig、bootstrap kubeconfig、客户端证书轮转。轮转开启时，certificate manager 在后台申请 CSR，transport 会动态取当前证书。`InitializeTLS` 则负责 kubelet 服务端证书；未配置证书时可生成自签名证书。客户端身份和服务端身份是两条独立链，不要混为一谈。

Linux 的 [`server_linux.go`](../../../cmd/kubelet/app/server_linux.go) 还可观察 lockfile contention；Windows 的 [`server_windows.go`](../../../cmd/kubelet/app/server_windows.go) 做权限检查。

## 2. `NewMainKubelet` 是对象图装配器

[`pkg/kubelet/kubelet.go`](../../../pkg/kubelet/kubelet.go) 中 `NewMainKubelet` 创建 `Kubelet`，并把大量子系统通过接口连接起来。重要成员按职责分组如下：

| 领域 | 关键成员 | 状态性质 |
|---|---|---|
| Pod 期望 | `podManager`、`podConfig` | 来自 apiserver、静态文件等 source |
| Pod 串行执行 | `podWorkers`、`workQueue` | 每个 UID 一个 worker 状态机 |
| 运行时现实 | `containerRuntime`、`runtimeCache`、`pleg` | CRI 查询和事件观察 |
| API 状态 | `statusManager` | 内存版本缓存，异步 Patch API |
| 健康 | `probeManager`、三个 results manager | 每容器每探针 worker |
| 存储 | `volumeManager` | DSW、ASW、异步 operation executor |
| 资源 | `containerManager`、`allocationManager` | cgroup、CPU/内存/设备/DRA checkpoint |
| 压力 | `evictionManager` | 阈值和 node condition 的时间状态 |
| 节点 | `nodeLeaseController`、node status setters | lease 高频心跳、status 低频完整上报 |

`NewMainKubelet` 创建容量为 1000 的 PLEG 事件 channel，并创建 liveness、readiness、startup 三个 results manager。它还把 `SyncPod`、`SyncTerminatingPod`、`SyncTerminatedPod` 注入 `podWorkers`。这是一种重要解耦：worker 只管理生命周期和串行性，真正动作仍由 Kubelet 执行。

## 3. `Kubelet.Run` 启动哪些 goroutine

`Kubelet.Run` 先初始化运行时依赖模块，再启动长期循环。主要并发单元包括：

- `statusManager.Start`：串行向 apiserver 同步 PodStatus。
- `probeManager` 创建的每探针 worker：按周期执行 exec/http/tcp/grpc 探测。
- `volumeManager.Run`：至少包含 DSW populator 和 reconciler 两个循环。
- `evictionManager.Start`：周期采样压力，Linux 内存阈值还可由 cgroup notifier 触发。
- `updateRuntimeUp`：每 5 秒检查 CRI RuntimeReady、NetworkReady、runtime handlers/features。
- `syncNodeStatus`：带 jitter 周期更新完整 NodeStatus。
- node lease：更轻量、更高频地证明节点仍存活。
- PLEG：generic relist 或 evented CRI event 流。
- 最终前台控制环 `syncLoop`。

所有循环共享 `ctx` 或 stop channel 才能结束，但部分历史循环仍使用 `wait.NeverStop`。因此“进程收到退出信号”不代表每个后台任务都逐一 join；真正的节点优雅关闭还依赖 node shutdown manager、Pod 终止和卷卸载路径。

## 4. `syncLoop` 与 `syncLoopIteration`

`syncLoop` 创建同步 ticker、housekeeping ticker，取得 `pleg.Watch()`，反复调用 `syncLoopIteration`。后者是事件多路复用器：

| 输入 | 动作 |
|---|---|
| `configCh` | 按 ADD/UPDATE/REMOVE/RECONCILE/DELETE 分派到 `HandlePod*` |
| `plegCh` | 让 runtime cache 失效，并把对应 Pod 送入 worker |
| `syncCh` | 对 work queue 中到期 Pod 做周期 resync |
| `housekeepingCh` | 清理孤儿 Pod、状态、目录、cgroup、probe worker |
| liveness update | 失败时触发对应 Pod sync，最终由 runtime 决定重启 |
| readiness update | 直接更新 container readiness，并触发状态同步 |
| startup update | 更新 started；失败可推动重启 |

`HandlePodAdditions/Updates/Removes/Reconcile/Syncs` 不应直接并发修改运行时。它们更新 pod manager，执行 admission 或分派 `podWorkers.UpdatePod`。这样同一个 UID 的变更不会并发执行。

## 5. `SyncPod` 的阶段顺序

`Kubelet.SyncPod` 是“运行态同步”入口，大体顺序是：生成 API PodStatus；写 status cache；检查网络和 runtime 错误；创建 Pod 数据目录；等待卷 attach/mount；获取 pull secrets；创建/更新 cgroup 与资源分配；注册 probes；调用 `containerRuntime.SyncPod`；记录结果和 backoff。

这里的顺序就是重建时的约束：卷没准备好不能启动依赖挂载的容器；资源准入失败不能创建容器；status 必须在多处失败时仍能向用户解释原因；所有失败必须允许下一次 sync 重试。

## 6. 运行时健康状态

[`pkg/kubelet/runtime.go`](../../../pkg/kubelet/runtime.go) 的 `runtimeState` 由 mutex 保护，保存最后一次 runtime sync、network/storage/runtime error、PodCIDR、runtime handlers 和 features。`runtimeErrors()` 不只看最后一次 RPC 是否成功，也检查距离上次成功同步是否超过阈值，并运行额外 health checks。

这避免了“某次状态查询成功就宣布节点健康”的错误。重建时应保存 `lastSuccessfulSync`，并区分 runtime、network、storage 三类错误，因为 NodeCondition 和 Pod 启动决策对它们的处理不同。

