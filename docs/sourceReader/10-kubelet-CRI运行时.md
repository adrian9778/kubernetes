[上一篇](09-kubelet同步循环.md) · [总目录](README.md) · [下一篇](11-informer与workqueue机制.md)

# kubelet CRI 运行时
> **层级**：第四层——函数级源码解析。
> **范围**：kubeGenericRuntimeManager.SyncPod 的 sandbox/container 执行、镜像拉取、CRI gRPC 客户端落点。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 翻译层 | `pkg/kubelet/kuberuntime`：manager（SyncPod/computePodActions）、sandbox、container |
| CRI 客户端 | `[staging] k8s.io/cri-client/pkg/remote_runtime.go`（grpc 拨号与方法）、`[staging] k8s.io/cri-api`（接口与生成 stub） |
| 对上接口 | `pkg/kubelet/container/runtime.go#Runtime`（kubelet 侧抽象，SyncPod） |
| 数据流 | Pod + podStatus → podActions（差集）→ PodSandboxConfig/ContainerConfig → CRI 请求 → 运行时 → 容器 ID/状态 |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `kubeGenericRuntimeManager` | `pkg/kubelet/kuberuntime/kuberuntime_manager.go` | 实现 kubecontainer.Runtime：把 Pod 同步翻译为 CRI sandbox/container 调用，持有 runtimeService、imageService、imagePuller |
| `podActions` | 同文件 | computePodActions 的结果：CreateSandbox、SandboxID、KillPod、ContainersToKill、各容器 next 动作 |
| `startSpec` | `kuberuntime_container.go` | 容器/init 容器规格的统一包装（container 字段） |
| `remoteRuntimeService` | `[staging] cri-client/pkg/remote_runtime.go` | internalapi.RuntimeService 实现，持有 grpc RuntimeServiceClient |

## 3. 函数级

### 函数：(*kubeGenericRuntimeManager).SyncPod
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_manager.go:1576`
**职责：** 单次 Pod 运行时同步的事务脚本，返回 PodSyncResult。
**参数：** `ctx`、`pod`、`podStatus *kubecontainer.PodStatus`、`pullSecrets`、`backOff`、`restartAllContainers bool`。
**返回值：** `kubecontainer.PodSyncResult`（聚合每步结果与错误）。
**执行流程：**
```text
SyncPod (+0)
├── Step 1 computePodActions 求差集 (+3)
├── CreateSandbox 分支：新 Pod 日志/SandboxChanged 事件 (+5 ～ +15)
├── Step 2 KillPod（sandbox 变化或容器全死）(+18 ～ +35)
│    killPodWithSyncResult；成功后 purgeInitContainers
├── Step 3（否则）杀 ContainersToKill (+37 ～ +46)
│    （feature gate）ContainersToReset 移除/强杀 (+48 ～ +74)
├── pruneInitContainersBeforeStart (+78)
├── 取 podIPs（默认来自 podStatus）(+86 ～ +94)
├── Step 4 CreateSandbox：
│    sysctl 分隔符转换 (+133)
│    PrepareDynamicResources (+138 ～ +148)
│    createPodSandbox (+150)
│    PodSandboxStatus 校验 (+176)
└── Step 5 对每个保留/新建容器 startContainer（后续段）
```
**错误处理：** 每步失败写入对应 SyncResult 后立即返回；Pod 在创建期间被删除时 sandbox 失败按非错误处理（:1722 ～ +1734）。
**设计意图（注释 :1650 ～ +1652）：** prune init 容器是相对 GC 的提前优化。

### 函数：(*kubeGenericRuntimeManager).computePodActions
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_manager.go:1272`
**职责：** 比较 Pod 期望与 podStatus：决定是否建 sandbox（新 Pod/sandbox 网络配置变化）、杀哪些容器、每个容器是 create 还是 skip/重启。
**返回值：** `podActions`。
**关键分支：** 无现存 sandbox → CreateSandbox=true、Attempt 递增；容器配置 hash 变化 → 旧容器进 ContainersToKill 并新建。

### 函数：(*kubeGenericRuntimeManager).createPodSandbox
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_sandbox.go:42`
**职责：** 生成配置、建日志目录、解析 runtime handler、调 CRI 建 sandbox。
**执行流程：**
1. `generatePodSandboxConfig`（+2）：Pod → PodSandboxConfig（hostname、dns、log 目录、labels、cgroup、资源）
2. `MkdirAll(LogDirectory)`（+10 ～ +15）
3. runtimeClass 存在时 `LookupRuntimeHandler`（+18 ～ +28）
4. `m.runtimeService.RunPodSandbox(ctx, config, runtimeHandler)`（+29）
**返回值：** sandboxID / message / error。

### 函数：(*kubeGenericRuntimeManager).startContainer
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_container.go:200`
**职责：** 单容器的拉镜像→创建→启动。
**参数：** podSandboxID、podSandboxConfig、`spec *startSpec`、pod、podStatus、pullSecrets、podIP/podIPs、imageVolumes。
**返回值：** containerID string、error。
**执行流程：**
```text
startContainer (+0)
├── 1. getPodRuntimeHandler (+5 ～ +7)
├── 2. GenerateContainerRef (+10 ～ +13)
├── 3. imagePuller.EnsureImageExists（拉镜像/查本地）(+15 ～ +21)
├── 4. 计算 restartCount（现存状态+1；节点重启场景按日志目录回推）(+24 ～ +45)
├── 5. spec.getTargetID（依赖容器目标）(+47 ～ +52)
├── 6. generateContainerConfig（命令、镜像 ref、挂载、env、设备、资源）(+55 ～ +63)
├── 7. setActuatedContainerResources (+66 ～ +70)
├── 8. internalLifecycle.PreCreateContainer 钩子 (+71 ～ +76)
├── 9. runtimeService.CreateContainer (+78 ～ +83)
├── 10. internalLifecycle.PreStartContainer (+84 ～ +89)
└── 11. runtimeService.StartContainer（函数后续行）
```
**错误处理：** 每类错误有哨兵返回值（ErrCreateContainerConfig/ErrCreateContainer/ErrPreCreateHook…）并记录具体容器事件；镜像失败返回 msg 供上层生成 FailedToCreate 事件。
**副作用：** 容器事件、镜像缓存、defer cleanupAction（临时配置清理）。

### 函数：NewRemoteRuntimeService
**源码：** `[staging] cri-client/pkg/remote_runtime.go:232`
**职责：** 建立到运行时 endpoint 的 gRPC 连接（grpc.NewClient），构造 `runtimeapi.NewRuntimeServiceClient(conn)`，包装为 remoteRuntimeService。
**被调用：** kubelet 启动时经 `NewRemoteRuntimeServiceBuilder`（:112），外层再包 metrics instrumentation。

### 函数：(*remoteRuntimeService).RunPodSandbox / CreateContainer / StartContainer
**源码：** `[staging] cri-client/pkg/remote_runtime.go:308 / :481 / :514`
**职责：** 参数 proto 化 → 调 grpc stub → 处理返回与超时/状态错误。
- RunPodSandbox 返回 sandboxID
- CreateContainer 返回 containerID
- StartContainer 无返回体
**错误处理：** gRPC 状态经 cri-client 统一转回 Go error（含 deadline/cancel 判定）。

## 4. CRI 调用关系总览
```text
Kubelet.SyncPod
 ↓
kubeGenericRuntimeManager.SyncPod
 ├── computePodActions
 ├── createPodSandbox
 │    └── remoteRuntimeService.RunPodSandbox → grpc → 运行时（CNI 配网在运行时侧）
 └── startContainer（每容器）
      ├── imagePuller.EnsureImageExists → ImageService gRPC
      ├── remoteRuntimeService.CreateContainer
      └── remoteRuntimeService.StartContainer
```

## 5. 关键边界与错误
| 场景 | 行为 |
|---|---|
| Pod 创建中被删除 | sandbox 错误静默返回 |
| 镜像拉取失败 | FailedToCreateContainer 事件，上层 SyncPod 失败重试 |
| runtimeClass handler 解析失败 | createPodSandbox 返回错误，不落 CRI |
| PreCreate/PreStart 钩子失败 | 带哨兵错误返回，不继续 CRI 调用 |
| sandbox status 查询失败 | 结果失败并跳过该 Pod，等待下轮同步 |

[上一篇](09-kubelet同步循环.md) · [总目录](README.md) · [下一篇](11-informer与workqueue机制.md)
