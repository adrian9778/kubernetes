[上一篇](17-CRD扩展与聚合层.md) · [总目录](README.md) · [下一篇](19-kubeadm集群引导.md)

# cloud-controller-manager
> **层级**：第四层——函数级源码解析。
> **范围**：CCM 进程结构、四类云控制器、cloud-provider 接口、与 KCM 的职责切分。
> **代码基准**：adrian@`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4，工作区干净）。
> **锚点记法**：`文件 符号+偏移`，函数定义行=+0；下文所有行位均按此换算，不出现裸行号。

## 1. 生产视角（CCM 对外行为、可见结果）

**它是什么**：cloud-controller-manager（下称 CCM）是与云厂商交互的控制器管理器进程，负责把「Kubernetes 集群」翻译成「云资源」，并把云资源的状态回写进集群。它运行在集群之外（或独立 pod），是云厂商深度集成的唯一入口。

**可见结果（对外行为）**：

| 面向对象 | 行为 | 可见产物 |
|---|---|---|
| 新 Node 注册 | 用云实例元数据初始化 Node | Node 被加上 `failure-domain.beta.kubernetes.io/region|zone` 等标准 label、`node.kubernetes.io/exclude-from-external-load-balancers`、地址（InternalIP/ExternalIP）被覆盖，`TaintExternalCloudProvider` 污点被移除 |
| 已有 Node | 周期同步地址与状态 | Node 的 `status.addresses`、`status.conditions` 随云上实例变化更新 |
| Node 失联/关机 | 周期轮询云上实例是否存在/是否关机 | 云上已删除 → 删除 Node 对象；云上已关机 → 加 `node.cloudprovider.kubernetes.io/shutdown` 污点 |
| Service Type=LoadBalancer | 调云 LB 接口创建/更新/删除负载均衡 | Service 的 `status.loadBalancer.ingress` 写入云 LB 的 IP/hostname |
| Node PodCIDR | 调云路由接口为每个节点维护云路由表 | 云路由表中每个节点一条到达其 PodCIDR 的路由条目 |

**运行前提**：`--cloud-provider=external`（in-tree 云 provider 自 v1.31 起禁用，见第 6 节）。云厂商以独立 CCM 二进制（或插件）接入，本仓库的 `cmd/cloud-controller-manager` 是参考实现/样板（`main.go` 顶部注释明示「This file should be written by each cloud provider」）。

## 2. 开发视角——模块级

| 项 | 内容 |
|---|---|
| 入口 | `cmd/cloud-controller-manager/main.go` `main+0`（本分支 cmd 下无 app 目录；仅 `.import-restrictions`、`OWNERS`、`README.md`、`main.go`、`nodeipamcontroller.go`、`providers.go`） |
| 命令框架与控制器实现 | **全部在 `[staging] k8s.io/cloud-provider/`**：`app/controllermanager.go`、`app/core.go`、`controllers/{node,nodelifecycle,service,route}/`、`cloud.go`、`plugins.go` |
| 启动参数 | `options.NewCloudControllerManagerOptions()`、`app.DefaultInitFuncConstructors`、`names.CCMControllerAliases()` |
| 外部依赖 | 具体云 provider 的 `cloudprovider.Interface` 实现（本仓库之外，经 `--cloud-provider=external` 以独立进程/插件形式接入） |
| 数据流 | cloud config → InitCloudProvider → 启动 4 类控制器：Node informer/Service 事件 → 调云 API → 回写 Node/Service/Route |

## 3. 开发视角——类型级

| 类型 | 文件 | 锚点 | 职责 |
|---|---|---|---|
| `CloudNodeController` | `[staging] cloud-provider/controllers/node/node_controller.go` | `NewCloudNodeController+0`（=122） | 队列式：用云元数据初始化新 Node（label、地址）、周期同步地址与状态 |
| `CloudNodeLifecycleController` | `[staging] cloud-provider/controllers/nodelifecycle/node_lifecycle_controller.go` | `NewCloudNodeLifecycleController+0`（=78） | 轮询式：判定 Node 在云上是否存在/是否关机，删除孤儿 Node 或加 ShutdownTaint |
| `cloudprovider.Interface` | `[staging] cloud-provider/cloud.go` | `Interface+0`（=54） | 云能力总接口（见第 6 节） |

## 4. 开发视角——启动函数级

### 函数：main
**源码：** `cmd/cloud-controller-manager/main.go` `main+0`（=46）
**执行流程：** 构造 options（`NewCloudControllerManagerOptions`）→ 取 `app.DefaultInitFuncConstructors` + `names.CCMControllerAliases()` → 注册 nodeIPAM（`nodeipamcontroller.go`，写入 `controllerInitializers[kcmnames.NodeIpamController]`，并把别名 `nodeipam` 加入 `controllerAliases`）→ `app.NewCloudControllerManagerCommand(ccmOptions, cloudInitializer, controllerInitializers, controllerAliases, fss, wait.NeverStop)` → `cli.Run` → `os.Exit`。

### 函数：cloudInitializer
**源码：** `cmd/cloud-controller-manager/main.go` `cloudInitializer+0`（=82）
**职责：** 调 `cloudprovider.InitCloudProvider(cloudConfig.Name, cloudConfig.CloudConfigFile)` 实例化云 provider；随后在 `cloudInitializer+11`（=93）校验 `cloud.HasClusterID()`：为假时，若 `AllowUntaggedCloud` 则打 Warning，否则 `klog.Fatalf` 退出。
> **关键点**：HasClusterID 校验**不在** `InitCloudProvider` 内部，而是上移到本函数（`cloudInitializer+11`）。`InitCloudProvider` 只负责按名字查表/实例化（见第 6 节）。

### 函数：NewCloudControllerManagerCommand
**源码：** `[staging] cloud-provider/app/controllermanager.go` `NewCloudControllerManagerCommand+0`（=87）
**职责：** 通用控制器管理器命令：解析 options、按 `controllerInitFuncConstructors` 逐个启动控制器、共享 leader election 与 client。

## 5. 开发视角——四类控制器注册与函数级

### 注册表：DefaultInitFuncConstructors
**源码：** `[staging] cloud-provider/app/controllermanager.go` `DefaultInitFuncConstructors+0`（=465）——**现为 `var`（`var DefaultInitFuncConstructors` 是 `map[string]ControllerInitFuncConstructor`），非 `func`**；main 直接引用该 map 作为 `controllerInitializers`。

| 控制器常量（`names/controller_names.go`，行 50-53） | 启动函数（`app/core.go`） | ClientName |
|---|---|---|
| CloudNodeController | startCloudNodeController+0（=43） | node-controller |
| CloudNodeLifecycleController | startCloudNodeLifecycleController+0（=64） | node-controller |
| ServiceLBController | startServiceController+0（=84） | service-controller |
| NodeRouteController | startRouteController+0（=105） | route-controller |

### CloudNodeController（队列式调谐）
| 函数 | 锚点 | 职责 |
|---|---|---|
| `NewCloudNodeController` | `node_controller.go` `NewCloudNodeController+0`（=122） | 校验 `cloud.Instances()` 或 `cloud.InstancesV2()` 至少支持其一；注册 Node add/update handler（enqueueNode），构造 workqueue |
| `RunWithContext` | `node_controller.go` `RunWithContext+0`（=175） | WaitForCacheSync 后启动：周期 `UpdateNodeStatus` 循环 + `workerCount` 个 worker（`runWorker`） |
| `processNextWorkItem` | `node_controller.go` `processNextWorkItem+0`（=226） | workqueue.Get → syncHandler（syncNode） |
| `syncNode` | `node_controller.go` `syncNode+0`（=482） | 单次调谐主体：拉取 Node，处理 cloud taint、标签与地址 |
| `reconcileNodeLabels` | `node_controller.go` `reconcileNodeLabels+0`（=323） | 用云 region/zone/instance-type 写标准 Node label（beta→GA 迁移） |
| `updateNodeAddress` | `node_controller.go` `updateNodeAddress+0`（=428） | 用云上地址集合覆盖 `Node.status.addresses`（跳过仍带 cloud taint 的 Node） |
| `getInstanceMetadata` | `node_controller.go` `getInstanceMetadata+0`（=645） | 经 InstancesV2/Instances 取实例元数据（优先 `node.Spec.ProviderID`） |

### CloudNodeLifecycleController（轮询式，无队列）
| 函数 | 锚点 | 职责 |
|---|---|---|
| `NewCloudNodeLifecycleController` | `node_lifecycle_controller.go` `NewCloudNodeLifecycleController+0`（=78） | 注入 nodeMonitorPeriod 与并发数 |
| `Run` | `node_lifecycle_controller.go` `Run+0`（=118） | `wait.UntilWithContext(ctx, MonitorNodes, nodeMonitorPeriod)` |
| `MonitorNodes` | `node_lifecycle_controller.go` `MonitorNodes+0`（=144） | list 全部 Node，`ParallelizeUntil` 并发：存在且 Ready → 去 ShutdownTaint；`ensureNodeExistsByProviderID`（`+0`=278）不存在 → 删 Node；`shutdownInCloudProvider`（`+0`=248）判定关机 → 加 ShutdownTaint |

**两种控制器形态对比：** Node 初始化走 informer 事件队列（及时、去重）；生命周期走周期全量轮询（需要持续核实云上关机状态）。

#### MonitorNodes 单节点判定深度拆解

`MonitorNodes`（`node_lifecycle_controller.go MonitorNodes+0`=144）是 CloudNodeLifecycleController 的轮询主体：每次列出全部 Node，经 `ParallelizeUntil` 并发对每个节点跑 `processNode`。判定是一条**状态驱动的三分支**：

**processNode 单节点判定（MonitorNodes 内，processNode 闭包起 :157）：**
1. 取 NodeReady condition，默认 `ConditionUnknown`（:159 ～ :163）。
2. **NodeReady == True** → `RemoveTaintOffNode(ShutdownTaint)`（:167 ～ :170）后 return——健康节点不应带关机 taint。
3. NodeReady 非 True（NotReady/Unknown）→ **`ensureNodeExistsByProviderID`**（:171，定义 :278）向云 provider 核实实例是否仍存在：
   - **不存在** → 发 `DeletingNode` 事件 + **删除 Node**（`kubeClient.CoreV1().Nodes().Delete`，:184 ～ :196）。云上已无此实例，taint 已无意义。
   - **存在** → **`shutdownInCloudProvider`**（:202，定义 :248）判定实例是否处于关机状态：
     - 关机 → `AddOrUpdateTaintOnNode(ShutdownTaint)`（:207 ～ :210）。
     - 未关机 → 不动（保持 NotReady，等 kubelet 自行恢复）。

**三分支对照：**

| Node 状态 | 云侧核实 | 动作 |
|---|---|---|
| Ready | —（跳过核实） | 去 ShutdownTaint |
| NotReady + 实例已不存在 | `ensureNodeExistsByProviderID` = false | 删除 Node 对象 |
| NotReady + 实例存在但已关机 | `shutdownInCloudProvider` = true | 加 ShutdownTaint |
| NotReady + 实例存在未关机 | 两者皆否 | 不动，等 kubelet 恢复 |

**为什么用轮询而非事件（源码可证）：** `shutdownInCloudProvider` 注释（:247 ～ +2）与 `processNode` else 分支注释（:199 ～ +204，引用 issue #46442）指出：**各云 provider 对「实例关机」的处理不一致**——有的不会在实例关机时把 Node 从集群删除。因此必须周期性向云侧主动核实关机状态，无法只靠 K8s 侧的 Node 事件。这是 CloudNodeLifecycleController 采用轮询式的根本原因。

**设计要点（源码可证）：**
1. **两段式核实，先存在性后关机性**：只有 NotReady 节点才向云侧发请求（Ready 节点直接去 taint 返回），把云 API 调用集中在真正需要判定的节点上。
2. **实例消失 → 删 Node；实例关机 → 打 taint**：区分「节点已彻底离开」与「节点暂时关机（如休眠、按需停机）」两种云侧状态，前者清理对象、后者靠 taint 驱逐 Pod 但保留 Node。
3. **并发安全**：`ParallelizeUntil` 并发跑 `processNode`，每个 piece 持有 `nodes[piece].DeepCopy()` 独立副本，避免共享竞争。

### Service / Route 控制器
- ServiceLBController：watch Service Type=LoadBalancer，调 `cloudprovider.LoadBalancer` 子接口 `EnsureLoadBalancer`（创建/更新云 LB 与后端实例），回写 Service 的 ingress IP/hostname。
- NodeRouteController：watch Node，调 `cloudprovider.Routes` `CreateRoute`/`ListRoutes`，为每个节点 PodCIDR 维护云路由表条目。`startRouteController` 先检查 `KubeCloudShared.ConfigureCloudRoutes` 与 `cloud.Routes()` 支持，再解析 `ClusterCIDR`（支持 dual-stack）。

## 6. 开发视角——cloud-provider 接口

### Interface（`cloud.go` `Interface+0`=54）
| 方法 | 锚点 | 说明 |
|---|---|---|
| `Initialize(ControllerClientBuilder, <-chan struct{})` | `Interface+4`（=58） | 注入 client 构造器与退出信号 |
| `LoadBalancer() (LoadBalancer, bool)` | `Interface+6`（=60） | 云 LB 能力 |
| `Instances() (Instances, bool)` | `Interface+8`（=62） | 实例元数据（旧） |
| `InstancesV2() (InstancesV2, bool)` | `Interface+13`（=67） | 统一实例元数据（新）；实现后禁用 Zones 调用 |
| `Zones() (Zones, bool)` | `Interface+18`（=72） | 已 DEPRECATED，由 InstancesV2 取代（v1.31 起） |
| `Clusters() (Clusters, bool)` | `Interface+20`（=74） | 集群 ID |
| `Routes() (Routes, bool)` | `Interface+22`（=76） | 云路由 |
| `ProviderName() string` | `Interface+24`（=78） | 云 provider 名称 |
| `HasClusterID() bool` | `Interface+31`（=85） | 配置校验；框架只读此布尔，不读 ID 本身，如何存储/验证由 provider 决定 |

子接口类型定义：LoadBalancer `+0`（=155）、Instances `+0`（=193）、InstancesV2 `+0`（=227）、Routes `+0`（=263）、Zones `+0`（=292）。
辅助：`plugins.go` `IsExternal+0`（=79，`name == externalCloudProvider` 即 true）、`plugins.go` `InitCloudProvider+0`（=99）。

### InitCloudProvider（`plugins.go` `InitCloudProvider+0`=99）
**职责：** 按名字查表/实例化云 provider。`name==""` 或 `IsExternal(name)` 时返回 `nil, nil`（external 由外部插件提供）；否则读 config 文件并调用已注册的 provider 构造器。
> **关键点**：函数体内**无 `HasClusterID` 校验**；该校验由调用方 `cloudInitializer+11` 完成。

## 7. 与 kube-controller-manager 的切分
- 当前 KCM 注册表（`cmd/kube-controller-manager/app/controller_descriptor.go` `NewControllerDescriptors+0`=138，**原 `pkg/controller/controller_descriptor.go` 已不存在**）已无 service/route/cloud-node/cloud-node-lifecycle 描述符；仅保留 nodeIPAM 与 NodeLifecycle（后者是 `pkg/controller/nodelifecycle` 的 Pod 驱逐/zone 失联逻辑，不含云查询）。
- 选项层证据（`cmd/kube-controller-manager/app/options/options.go` `Validate+56`=507）：in-tree cloud providers 自 v1.31（KEP-2395）禁用，非 external 的 `--cloud-provider` 直接报 `ErrorForDisabledProvider`。CCM 侧 `[staging] cloud-provider/options/options.go` `Validate+0`（=263）无此检查。
- 职责结果：云节点初始化/生命周期、Service LB、Route 四类循环归 CCM；KCM 保留与云无关的节点驱逐与其他控制器。

## 8. 调用关系总览
```text
main → NewCloudControllerManagerCommand
 └── cloudInitializer → InitCloudProvider（无 HasClusterID 校验）
      │                  └── cloudInitializer+11：HasClusterID 校验（Warning/Fatal）
      └── start*Controller（经 DefaultInitFuncConstructors 注册）
           ├── CloudNodeController: Node 事件 → syncNode
           │    ├── reconcileNodeLabels
           │    └── updateNodeAddress（云 Instances API）
           ├── CloudNodeLifecycle: MonitorNodes 轮询
           │    ├── ensureNodeExistsByProviderID
           │    └── shutdownInCloudProvider → ShutdownTaint
           ├── ServiceLB: EnsureLoadBalancer
           └── Route: CreateRoute
```

## 9. 设计意图与验证
**设计意图：** CCM 把「云相关」的控制器循环从 KCM 中抽离，形成独立进程。理由：①in-tree 云 provider 禁用后，云厂商各自提供 CCM 二进制，避免上游耦合；②云 API 调用节奏、重试、限流与 KCM 本地循环隔离，互不影响；③`InstancesV2` 接口把「按 Node 对象查询元数据」统一化，显著减少注册/同步时的云 API 调用，并取代已废弃的 `Zones`。

**验证：**
- 符号与偏移全部按 `adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，工作区干净）逐一核证，无符号消失。
- 关键修正点均已随证据更新：①HasClusterID 校验位置（`cmd/cloud-controller-manager/main.go` `cloudInitializer+11`，非 `InitCloudProvider`）；②`DefaultInitFuncConstructors` 为 `var` 而非 `func`；③KCM 注册表路径为 `cmd/kube-controller-manager/app/controller_descriptor.go`；④node_controller.go 各函数行漂移（syncNode/updateNodeAddress 各 +66）。
- 本次基准前移（`889392393810..6bf0bfd51d7`）复核：`git diff` 显示 `cmd/cloud-controller-manager/` 与 `[staging] k8s.io/cloud-provider/` 全区间零改动，本篇所有锚点偏移不变，故正文未作锚点改写。
- `cloudInitializer` 归属澄清：codegraph 确认其为 `cmd/cloud-controller-manager/main.go` 的**顶层函数**（定义行 82，`HasClusterID` 调用在其体内 +11=93），非局部闭包/变量；同名函数另存于 `[staging] cloud-provider/sample/basic_main.go`，故裸写 `cloudInitializer+11` 会被校验器误归属到 KCM，本篇相关处已改用带路径的全限定写法。
- 待确认：`[源码未确认]` —— 无。全部锚点均有证据支撑。

[上一篇](17-CRD扩展与聚合层.md) · [总目录](README.md) · [下一篇](19-kubeadm集群引导.md)
