[上一篇](17-CRD扩展与聚合层.md) · [总目录](README.md) · [下一篇](19-kubeadm集群引导.md)

# cloud-controller-manager
> **层级**：第四层——函数级源码解析。
> **范围**：CCM 进程结构、四类云控制器、cloud-provider 接口、与 KCM 的职责切分。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/cloud-controller-manager/main.go#main`（本分支无 cmd 下 app 目录） |
| 命令框架与控制器实现 | **全部在 `[staging] k8s.io/cloud-provider/`**：`app/controllermanager.go`、`controllers/{node,nodelifecycle,service,route}/`、`cloud.go` |
| 启动参数 | `options.NewCloudControllerManagerOptions()`、`app.DefaultInitFuncConstructors`、`names.CCMControllerAliases()` |
| 外部依赖 | 具体云 provider 的 `cloudprovider.Interface` 实现（本仓库之外，经 `--cloud-provider=external` 以独立进程/插件形式接入） |
| 数据流 | cloud config → InitCloudProvider → 启动 4 类控制器：Node informer/Service 事件 → 调云 API → 回写 Node/Service/Route |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `CloudNodeController` | `cloud-provider/controllers/node/node_controller.go:98` | 队列式：用云元数据初始化新 Node（label、地址）、周期同步地址与状态 |
| `CloudNodeLifecycleController` | `controllers/nodelifecycle/node_lifecycle_controller.go:62` | 轮询式：判定 Node 在云上是否存在/是否关机，删除孤儿 Node 或加 ShutdownTaint |
| `cloudprovider.Interface` | `cloud-provider/cloud.go:43` | 云能力总接口（见第 5 节） |

## 3. 启动函数级

### 函数：main
**源码：** `cmd/cloud-controller-manager/main.go:47`
**执行流程：** 构造 options → 取 DefaultInitFuncConstructors + 别名 → 注册 nodeIPAM（`nodeipamcontroller.go`）→ `app.NewCloudControllerManagerCommand(...)`。
**cloudInitializer（main.go:82）：** 调 `cloudprovider.InitCloudProvider`，校验 `HasClusterID`，返回 cloudprovider.Interface。

### 函数：NewCloudControllerManagerCommand
**源码：** `cloud-provider/app/controllermanager.go:83`
**职责：** 通用控制器管理器命令：解析 options、按 `controllerInitFuncConstructors` 逐个启动控制器、共享 leader election 与 client。

## 4. 四类控制器注册与函数级

### 注册表：DefaultInitFuncConstructors
**源码：** `app/controllermanager.go:441`
| 控制器常量（`names/controller_names.go`） | 启动函数（`app/core.go`） |
|---|---|
| CloudNodeController | startCloudNodeController:42 |
| CloudNodeLifecycleController | startCloudNodeLifecycleController:63 |
| ServiceLBController | startServiceController:83 |
| NodeRouteController | startRouteController:102 |

### CloudNodeController（队列式调谐）
| 函数 | 行 | 职责 |
|---|---|---|
| `NewCloudNodeController` | node_controller.go:117 | 注册 Node handler，构造 nodeQueue |
| `RunWithContext` | :170 | WaitForCacheSync 后启动 worker |
| `processNextWorkItem` | :221 | Get → syncHandler |
| `syncNode` | :416 | 单次调谐主体 |
| `reconcileNodeLabels` | :312 | 用云 region/zone/instance-type 写标准 Node label |
| `updateNodeAddress` | :362 | 用云上地址集合覆盖 Node.status.addresses |
| `getInstanceMetadata` | :580 | 经 InstancesV2/Instances 取实例元数据 |

### CloudNodeLifecycleController（轮询式，无队列）
| 函数 | 行 | 职责 |
|---|---|---|
| `NewCloudNodeLifecycleController` | node_lifecycle_controller.go:80 | 注入 monitor period 与并发数 |
| `Run` | :120 | `wait.UntilWithContext(ctx, MonitorNodes, nodeMonitorPeriod)` |
| `MonitorNodes` | :146 | list 全部 Node，`ParallelizeUntil` 并发：存在且 Ready → 去 ShutdownTaint；`ensureNodeExistsByProviderID`（:280）不存在 → 删 Node；`shutdownInCloudProvider`（:250）判定关机 → 加 ShutdownTaint |

**两种控制器形态对比：** Node 初始化走 informer 事件队列（及时、去重）；生命周期走周期全量轮询（需要持续核实云上关机状态）。

### Service / Route 控制器
- ServiceLBController：watch Service Type=LoadBalancer，调 `cloudprovider.LoadBalancer` 子接口 EnsureLoadBalancer（创建/更新云 LB 与后端实例），回写 Service 的 ingress IP/hostname。
- NodeRouteController：watch Node，调 `cloudprovider.Routes` CreateRoute/ListRoutes，为每个节点 PodCIDR 维护云路由表条目。

## 5. cloud-provider 接口

### Interface（cloud.go:43）
| 方法 | 行 | 说明 |
|---|---|---|
| `Initialize(ControllerClientBuilder, <-chan struct{})` | 47 | 注入 client 构造器与退出信号 |
| `LoadBalancer() (LoadBalancer, bool)` | 49 | 云 LB 能力 |
| `Instances() (Instances, bool)` | 51 | 实例元数据（旧） |
| `InstancesV2() (InstancesV2, bool)` | 56 | 统一实例元数据（新） |
| `Zones() (Zones, bool)` | 60 | 已 DEPRECATED，由 InstancesV2 取代 |
| `Clusters() (Clusters, bool)` | 62 | 集群 ID |
| `Routes() (Routes, bool)` | 64 | 云路由 |
| `ProviderName() string` / `HasClusterID() bool` | — | 名称与配置校验 |

子接口定义：LoadBalancer:138、Instances:176、InstancesV2:210、Routes:246、Zones:274。
辅助：`plugins.go:79 IsExternal`（name == externalCloudProvider 即 true）、`plugins.go:99 InitCloudProvider`。

## 6. 与 kube-controller-manager 的切分
- 当前 KCM 注册表（`cmd/kube-controller-manager/app/controller_descriptor.go:174 ～ +225`）已无 service/route/cloud-node/cloud-node-lifecycle 描述符；仅保留 nodeIPAM 与 NodeLifecycle（后者是 `pkg/controller/nodelifecycle` 的 Pod 驱逐/zone 失联逻辑，不含云查询）。
- 选项层证据（`cloud-provider/options/options.go:477 ～ +482`）：in-tree cloud providers 自 v1.31（KEP-2395）禁用，非 external 的 `--cloud-provider` 直接报 ErrorForDisabledProvider。
- 职责结果：云节点初始化/生命周期、Service LB、Route 四类循环归 CCM；KCM 保留与云无关的节点驱逐与其他控制器。

## 7. 调用关系总览
```text
main → NewCloudControllerManagerCommand
 └── cloudInitializer → InitCloudProvider
      └── start*Controller
           ├── CloudNodeController: Node 事件 → syncNode
           │    ├── reconcileNodeLabels
           │    └── updateNodeAddress（云 Instances API）
           ├── CloudNodeLifecycle: MonitorNodes 轮询
           │    ├── ensureNodeExistsByProviderID
           │    └── shutdownInCloudProvider → ShutdownTaint
           ├── ServiceLB: EnsureLoadBalancer
           └── Route: CreateRoute
```

[上一篇](17-CRD扩展与聚合层.md) · [总目录](README.md) · [下一篇](19-kubeadm集群引导.md)
