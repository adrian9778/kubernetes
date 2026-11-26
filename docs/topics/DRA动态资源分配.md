[总目录](../sourceReader/README.md)

# DRA 动态资源分配（Dynamic Resource Allocation）专题

> **代码基准**：`k8s.io/kubernetes`，adrian@`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d7`（2026-11-26，工作区除文档产物外干净），Go 1.27.4。
> **范围**：DRA 从「驱动发布设备库存」到「工作负载用上设备」的完整数据流——ResourceSlice/ResourceClaim/DeviceClass/ResourceClaimTemplate 四类 API 对象、调度器 dynamicresources 插件的各扩展点、resourceclaim controller 的 claim 生命周期、kubelet DRA manager 的设备准备与取消准备、devicetainteviction 的设备级驱逐。覆盖调度器 + controller + kubelet 三侧的当前实现。
> **不覆盖**：具体设备驱动的实现（厂商侧，不在本仓库）；组调度与 PodGroup（见 [v1.37 PodGroup 与组合调度](v1.37-PodGroup与组合调度.md)）；传统 device plugin 的注册/Allocate 路径（见 [设备插件注册与 Allocate](设备插件注册与Allocate.md)，DRA 是与之并行的另一套机制）。
> **主文件**：`pkg/scheduler/framework/plugins/dynamicresources/`（调度插件 + DRA manager）、`pkg/controller/resourceclaim/`（claim 生命周期 controller）、`pkg/kubelet/cm/dra/`（节点侧准备/取消准备）、`pkg/controller/devicetainteviction/`（设备 taint 驱逐）、`staging/src/k8s.io/api/resource/v1beta1/`（API 类型）。

## 0. 结论速览

DRA 是 Kubernetes 处理**第三方设备**（GPU、FPGA、网卡、自定义加速器等）的新机制。它把传统 device plugin 的「kubelet 中心化注册 + 按容器 Allocate」换成一套**声明式、可调度感知**的模型：驱动把设备库存发布为 `ResourceSlice`，工作负载通过 `ResourceClaim`/`ResourceClaimTemplate` 声明资源请求，调度器用 `dynamicresources` 插件在 Filter/Reserve 阶段判断「这个节点有没有可分配的设备」，并把分配结果写回 API；kubelet 在 Pod 落地后准备（prepare）设备、Pod 终止时取消准备（unprepare）。

### 0.1 这项能力现在能做什么

- 驱动通过 `ResourceSlice` **发布设备库存**：节点或驱动侧把可提供哪些设备、每个设备的属性上报。
- 工作负载通过 `ResourceClaim`（或 `ResourceClaimTemplate` 生成）**声明设备请求**，与 Pod 解耦、可跨 Pod 共享。
- `DeviceClass` 定义**设备选择/配置类别**：用 CEL selector 表达「要哪类设备」。
- 调度器 `dynamicresources` 插件在 **PreFilter/Filter/Score/Reserve/PreBind** 各扩展点参与调度决策，产生设备分配（allocation）。
- kubelet DRA manager 在节点侧 **PrepareResources / UnprepareResources**：让设备对容器可见（如生成 CDI 信息），Pod 终止按生命周期取消准备。
- **DRA 抢占（KEP-5690）**：当高优先级 Pod 的设备请求在某节点放不下时，调度器可在抢占模拟中释放低优先级 Pod 独占占用的设备，并为当选的抢占者**保留（hold）**这些设备直到它真正调度上去；这靠 `dynamicresources` 插件实现的 nomination / victim 扩展点完成。
- `devicetainteviction` controller 把 Node taint 思路扩展到**设备级**：设备不可用/被打 taint 且无法容忍时，驱逐正在使用它的 Pod。

### 0.2 关键结论、适用范围与不做什么

| 结论 | 说明 |
|---|---|
| DRA 与 device plugin 是两套并行机制 | 传统 device plugin（kubelet 注册/Allocate）不等于 DRA；DRA 走 ResourceClaim/ResourceSlice 模型 |
| 设备分配发生在调度期，不是 Pod 运行期 | 调度器 Reserve 阶段锁定分配，PreBind 写回 API；kubelet 只负责「准备已分配的设备」 |
| ResourceClaim 与 Pod 解耦 | claim 可独立于 Pod 创建、可共享；`ResourceClaimTemplate` 按工作负载生成 claim |
| 受版本/特性演进影响 | resource API 存在 v1alpha3/v1beta1/v1beta2/v1 多版本，本文以当前工作区 `v1beta1` 类型为准 |
| 设备不可用会触发驱逐 | devicetainteviction 按设备 taint 与容忍关系推进驱逐，扩展了 Node taint 语义 |
| 高优先级 Pod 可抢占低优先级 Pod 的**独占**设备 | `DRAPreemption` 启用时，抢占模拟释放受害者独占占用的节点本地设备，并为抢占者保留到它调度成功；共享（ShareID）与管理员访问设备在 alpha 不释放 |

**不做什么**：不实现真实设备驱动；不保证跨驱动的设备语义一致；不处理 device plugin 侧的 `Register`/`Allocate` gRPC（那是 [设备插件注册与 Allocate](设备插件注册与Allocate.md) 的范围）。

## 1. 生产视角：实际怎么用、会得到什么

### 1.1 参与者、真实入口与前置条件

| 角色 | 是谁 | 做什么 |
|---|---|---|
| 设备驱动（vendor） | 集群内运行的 DRA driver | 发布 `ResourceSlice`（设备库存）、处理设备分配 |
| 用户 / 工作负载 | 提交带 `resourceClaims` 的 Pod + `ResourceClaimTemplate` | 声明「我要某类设备」 |
| kube-scheduler | `dynamicresources` 插件 | 调度期判断节点可分配性、锁定分配 |
| resourceclaim controller | `pkg/controller/resourceclaim` | 推进 claim 的生命周期（reserve/binding 等） |
| kubelet DRA manager | `pkg/kubelet/cm/dra` | 节点侧准备/取消准备设备，生成容器可见信息 |
| devicetainteviction controller | `pkg/controller/devicetainteviction` | 设备 taint/不可用时驱逐相关 Pod |
| 被抢占的设备使用方 | 持有独占设备分配的低优先级 Pod | 抢占发生时其 Pod 被删除、设备分配被释放，让位给高优先级抢占者 |

前置条件：① 集群已启用 DRA 相关 API（resource API 版本随版本演进）；② 有 DRA driver 发布了 `ResourceSlice`；③ Pod 通过 `spec.resourceClaims` 引用 claim。缺 ② 时 claim 无法分配、Pod 卡在调度期；缺 ③ 时设备请求不生效。

### 1.2 生产主流程图

```mermaid
flowchart TD
    A["DRA driver 发布 ResourceSlice（设备库存）"] --> B["用户提交 Pod + ResourceClaimTemplate"]
    B --> C["resourceclaim controller 由 template 生成 ResourceClaim"]
    C --> D["Pod 引用 claim，进入调度队列"]
    D --> E["调度器 dynamicresources 插件"]
    E --> F["PreFilter：收集 Pod 的 claims、校验 DeviceClass"]
    F --> G["Filter：按节点 ResourceSlice + CEL selector 判断可分配"]
    G --> H["Score：设备维度打分（可分配数量等）"]
    H --> I["Reserve：锁定设备分配"]
    I --> J["PreBind：把分配结果写回 claim/API"]
    J --> K["Pod 落到节点，kubelet DRA manager"]
    K --> L["PrepareResources：设备对容器可见（CDI 等）"]
    L --> M["容器运行时拿到设备信息启动容器"]
    M --> N["Pod 终止 → UnprepareResources 取消准备"]
    G -->|无可分配设备| O["调度失败 / PostFilter 处理（组调度走 PodGroupPostFilter）"]
```

### 1.3 输入、处理、输出与接收方

| 生产环节 | 输入 | 实际处理 | 可见输出 | 接收方 |
|---|---|---|---|---|
| 库存发布 | 驱动上报设备 | driver 写 `ResourceSlice` | 节点/驱动设备库存 | 调度器（resourceslicetracker） |
| claim 生成 | `ResourceClaimTemplate` | controller 生成 `ResourceClaim` | claim 对象 | API / 调度器 |
| 调度决策 | Pod 的 claims + 节点库存 | Filter/Score/Reserve | 设备 allocation | claim 状态 / API |
| 节点准备 | 已绑定 Pod + claim | kubelet prepare | 设备对容器可见（CDI 信息） | 容器运行时 |
| 设备驱逐 | DeviceTaintRule / 设备不可用 | devicetainteviction 判定容忍 | 驱逐相关 Pod | kubelet / API |

### 1.4 主流程、分支与异常结果

- **正常路径**：库存就绪 → claim 生成 → 调度器各扩展点通过 → PreBind 写回分配 → kubelet 准备设备 → 容器启动。
- **无设备可分配**：Filter 阶段失败，Pod 不可调度；单 Pod 走 `PostFilter`，组调度走 `PodGroupPostFilter`（联动 [v1.37 PodGroup 与组合调度](v1.37-PodGroup与组合调度.md)）。
- **Reserve 后分配冲突**：`Unreserve` 回滚已锁定的分配。
- **Pod 终止**：`UnprepareResources` 按生命周期取消准备设备。
- **设备被打 taint / 不可用**：`devicetainteviction` 根据设备 taint 与 Pod 容忍关系驱逐正在使用它的 Pod——把 Node taint 扩展到「被 claim 使用的设备」。
- **设备被高优先级 Pod 抢占**：当某节点的设备被低优先级 Pod 独占、高优先级抢占者放不下时，调度器的抢占模拟会选中受害者并释放其**独占**设备，把这些设备为抢占者**保留**在该节点，直到抢占者调度成功；受害者 Pod 随之被删除、其 claim 分配被解除。共享设备与管理员访问设备在 alpha 不参与释放。抢占未走通时，被保留的设备会被释放并回发事件，唤醒同样在等设备的 Pod。

### 1.5 边界与验收表现

- DRA 不改变无设备请求 Pod 的调度行为；未引用 claim 的 Pod 完全不经过 dynamicresources 插件的设备分支。
- 验收看三点：claim 是否被正确 reserve/allocation；节点上设备是否对容器可见；设备不可用时相关 Pod 是否按 taint 规则被驱逐。

## 2. 开发视角：当前代码怎么实现

### 2.1 实现地图

| 职责 | 文件 · 符号 |
|---|---|
| 调度插件入口/扩展点 | `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `DynamicResources` |
| 调度期 DRA manager | `pkg/scheduler/framework/plugins/dynamicresources/dra_manager.go` · `DefaultDRAManager` |
| claim 生命周期 controller | `pkg/controller/resourceclaim/controller.go` · `Controller` |
| kubelet DRA manager | `pkg/kubelet/cm/dra/manager.go` · `Manager` |
| kubelet DRA plugin manager | `pkg/kubelet/cm/dra/plugin/dra_plugin_manager.go` · `DRAPluginManager` |
| 设备 taint 驱逐 | `pkg/controller/devicetainteviction/device_taint_eviction.go` · `Controller` |
| API 类型 | `staging/src/k8s.io/api/resource/v1beta1/types.go` · `ResourceSlice` / `ResourceClaim` / `DeviceClass` / `ResourceClaimTemplate` |

### 2.2 真实调用链

```
driver → ResourceSlice（库存）
user → ResourceClaimTemplate → resourceclaim controller（生成 ResourceClaim）
Pod(spec.resourceClaims) → 调度队列
  → DynamicResources.PreEnqueue                     （入队前判定 claim 是否就绪）
  → DynamicResources.PreFilter  +0                  （收集 claims、校验 DeviceClass）
  → DynamicResources.Filter     +0                  （按节点库存 + CEL selector 判可分配）
  → DynamicResources.Score      +0 / NormalizeScore （设备维度打分）
  → DynamicResources.Reserve    +0                  （锁定分配）
  → DynamicResources.PreBind    +0                  （分配结果写回 API）
  → 失败：PostFilter +0 / PodGroupPostFilter / Unreserve
Pod 落节点 → kubelet Manager.Start +0 → reconcileLoop +0
  → PrepareResources +0（prepare 设备，容器可见）
  → Pod 终止 → UnprepareResources +0
设备 taint/不可用 → devicetainteviction Controller.Run +0 → 驱逐相关 Pod
```

### 2.3 关键源码定位

锚点记法 `符号+N`：N 为相对符号定义行的偏移，`+0` 即定义行。接收者短名：调度插件 `pl`（`*DynamicResources`）、kubelet manager `m`、resourceclaim controller `ec`、devicetainteviction controller `tc`。可用 `.progress/verify_anchors.py` 反查。

| 符号 | 定义文件 | 作用 |
|---|---|---|
| `New+0` | `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` | 插件构造 |
| `pl.PreFilter+0` | 同上 | 收集 claims、校验 DeviceClass |
| `pl.Filter+0` | 同上 | 逐节点判设备可分配 |
| `pl.Score+0` / `pl.NormalizeScore+0` | 同上 | 设备维度打分 |
| `pl.Reserve+0` / `pl.Unreserve+0` | 同上 | 锁定/回滚分配 |
| `pl.PreBind+0` | 同上 | 分配结果写回 |
| `pl.PostFilter+0` / `pl.PodGroupPostFilter+0` | 同上 | 不可调度兜底（单 Pod / 组） |
| `pl.deallocateOrDeletePodClaims+0` | 同上 | 失败时释放 claim |
| `NewDRAManager+0` | `pkg/scheduler/framework/plugins/dynamicresources/dra_manager.go` | 调度期 DRA manager |
| `ec.Run+0` / `ec.syncHandler+0` | `pkg/controller/resourceclaim/controller.go` | claim 生命周期 controller |
| `NewManager+0` | `pkg/kubelet/cm/dra/manager.go` | kubelet DRA manager 构造 |
| `m.Start+0` / `m.reconcileLoop+0` | `pkg/kubelet/cm/dra/manager.go` | 节点侧调谐循环 |
| `m.PrepareResources+0` / `m.UnprepareResources+0` | `pkg/kubelet/cm/dra/manager.go` | 准备/取消准备设备 |
| `NewDRAPluginManager+0` | `pkg/kubelet/cm/dra/plugin/dra_plugin_manager.go` | 节点侧 DRA plugin manager |
| `tc.Run+0` | `pkg/controller/devicetainteviction/device_taint_eviction.go` | 设备 taint 驱逐 controller |
| `ResourceSlice+0` / `ResourceClaim+0` / `DeviceClass+0` / `ResourceClaimTemplate+0` | `staging/src/k8s.io/api/resource/v1beta1/types.go` | 四类核心 API 对象 |

- 调度插件状态：`stateData+0`（单 Pod 周期状态）、`podGroupStateData+0`（组调度状态，联动组调度）、`nodeAllocation+0`（节点分配结果）。
- 设备池模型：`pkg/controller/devicetainteviction/device_taint_eviction.go` 的 `pool+0` / `poolID+0` / `taintedDevice+0` 表达「驱动→设备池→被 taint 设备」，`allocatedClaim+0` 关联被 claim 占用的设备。

### 2.4 核心机制与必须保持的规则

- **分配在调度期锁定**：`Reserve` 锁定、`PreBind` 写回、`Unreserve` 回滚——三者必须成对，否则分配泄漏。
- **claim 与 Pod 生命周期解耦**：claim 可先于 Pod 存在、可共享；controller 负责把 claim 推进到可分配/已分配。
- **节点侧只「准备已分配设备」**：kubelet `PrepareResources` 不做分配决策，只让已 reserve 的设备对容器可见；Pod 终止必须 `UnprepareResources`，否则设备泄漏。
- **库存来自 ResourceSlice**：调度器不直接问驱动，而是读 `ResourceSlice`（经 resourceslicetracker 缓存）；库存变化通过事件触发重新入队。
- **设备 taint 驱逐独立于 Node taint**：`devicetainteviction` 观察 Pod、ResourceClaim、ResourceSlice、DeviceTaintRule、DeviceClass，把 taint 语义下沉到被 claim 使用的具体设备。

### 2.5 DRA 抢占子系统（KEP-5690）

`DRAPreemption` 特性门控启用后，`dynamicresources` 插件不再只做「节点有没有空闲设备」的判断，而是接入调度框架的**抢占流程**：当高优先级 Pod 在某节点因设备被占用而放不下时，插件参与「释放哪些受害者的设备能让它放得下」的模拟，并在抢占者当选后**保留**这些设备。整套实现分散在 5 个新文件 + 主文件联动。

**生产语义**：抢占者（高优先级 Pod）带着 `resourceClaims` 进入调度，插件先判断它「有没有资格靠抢占腾设备」（`canPreemptDevices`：有待分配 claim、claim 归它所有、未禁用抢占、不属于 PodGroup、非 DRA 扩展资源）。资格成立时，抢占模拟逐个尝试移除低优先级受害者 Pod，看其独占占用的**节点本地**设备被释放后抢占者能否通过 Filter。模拟收敛后，插件从「最终受害者集合」对应的 Filter 结果里取出待分配设备的分配方案，作为抢占者的**设备保留（hold）**登记到内存里的 PodNominator——只要抢占者还被提名（nominated）在该节点，这些设备就不会被优先级不高于它的其它 Pod 抢走。受害者 Pod 被删除后其 claim 分配解除，释放出的设备按保留归抢占者使用。若抢占者最终没调度上去（提名结束），保留被撤销并回发一个「claim 分配→nil」的合成事件，唤醒同样在等这些设备的 Pod。

#### 2.5.1 抢占资格与模拟数据（`preemption.go`）

| 符号 | 定义 | 作用 |
|---|---|---|
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `preemptionData` | `+0` | 单个调度周期内、跨 CycleState 克隆共享的抢占状态（`canPreemptDevices`、allocatedState/ResourceSlice 快照、Filter 结果缓存） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `newPreemptionData` | `+0` | 构造 `preemptionData`，据 claim 归属/PreemptionPolicy/PodGroup 计算资格位 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `ownsClaimsToAllocate` | `+0` | Pod 是否拥有其全部待分配 claim（alpha 仅放行 `ResourceClaimTemplate` 生成、被 Pod 控制的 claim） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `canReleaseOnNode` | `+0` | 判断一次设备分配是否全为节点本地、且至少一个独占占用——只有这种释放才能为抢占者腾出设备 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `hasAllocatedNodeLocalDevice` | `+0` | 节点上是否存在被独占分配的本地设备（alpha 唯一能被抢占释放的形态） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `allocatedStateForSlices` | `+0` | 把 PreFilter 的 `AllocatedState` 收敛到给定 ResourceSlice 的设备，避免每次模拟 Filter 都整集群拷贝 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `simulatedResults` | `+0` | 缓存本周期各节点在各「已释放 claim 集」下的 Filter 结果；`final` 取最终受害者集合对应的那份 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `releasable` | `+0` | 判断受害者 Pod 的某个 claim 是否可被本次抢占释放 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `preemptionSimulationState` | `+0` | 存进独立 CycleState 条目的「已移除 Pod + 已提名保留」模拟态，`Clone` 复制它（主 `stateData` 不复制） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `RemovePod` / `AddPod` | `+0` / `+0` | `PreFilterExtensions` 的两侧：模拟移除受害者 / 悔改恢复受害者 |

`preemptionData` 由主文件 `stateData.preemption` 字段持有，`DRAPreemption` 未启用或 Pod 无 claim 时为 nil。

#### 2.5.2 跨周期跟踪器（`preemption_tracker.go`）

抢占状态要跨调度周期存在（受害者 claim 等待解除、抢占者保留等待落地），由 `DefaultDRAManager` 持有的跟踪器负责，多个 scheduler profile 共享一份。

| 符号 | 定义 | 作用 |
|---|---|---|
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `preemptionTracker` | `+0` | 跨周期抢占态：提名保留表（`holds`）+ 等待解除的受害者 claim 表（按节点与 claim UID 双索引） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `nominate` | `+0` | 记录某提名 Pod 在节点上的设备保留，返回被替换的旧保留 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `hold` | `+0` | 读取某 Pod 当前的设备保留 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `release` | `+0` | 撤销某 Pod 的设备保留（提名结束时） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `assumeVictimClaim` | `+0` | 登记被抢占受害者的 claim 为「等待解除」 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `victimClaimsOnNode` | `+0` | 列出某节点上仍在等待解除的受害者 claim |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `forgetVictimClaim` | `+0` | 受害者 claim 已解除分配或被删除后，从跟踪表摘除 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `claimHandlers` | `+0` | 返回 claim 事件的 cache 处理器：分配被清空或 claim 被删时自动 `forgetVictimClaim` |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption_tracker.go` · `podHold` / `releasedClaim` | `+0` / `+0` | 保留条目（节点 + 各 claim 的分配结果） / 受害者 claim 条目（含所属 Pod UID） |

锁序约束：调度队列在持自身锁时调用 nomination 相关回调，故持 `preemptionTracker.mutex` 的代码不得再回调调度队列或发起 API 调用。

#### 2.5.3 nomination 与 victim 扩展点（`nominationextensions.go` / `victimextensions.go` / `holds.go`）

调度框架新增了 nomination / victim 两类扩展点，`dynamicresources` 都实现了：

| 符号 | 定义 | 作用 |
|---|---|---|
| `pkg/scheduler/framework/plugins/dynamicresources/nominationextensions.go` · `PrepareNomination` | `+0` | 从最终受害者集合的 Filter 结果里取出该节点要保留的设备，产出 `podHold`（无可保留则返回 nil） |
| `pkg/scheduler/framework/plugins/dynamicresources/nominationextensions.go` · `AddNomination` | `+0` | 把 `podHold` 登记为提名 Pod 的设备保留；prepared 为 nil 时改为撤销既有保留 |
| `pkg/scheduler/framework/plugins/dynamicresources/nominationextensions.go` · `RemoveNomination` | `+0` | 提名结束，撤销该 Pod 的设备保留 |
| `pkg/scheduler/framework/plugins/dynamicresources/nominationextensions.go` · `releaseResult` | `+0` | 撤销保留时判断是否真的放出了设备，是则发 `EventResourceClaimHoldReleased` 唤醒等待者 |
| `pkg/scheduler/framework/plugins/dynamicresources/victimextensions.go` · `AssumeVictim` | `+0` | 抢占者当选后，把受害者 Pod 的受控、已分配 claim 登记为等待解除 |
| `pkg/scheduler/framework/plugins/dynamicresources/victimextensions.go` · `PodEligibleToPreempt` | `+0` | 抢占者在本节点被提名的受害者 claim 尚未解除分配时返回 false，阻止对同一节点重复抢占 |
| `pkg/scheduler/framework/plugins/dynamicresources/victimextensions.go` · `isVictimClaimSettling` | `+0` | 判断某 claim 是否仍是等待解除的受害者 claim（并清理过期条目） |
| `pkg/scheduler/framework/plugins/dynamicresources/victimextensions.go` · `settlingVictimClaims` | `+0` | 列出节点上仍在等待解除的受害者 claim 名，供 `preemptionMightHelp` 快速判空 |
| `pkg/scheduler/framework/plugins/dynamicresources/victimextensions.go` · `deallocateVictimClaims` | `+0` | `PostFilter` 兜底：`Unreserve` 后仍有 PreBind 抢占留下的已分配 claim 时主动解除 |
| `pkg/scheduler/framework/plugins/dynamicresources/holds.go` · `nodeHolds` | `+0` | 聚合单次 Filter 里某节点上所有提名 Pod 保留的独占设备 |
| `pkg/scheduler/framework/plugins/dynamicresources/holds.go` · `holdsOnNode` | `+0` | 从模拟态收集该节点的活动保留（无则 nil） |
| `pkg/scheduler/framework/plugins/dynamicresources/holds.go` · `applyTo` | `+0` | 把保留的设备并入 `AllocatedState`，使 Filter/Reserve 不会把保留设备分给别人 |

保留只覆盖**独占分配**的设备，所以把它们并集进 `AllocatedDevices` 不需要与仍在解除中的受害者 claim 对账。

#### 2.5.4 主文件联动（`dynamicresources.go`）

| 符号 | 定义 | 与抢占的关系 |
|---|---|---|
| `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `stateData` | `+0` | 单 Pod 周期状态；`preemption` 字段持有 `preemptionData`，`preBindSucceeded`/`claimBeingAllocated` 服务抢占路径。同名类型在 `volume_binding.go` 也有，本篇指 DRA 插件这一份 |
| `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `podGroupStateData` | `+0` | PodGroup 组调度周期状态；组内 Pod 的抢占交给 workload-aware preemption，`canPreemptDevices` 因此为 false。同名类型在 `pkg/scheduler/backend/queue/podgroupstate.go` 也有，本篇指 DRA 插件这一份 |
| `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `nodeAllocation` | `+0` | 每节点分配结果，抢占模拟的 Filter 结果最终落成它 |
| `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `Filter` | `+0` | 抢占模拟的核心消费方：套用受害者释放（`getPreemptionSimulationState`）与设备保留（`holdsOnNode`）后重算可分配性，并按 `isVictimClaimSettling` 跳过仍在解除的 claim |
| `pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go` · `computeScore` | `+0` | 设备维度打分，抢占腾出设备后按 `FirstAvailable` 请求优先级给分 |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `getPreemptionSimulationState` | `+0` | 从 CycleState 读出抢占模拟态（注意它在 `preemption.go`，不在主文件） |
| `pkg/scheduler/framework/plugins/dynamicresources/preemption.go` · `preemptionMightHelp` | `+0` | 判断「抢占在本节点是否可能有用」，避免无谓触发抢占 |
| `pkg/scheduler/framework/plugins/dynamicresources/nodeallocatabledynamicresources.go` · `validateNodeAllocatableDRAClaimSharing` | `+0` | 节点可分配资源型设备的 claim 共享校验（在 `nodeallocatabledynamicresources.go`，非主文件） |
| `pkg/features/kube_features.go` · `DRAPreemption` | `+0` | 抢占总开关；与 `EnableDRAPrioritizedList`、`EnableDRANodeAllocatableResources`、`EnableDRASchedulerFilterTimeout`、`EnableDRADeviceBindingConditions`、`EnableDRAResourceClaimDeviceStatus`、`DRAOptionalNodeOperations` 等门控并存 |

#### 2.5.5 抢占实现流程图

```mermaid
flowchart TD
    A["高优先级抢占者 Pod 带 resourceClaims 进入调度"] --> B["PreFilter：newPreemptionData 判定 canPreemptDevices"]
    B -->|无资格| Z1["不参与 DRA 抢占，走普通调度"]
    B -->|有资格| C["Filter：设备放不下该节点"]
    C --> D["preemptionMightHelp：本节点有无低优先级 claim Pod / 待解除受害者"]
    D -->|无| Z2["本节点无抢占价值"]
    D -->|有| E["DefaultPreemption 触发 PreFilterExtensions.RemovePod 逐个试移受害者"]
    E --> F["Filter 套用受害者释放 + holdsOnNode 重算可分配性"]
    F --> G{"抢占者能通过 Filter？"}
    G -->|否| E
    G -->|是| H["模拟收敛，最终受害者集合确定"]
    H --> I["PrepareNomination：从最终结果取待保留设备产出 podHold"]
    I --> J["AddNomination：设备保留登记进 preemptionTracker + PodNominator"]
    J --> K["AssumeVictim：受害者 claim 登记为等待解除"]
    K --> L["受害者 Pod 被删除，claim 分配解除"]
    L --> M["保留设备归抢占者，Reserve/PreBind 落地分配"]
    M --> N["抢占者调度成功，提名结束 RemoveNomination 撤销保留"]
    M -->|抢占者未落地| O["RemoveNomination 撤销保留 + 发 HoldReleased 事件唤醒等待者"]
    K -.->|PodEligibleToPreempt| P["受害者 claim 未解除前阻止同节点重复抢占"]
```

### 2.6 验证方式与可观察结果

- 单元测试：`pkg/scheduler/framework/plugins/dynamicresources/dynamicresources_test.go`、`dra_manager_test.go`；`pkg/controller/devicetainteviction/device_taint_eviction_test.go`。
- 代码回核：本文锚点可用 `.progress/verify_anchors.py` 反查定义行与偏移。
- 生产实测：需集群启用 DRA + 部署真实 driver 后，观察 claim 的 allocation 状态、节点设备可见性、设备 taint 驱逐 `[待运行验证]`。

### 2.7 后续引用与修改联动

- 组调度中的设备：[v1.37 PodGroup 与组合调度](v1.37-PodGroup与组合调度.md)（`PodGroupPostFilter`、`podGroupStateData`）。
- 传统设备插件对照：[设备插件注册与 Allocate](设备插件注册与Allocate.md)。
- kubelet 资源管理：[kubelet volume manager](kubelet-volume-manager.md)（同类「准备/取消准备」双状态模型）。
- 改动联动：调整 resource API 版本（v1beta1→v1）会影响调度插件、controller、kubelet 三侧的类型引用；调整 `Reserve`/`Unreserve` 配对逻辑会直接影响分配一致性。

## 3. 证据与缺口

- 本文符号均在当前工作区 `pkg/scheduler/framework/plugins/dynamicresources/`、`pkg/controller/resourceclaim/`、`pkg/controller/devicetainteviction/`、`pkg/kubelet/cm/dra/`、`staging/src/k8s.io/api/resource/v1beta1/` 下经 grep 核实存在；偏移以定义行为 `+0`。
- §2.5 抢占子系统的符号定义行经 codegraph 逐个回核（`preemptionData`@59、`nominate`@86、`PodEligibleToPreempt`@81、`holdsOnNode`@47、`Filter`@1024、`stateData`@73、`nodeAllocation`@197 等），与文中偏移一致。§2.5 锚点采用带路径的 `pkg/.../file.go` · `Symbol` `+0` 记法以消歧同名符号（`stateData`、`podGroupStateData` 等在多文件存在），与 §2.3 的裸 `Symbol+0` 记法并存（此处 `Symbol+0` 为记法示例，非具体符号引用）。
- 生产实测：抢占链路（受害者选定、设备保留、HoldReleased 事件、PodEligibleToPreempt 阻止重复抢占）需 `DRAPreemption` 启用 + 真实多优先级设备工作负载才能观察 `[待运行验证]`。
- 源自旧库 `docs/adrian/sourceReader2/11-v1.37-Workload-PodGroup与DRA.md` 的主题，按当前源码重写并替换其 `#L` 行号引用为符号+偏移。
- 待确认：resource API 的默认启用版本与各版本 gate 关系，以 `staging/src/k8s.io/api/resource/` 当前版本目录与 `pkg/features/` 为准 `[待运行验证]`。

[总目录](../sourceReader/README.md)
