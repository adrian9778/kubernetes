[上一篇](09-kubelet同步循环.md) · [总目录](README.md) · [下一篇](11-informer与workqueue机制.md)

# kubelet CRI 运行时
> **层级**：第四层——函数级源码解析。
> **范围**：kubeGenericRuntimeManager.SyncPod 的 sandbox/container 执行、镜像拉取、CRI gRPC 客户端落点。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区源码干净），Go 1.27.4。

## 1. 生产视角：CRI 执行对外行为与可见结果

本节描述 kubelet 每轮 Pod 同步时，通过 CRI 对外呈现的可观察行为与结果。全部行为以当前代码核实（源码走读，非运行时实测）；与代码推断不同处已单独标注。

**触发入口**：kubelet 同步循环每轮对每个 Pod 调用 `kubeGenericRuntimeManager.SyncPod`（`pkg/kubelet/kuberuntime/kuberuntime_manager.go#SyncPod+0`）。它是把「Pod 期望状态」翻译成「CRI 运行时调用序列」的事务脚本。

**对外可见结果（按发生顺序）**：

1. **决策（不可见但决定行为）**：`computePodActions`（#computePodActions+0）比较 Pod 期望与 `podStatus` 差集，产出 `podActions`——是否需要新建 sandbox、杀哪些容器、各容器是 create 还是 skip/重启。
2. **新建 sandbox（可见）**：当决策为 `CreateSandbox` 时，先 `createPodSandbox`（kuberuntime_sandbox.go#createPodSandbox+0）生成 `PodSandboxConfig`、建日志目录、解析 runtime handler，再经 `remoteRuntimeService.RunPodSandbox`（remote_runtime.go#RunPodSandbox+0）向运行时发 gRPC。运行时侧由此创建 Pod 沙箱并完成 CNI 配网。
3. **杀旧容器（可见）**：sandbox 变化或容器全死时 `KillPod` 批量终止；否则按 `ContainersToKill` 逐个终止（含 `ContainersToReset` 的移除/强杀分支）。
4. **sandbox 就绪回调（可见，新步骤）**：sandbox 创建成功后触发 `OnPodSandboxReady` 回调（SyncPod+233～+241），设置 `PodReadyToStartContainers=True` 条件。
5. **动态资源与校验（可见）**：`PrepareDynamicResources`（SyncPod+153～+162）准备动态资源，随后对 `PodSandboxStatus` 做校验（SyncPod+189～+203）。
6. **容器生命周期（可见）**：对每个保留/新建容器调用 `startContainer`（kuberuntime_container.go#startContainer+0）——拉镜像、生成配置、建容器、启容器，各步经 `runtimeService.CreateContainer`/`StartContainer`（remote_runtime.go#CreateContainer+0 / #StartContainer+0）发 gRPC。
7. **Pod 调整（可见，新步骤）**：`Resize` 步骤（SyncPod+252～+259）按 `InPlacePodVerticalScalingAllowed` 对 Pod 与运行中容器做垂直伸缩；`VolumesToUpsize/Downsize/UpdatePodResources` 触发 `doPodResizeAction`。
8. **临时容器（可见，新步骤）**：`Step 7` 启动临时容器（SyncPod 后续段），先于 init 容器启动，允许在 init 失败时仍运行临时容器。

**对上层（kubelet）的可见结果**：`SyncPod` 返回 `kubecontainer.PodSyncResult`，聚合每步结果与错误；某步失败即写入对应 `SyncResult` 并立即返回。Pod 在创建期间被删除时，sandbox 失败按非错误静默返回（SyncPod+173～+176）。

**对使用者的可见结果**：容器/镜像事件（如 `FailedToCreateContainer`）、容器 ID 与状态、Pod 就绪条件。镜像拉取失败通过事件与错误消息上报，由上层重试。

## 2. 模块级
| 项 | 内容 |
|---|---|
| 翻译层 | `pkg/kubelet/kuberuntime`：manager（SyncPod/computePodActions）、sandbox、container |
| CRI 客户端 | `[staging] k8s.io/cri-client/pkg/remote_runtime.go`（grpc 拨号与方法）、`[staging] k8s.io/cri-api`（接口与生成 stub） |
| 对上接口 | `pkg/kubelet/container/runtime.go`#Runtime（kubelet 侧抽象，SyncPod） |
| 数据流 | Pod + podStatus → podActions（差集）→ PodSandboxConfig/ContainerConfig → CRI 请求 → 运行时 → 容器 ID/状态 |

## 3. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `kubeGenericRuntimeManager` | `pkg/kubelet/kuberuntime/kuberuntime_manager.go#kubeGenericRuntimeManager+0` | 实现 kubecontainer.Runtime：把 Pod 同步翻译为 CRI sandbox/container 调用，持有 runtimeService、imageService、imagePuller |
| `podActions` | 同文件 #podActions+0 | computePodActions 的结果：CreateSandbox、SandboxID、KillPod、ContainersToKill、各容器 next 动作 |
| `startSpec` | `kuberuntime_container.go#startSpec+0` | 容器/init 容器规格的统一包装（container 字段） |
| `remoteRuntimeService` | `[staging] cri-client/pkg/remote_runtime.go#remoteRuntimeService+0` | internalapi.RuntimeService 实现，持有 grpc RuntimeServiceClient |

## 4. 函数级

### 函数：(*kubeGenericRuntimeManager).SyncPod
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_manager.go#SyncPod+0`
**职责：** 单次 Pod 运行时同步的事务脚本，返回 PodSyncResult。
**参数：** `ctx`、`pod`、`podStatus *kubecontainer.PodStatus`、`pullSecrets`、`backOff`、`restartAllContainers bool`。
**返回值：** `kubecontainer.PodSyncResult`（聚合每步结果与错误）。
**执行流程：**
```text
SyncPod (+0)
├── Step 1 computePodActions 求差集 (+14)
├── CreateSandbox 分支：新 Pod 日志/SandboxChanged 事件 (+16 ～ +26)
├── Step 2 KillPod（sandbox 变化或容器全死）(+29 ～ +45)
│    killPodWithSyncResult；成功后 purgeInitContainers
├── Step 3（否则）杀 ContainersToKill (+48 ～ +60)
│    （feature gate）ContainersToReset 移除/强杀 (+62 ～ +89)
├── pruneInitContainersBeforeStart (+95)
├── 取 podIPs（默认来自 podStatus）(+107 ～ +110)
├── Step 4 CreateSandbox：
│    sysctl 分隔符转换 (+150)
│    PrepareDynamicResources (+153 ～ +162)
│    createPodSandbox (+164)
│    Pod 删除时 sandbox 失败非错误 (+173 ～ +176)
│    PodSandboxStatus 校验 (+189 ～ +203)
├── Step 5 OnPodSandboxReady 回调 (+233 ～ +241)
├── Step 6 Resize（Pod 与运行中容器垂直伸缩）(+252 ～ +259)
├── startContainer（每容器，含 init：startWithInitState 内 +290）
└── Step 7 start ephemeral 容器（后续段）
```
**错误处理：** 每步失败写入对应 SyncResult 后立即返回；Pod 在创建期间被删除时 sandbox 失败按非错误处理（+173 ～ +176）。
**设计意图（注释 +92 ～ +94）：** prune init 容器是相对 GC 的提前优化。

### 函数：(*kubeGenericRuntimeManager).computePodActions
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_manager.go#computePodActions+0`
**职责：** 比较 Pod 期望与 podStatus：决定是否建 sandbox（新 Pod/sandbox 网络配置变化）、杀哪些容器、每个容器是 create 还是 skip/重启。
**返回值：** `podActions`。
**关键分支：**
- 无现存 sandbox → `CreateSandbox=true`、Attempt 递增；容器配置 hash 变化 → 旧容器进 `ContainersToKill` 并新建。
- `RestartAllContainersOnContainerExits` feature gate 开启且 `restartAllContainers` 为真（computePodActions+15～+28）：按逆序 kill 并移除容器（源容器最后移除），`ContainersToReset` 依次追加 target 容器、target init 容器、source 容器、source init 容器，再 `killRemovedContainers`，随后提前返回 `changes`。

### 函数：(*kubeGenericRuntimeManager).createPodSandbox
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_sandbox.go#createPodSandbox+0`
**职责：** 生成配置、建日志目录、解析 runtime handler、调 CRI 建 sandbox。
**执行流程：**
1. `generatePodSandboxConfig`（+2）：Pod → PodSandboxConfig（hostname、dns、log 目录、labels、cgroup、资源）
2. `MkdirAll(LogDirectory)`（+10 ～ +15）
3. runtimeClass 存在时 `LookupRuntimeHandler`（+19 ～ +27）
4. `m.runtimeService.RunPodSandbox(ctx, config, runtimeHandler)`（+29）
**返回值：** sandboxID / message / error。

### 函数：(*kubeGenericRuntimeManager).startContainer
**源码：** `pkg/kubelet/kuberuntime/kuberuntime_container.go#startContainer+0`
**职责：** 单容器的拉镜像→创建→启动。
**参数：** podSandboxID、podSandboxConfig、`spec *startSpec`、pod、podStatus、pullSecrets、podIP/podIPs、imageVolumes。
**返回值：** containerID string、error。
**执行流程：**
```text
startContainer (+0)
├── 1. getPodRuntimeHandler (+5 ～ +8)
├── 2. GenerateContainerRef (+10 ～ +13)
├── 3. ensureContainerSecurityProfile（安全配置）(+15)
├── 4. imagePuller.EnsureImageExists（拉镜像/查本地）(+20 ～ +25)
├── 5. 计算 restartCount（现存状态+1；节点重启场景按日志目录回推）(+29 ～ +50)
├── 6. spec.getTargetID（依赖容器目标）(+52 ～ +57)
├── 7. generateContainerConfig（命令、镜像 ref、挂载、env、设备、资源）(+59 ～ +67)
├── 8. setActuatedContainer (+70)
├── 9. internalLifecycle.PreCreateContainer 钩子 (+75 ～ +80)
├── 10. runtimeService.CreateContainer (+82 ～ +87)
├── 11. internalLifecycle.PreStartContainer (+88 ～ +93)
└── 12. runtimeService.StartContainer (+97)
```
**错误处理：** 每类错误有哨兵返回值（ErrCreateContainerConfig/ErrCreateContainer/ErrPreCreateHook…）并记录具体容器事件；镜像失败返回 msg 供上层生成 FailedToCreate 事件。
**副作用：** 容器事件、镜像缓存、defer cleanupAction（临时配置清理）。
**注意：** 第 8 步调用的是 `setActuatedContainer`（startContainer+70 调用点，独立方法定义 #setActuatedContainer+0），而非 `setActuatedContainerResources`——后者是独立方法（#setActuatedContainerResources+0），不在此调用链内。

### 函数：NewRemoteRuntimeService
**源码：** `[staging] cri-client/pkg/remote_runtime.go#NewRemoteRuntimeService+0`
**职责：** 建立到运行时 endpoint 的 gRPC 连接（grpc.NewClient），构造 `runtimeapi.NewRuntimeServiceClient(conn)`，包装为 remoteRuntimeService。
**Deprecated：** 本函数已标记 `Deprecated`（NewRemoteRuntimeService-3 ～ -1），推荐 `NewRemoteRuntimeServiceBuilder`（#NewRemoteRuntimeServiceBuilder+0）。
**被调用：** kubelet 启动时经 `NewRemoteRuntimeServiceBuilder`（kubelet.go#NewRemoteRuntimeServiceBuilder+0 调用点）构造，外层再包 metrics instrumentation（kubelet.go#NewInstrumentedRuntimeService+0 调用点 `cri.NewInstrumentedRuntimeService`）；gRPC stub 在 `validateServiceConnection` 内 `runtimeapi.NewRuntimeServiceClient(conn)`（#validateServiceConnection 内，remote_runtime.go 当前 :254）。

### 函数：(*remoteRuntimeService).RunPodSandbox / CreateContainer / StartContainer
**源码：** `[staging] cri-client/pkg/remote_runtime.go#RunPodSandbox+0 / #CreateContainer+0 / #StartContainer+0`
**职责：** 参数 proto 化 → 调 grpc stub → 处理返回与超时/状态错误。
- RunPodSandbox 返回 sandboxID
- CreateContainer 返回 containerID
- StartContainer 无返回体
**错误处理：** gRPC 状态经 cri-client 统一转回 Go error（含 deadline/cancel 判定）。

## 5. CRI 调用关系总览
```text
Kubelet.SyncPod
 ↓
kubeGenericRuntimeManager.SyncPod
 ├── computePodActions
 ├── createPodSandbox
 │    └── remoteRuntimeService.RunPodSandbox → grpc → 运行时（CNI 配网在运行时侧）
 ├── Step 5 OnPodSandboxReady 回调
 ├── Step 6 Resize（doPodResizeAction）
 └── startContainer（每容器）
      ├── imagePuller.EnsureImageExists → ImageService gRPC
      ├── ensureContainerSecurityProfile
      ├── remoteRuntimeService.CreateContainer
      └── remoteRuntimeService.StartContainer
```
**构建链：** `NewRemoteRuntimeServiceBuilder`（#NewRemoteRuntimeServiceBuilder+0）→ `Build` → kubelet.go 调用点（#NewRemoteRuntimeServiceBuilder 调用处）；metrics 包装 `cri.NewInstrumentedRuntimeService`（kubelet.go#NewInstrumentedRuntimeService 调用处）；gRPC stub 构造 `runtimeapi.NewRuntimeServiceClient(conn)`（remote_runtime.go#validateServiceConnection 内）。

## 5b 镜像拉取与镜像 GC 深度拆解

镜像不在 `kuberuntime_image.go`，而在 `pkg/kubelet/images/`（`image_manager.go` 拉取、`image_gc_manager.go` 回收）。startContainer 经 `imagePuller.EnsureImageExists` 调到前者。

### 镜像拉取：EnsureImageExists

**源码：** `pkg/kubelet/images/image_manager.go EnsureImageExists+0`（:181）
**职责：** 确保容器镜像在本节点存在；本地有且策略允许则复用，否则拉取。
**执行流程：**
1. `applyDefaultImageTag`（:435）：镜像无 tag/digest 时补默认 tag `:latest`；失败返回 `ErrInvalidImageName`。
2. 收集 Pod annotation 转 `kubecontainer.Annotation`，连同 `RuntimeHandler` 组成 `kubecontainer.ImageSpec`。
3. **`imagePullPrecheck`**（:120）：结合 `pullPolicy`（Always/Never/IfNotPresent）与本地是否存在镜像判定「是否需要拉取」；`imageNotPresentOnNeverPolicyError`（:173）处理 `Never` 策略下镜像缺失的错误。
4. `makeLookupPullCredentialsFunc`（:302）：惰性包装，**只在真正需要时查一次**凭据（imagePullSecrets / imagePullServiceAccount），避免无谓的 Secret 读取。
5. **`pullImage`**（:335）：真正发起 CRI PullImage，带 `TrackedAuthConfig` 凭据与 PodSandboxConfig；失败经 `evalCRIPullErr`（:402）把 CRI 错误翻译成可读信息。
6. `recordEnsureImageRequest`（defer）：按 `pullPolicy`/本地是否存在/是否拉取 记录指标。

**设计要点（源码可证）：**
- **凭据惰性单次查找**：`lookupPullCredentials` 包装（:302 注释「only look up credentials once and no earlier than needed」）——本地命中镜像时完全不读 Secret。
- **PullPolicy 分流**：`Never` 缺镜像报错、`Always` 强制拉取、`IfNotPresent` 本地命中跳过（imagePullPrecheck :120）。

### 镜像 GC：realImageGCManager

**源码：** `pkg/kubelet/images/image_gc_manager.go`
- `NewImageGCManager`（+192）：构造 `realImageGCManager`，持有 runtime、statsProvider、postGCHooks、policy（`ImageGCPolicy`）。
- `Start`（+218）：周期驱动 GC。

**驱逐候选排序：`imagesInEvictionOrder`（偏移：+549）**
1. **`detectImages`**（:244）算出当前「在用」镜像集合 `imagesInUse`。
2. 加 `imageRecordsLock`，遍历 `imageRecords`：
   - `isImageUsed`（:655）命中的跳过（在用，不回收）。
   - **`record.pinned` 为真的跳过**（防回收，+16 ～ +20）——固定镜像不被 GC。
   - feature gate `RuntimeClassInImageCriAPI` 开启时用 `getImageTuple`/`getImageIDFromTuple`（:625/:633）处理带 runtime handler 的复合 ID。
3. **`sort.Sort(byLastUsedAndDetected(images))`**（+43）：按「最后使用时间 → 检测时间」升序，最久未用的排最前，优先驱逐。

**GC 主流程：`GarbageCollect`（偏移：+349）**
1. `imagesInEvictionOrder` 取驱逐序列 → `freeOldImages`（:426）先删超过 `MinAge` 的旧镜像。
2. `statsProvider.ImageFsStats` 取镜像文件系统容量/可用空间；容量为 0 报 `InvalidDiskCapacity`。
3. **阈值判定**：`usagePercent = 100 - available*100/capacity`；`usagePercent >= HighThresholdPercent` 时触发回收。
4. **`amountToFree = capacity*(100-LowThresholdPercent)/100 - available`**：目标是把占用降到 LowThreshold，而非清到极限。
5. **`freeSpace`**（:483）按驱逐序列逐个 `freeImage`（:524）删除直到腾出 `amountToFree`；`runPostGCHooks`（:420）通知剩余镜像。
6. `freed < amountToFree` → 发 `FreeDiskSpaceFailed` 事件并报错——**镜像 GC 无法解决非镜像占用**（日志/卷/其它文件，注释 :405 ～ +410）。

**驱逐排序比较器：`byLastUsedAndDetected.Less`（偏移：+647）**：先比 lastUsed（越早越先驱逐），再比 detected 兜底。

**设计要点（源码可证）：**
1. **双阈值回收**：只在超过 HighThreshold 时启动，降到 LowThreshold 即停——避免频繁 GC 抖动，也为突发留缓冲。
2. **在用/固定镜像绝不回收**：`isImageUsed` + `record.pinned` 双重保护，防止删掉正在运行的容器镜像或被 node 指定保留的镜像。
3. **GC 只管镜像盘**：回收失败会明确区分「磁盘被非镜像数据占满」的场景，提示排查其它占用而非继续删镜像。
4. **`freeOldImages` 先行**：超过 MinAge 的镜像先删，再按需做阈值驱动回收，减少不必要的删除。

### 5c 一次新建 Pod 的 CRI gRPC 调用序列解剖（第二层补充视角）

本节把 SyncPod 新建 Pod 展开成**按顺序发出的 CRI gRPC 调用**：每个调用的方法名、关键请求字段、成功信号与失败事件。这是「wire-level」视角——不看函数内部逻辑，只看 kubelet 与容器运行时之间实际往返了哪些 gRPC。

#### 调用序列（新建 Pod，主成功路径）

| 序 | CRI gRPC 方法 | 关键请求字段 | 成功信号 | 失败事件（reason） |
|---|---|---|---|---|
| 1 | `PullImage` | image、auth（pullSecrets）、sandboxConfig | `imageRef` 返回 | `FailedToPullImage`（image_manager.go:380） |
| 2 | `RunPodSandbox` | PodSandboxConfig（hostname、dns、logDir、labels、cgroup、linux namespaces） | `podSandboxID` | `FailedCreatePodSandBox`（SyncPod+2038） |
| 3 | `PodSandboxStatus` | podSandboxID、verbose=false | `resp.Status`（含 podIPs） | `FailedStatusPodSandBox`（SyncPod+2056） |
| 4 | `CreateContainer`（每容器） | podSandboxID、ContainerConfig、PodSandboxConfig | `containerID` | `FailedToCreateContainer`（kuberuntime_container.go:296） |
| 5 | `StartContainer`（每容器） | containerID | 无返回体 | `FailedToStartContainer`（:313） |

**调用顺序（源码锚点）：**
```text
SyncPod (kuberuntime_manager.go:1854)
  ├─ computePodActions (+1388)  ← 决策：建 sandbox + 启哪些容器
  ├─ createPodSandbox (:2018 调用) → runtimeService.RunPodSandbox
  ├─ runtimeService.PodSandboxStatus (:2043) → 取 podIPs
  │     └─ 网络隔离但拿到 IP → StopPodSandbox + 失败（:2073）
  └─ 逐容器 startContainer (kuberuntime_container.go:213)
       ├─ imagePuller.EnsureImageExists (:233) → PullImage
       ├─ runtimeService.CreateContainer (:295) → containerID
       ├─ internalLifecycle.PreStartContainer (:300)
       ├─ runtimeService.StartContainer (:313)
       └─ recordContainerEvent: CreatedContainer → StartedContainer（Normal）
```

**每个 gRPC 调用的失败事件（源码可证）：**

| gRPC 方法 | 失败事件 reason | 事件类型 | 锚点 |
|---|---|---|---|
| `PullImage` | `FailedToPullImage` | Warning | `image_manager.go:380` |
| `RunPodSandbox` | `FailedCreatePodSandBox` | Warning | `kuberuntime_manager.go:2038` |
| `PodSandboxStatus` | `FailedStatusPodSandBox` | Warning | `kuberuntime_manager.go:2056` |
| `CreateContainer` | `FailedToCreateContainer` | Warning | `kuberuntime_container.go:296` |
| `PreStartContainer` 钩子 | `FailedToStartContainer` | Warning | `kuberuntime_container.go:304` |
| `StartContainer` | `FailedToStartContainer` | Warning | `kuberuntime_container.go:313` |
| 成功：容器已建/已启 | `CreatedContainer` / `StartedContainer` | Normal | `kuberuntime_container.go:299` / `:318` |

#### gRPC 错误的统一处理

- 每个 CRI 调用的 gRPC status 经 `grpcstatus.FromError(err)` 取出 `s.Message()`，包装成 K8s 事件 + Go error（`kuberuntime_container.go:296`、:304、:313）。
- sandbox 创建失败的错误可能是 CNI/CSI/CRI 任一来源；若 Pod 已被标记删除（`IsPodTerminationRequested`）则**静默返回**、不记失败（SyncPod:2020 ～ :2026）。
- PodSandboxStatus 拿到 IP 后，若 Pod 要求网络隔离但运行时仍配了网（`IsNetworkIsolatedPod && len(podIPs)>0`），则 `StopPodSandbox` 拆掉 sandbox 并失败（:2073）。

#### 关键 gRPC 调用的顺序依赖（源码可证）

1. **RunPodSandbox 必须先于 CreateContainer**：容器建在 sandbox 的 network namespace 内，sandbox 没起来就没有可加入的网络。
2. **PodSandboxStatus 必须在 CreateContainer 前**：容器的 IP 来自 sandbox status（`determinePodSandboxIPs`，:2061），要先把 IP 填进容器环境。
3. **PullImage 必须先于 CreateContainer**：CreateContainer 的 config 引用镜像引用，镜像不在本地则 Create 失败。
4. **CreateContainer 必须先于 StartContainer**：先建后启，PreStartContainer 钩子夹在两者之间。

## 6. 关键边界与错误
| 场景 | 行为 |
|---|---|
| Pod 创建中被删除 | sandbox 错误静默返回（SyncPod+173～+176） |
| 镜像拉取失败 | FailedToCreateContainer 事件，上层 SyncPod 失败重试 |
| runtimeClass handler 解析失败 | createPodSandbox 返回错误，不落 CRI |
| PreCreate/PreStart 钩子失败 | 带哨兵错误返回，不继续 CRI 调用 |
| sandbox status 查询失败 | 结果失败并跳过该 Pod，等待下轮同步 |
| `RestartAllContainersOnContainerExits` 开启 | computePodActions 提前返回，全部容器进 ContainersToReset（computePodActions+15～+28） |

## 7. 设计意图与验证
**设计意图：**
- **prune init 容器提前优化**：SyncPod 在创建 sandbox 前调用 `pruneInitContainersBeforeStart`（SyncPod+95），比容器 GC 更早清理已终止的 init 容器，降低资源占用（注释 SyncPod+92～+94）。
- **sandbox 就绪回调（Step 5）**：在动态资源准备好、卷挂载完成后，触发 `OnPodSandboxReady` 设置 `PodReadyToStartContainers=True`，失败仅记日志并继续创建（SyncPod+233～+241）。
- **临时容器先于 init 启动（Step 7）**：允许 init 容器启动失败时仍能运行临时容器（SyncPod 后续段）。

**验证：**
- 所有函数位置、内部偏移、CRI 客户端落点、kubelet 调用点均按当前代码基准 `master@8893923` 逐行核实（见 `.progress/evidence/10.md` 证据索引）。
- 关键差异（当前绝对行号，供核对）：`SyncPod` 起点由旧文 :1576 漂移至 :1854（+278）；`computePodActions` :1272 → :1388（+116）；`startContainer` :200 → :213（+13）；`createPodSandbox` :42 → :43（+1）；CRI 客户端方法均 +1 ～ +5。
- 新增/修正内容：SyncPod 步骤由「Step 1-5」扩为「Step 1-7」，新增 Step 5（OnPodSandboxReady 回调）、Step 6（Resize）、Step 7（ephemeral）；startContainer 新增 `ensureContainerSecurityProfile` 步骤（+15）；调用点由 `setActuatedContainerResources` 修正为 `setActuatedContainer`（独立方法 #setActuatedContainerResources+0）；`NewRemoteRuntimeService` 标记 Deprecated，推荐 `NewRemoteRuntimeServiceBuilder`；computePodActions 补入 `RestartAllContainersOnContainerExits` 分支。

[上一篇](09-kubelet同步循环.md) · [总目录](README.md) · [下一篇](11-informer与workqueue机制.md)
