[上一篇](19-kubeadm集群引导.md) · [总目录](README.md) · [下一篇](21-设备插件与网络边界.md)

# CSI 存储插件
> **层级**：第四层——函数级源码解析。
> **范围**：kubelet 侧 CSI 插件、plugin manager 注册、volume manager 期望/实际状态调谐、到 NodePublishVolume gRPC 的完整链路。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 卷插件目录 | **`pkg/volume/csi/`**（本分支无 pkg/kubelet/csi）：csi_plugin.go、csi_client.go、csi_mounter.go、csi_block.go、csi_attacher.go、csi_drivers_store.go、nodeinfomanager/ |
| 插件管理 | `pkg/kubelet/pluginmanager/`（监听 plugins_registry） |
| 卷管理 | `pkg/kubelet/volumemanager/`（desired/actual state + reconciler） |
| 操作执行 | `pkg/volume/util/operationexecutor/` |
| CSI gRPC 定义 | 上游 `github.com/container-storage-interface/spec v1.13.0`（vendor；本分支 staging 的 `k8s.io/csi-api` 已移除） |
| 迁移翻译 | `[staging] k8s.io/csi-translation-lib` |
| 数据流 | Pod 的 CSI 卷 → desired state → reconciler → Attach/Mount 操作 → CSI mounter → NodeStage/NodePublish gRPC → actual state 更新 |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `csiPlugin` | `pkg/volume/csi/csi_plugin.go` | 实现 volume.VolumePlugin；常量 `CSIPluginName = "kubernetes.io/csi"`（:54），持有 RegistrationHandler |
| `csiDriverClient` | `csi_client.go` | 封装 CSI Identity/Node/Controller v1 gRPC client |
| `csiClient`（interface） | `csi_client.go:41` | NodeGetInfo、NodePublishVolume、NodeStageVolume、NodeUnpublishVolume、NodeExpandVolume、NodeGetVolumeStats |
| `csiMountMgr` | `csi_mounter.go` | 卷的 SetUp/NodePublish 执行体，缓存 csiClient |
| `DriversStore` | `csi_drivers_store.go` | driver name → endpoint/元数据 |
| PluginManager | `pluginmanager/plugin_manager.go:55` | 插件 socket 发现与 handler 分发 |
| VolumeManager | `volumemanager/volume_manager.go:191` | 期望/实际状态与 reconciler 的总装配 |

## 3. 插件注册函数级

### 函数：ProbeVolumePlugins / RegistrationHandler
**源码：** `pkg/volume/csi/csi_plugin.go`
**职责：** ProbeVolumePlugins 向 kubelet 注册 CSI volume plugin；RegistrationHandler 的 RegisterPlugin（:105）/DeRegisterPlugin（:118）/ValidatePlugin（:270）处理 driver socket 上下线，写入 DriversStore。

### 函数：NewPluginManager / Run / AddHandler
**源码：** `pluginmanager/plugin_manager.go:55 / :109 / :129`
**监听目录：** `DefaultKubeletPluginsRegistrationDirName = "plugins_registry"`（`kubeletconfig/defaults.go:26`，完整路径 `/var/lib/kubelet/plugins_registry`）。
**内部链路：** `pluginwatcher.NewWatcher`（plugin_watcher.go:42）fsnotify 监听 socket 文件 → traversePluginDir（:114）→ handlePluginRegistration（:193）→ reconciler（`reconciler.go:61 NewReconciler`，:110 reconcile）回调 CSI RegistrationHandler。
kubelet 接线处：`kubelet.go:1065`。

## 4. 卷管理函数级

### 函数：NewVolumeManager / Run
**源码：** `volumemanager/volume_manager.go:191 / :319`
**组成：**
- DesiredStateOfWorld（`cache/desired_state_of_world.go:151 NewDesiredStateOfWorld`）：populator 从 podManager 中 Pod 的卷声明生成期望（应 attach、应 mount）。
- ActualStateOfWorld（`cache/actual_state_of_world.go:226`）：已 attach/mount 的实际记录。
- reconciler（`reconciler/reconciler.go:26 Run`，:33 reconcile）：周期对比两个世界，把差异交给 operationExecutor。

### 操作执行函数
| 函数 | 行 | 职责 |
|---|---|---|
| `operationExecutor.AttachVolume` | `operationexecutor/operation_executor.go:772` | 异步发起 attach（external-attacher 场景或 CSI ControllerPublish 回退） |
| `operationExecutor.MountVolume` | :834 | 异步发起 mount（NodeStage + NodePublish） |
| `GenerateAttachVolumeFunc` | `operation_generator.go:250` | 生成 attach 执行函数 |
| `GenerateMountVolumeFunc` | :433 | 生成 mount 执行函数：调卷插件 mounter SetUp |
**状态更新：** 操作完成（或失败）后写 ActualStateOfWorld；失败保留 desired，下个 reconcile 周期带退避重试。

## 5. CSI gRPC 函数级

### 函数：newCsiDriverClient
**源码：** `pkg/volume/csi/csi_client.go:170`
**职责：** 按 driver name 从 DriversStore 取 endpoint，grpc 拨号，构造 `csipbv1.IdentityClient`/`NodeClient`/`ControllerClient`（类型来自 vendor CSI spec，alias csipbv1）。本分支无导出的 NewCsiDriverClient。

### 函数：(*csiDriverClient).NodePublishVolume
**源码：** `csi_client.go:228`
**执行流程：** 组装 `csipbv1.NodePublishVolumeRequest`（volume id、target path、fsType、volume context、readonly、secrets）（:265）→ `nodeClient.NodePublishVolume(ctx, req)`（:299）。
**调用方：** `csiMountMgr.SetUpAt`（csi_mounter.go:282，入口 :103；RPC 在 :301）；块设备路径在 `csi_block.go:257`。client 惰性创建并缓存。

### NodeStage / 其他
- NodeStageVolume：经 csi_attacher/ mounter 在 NodePublish 前对块设备做 staging（一个卷多 Pod 共享时先 stage 再各 publish）。
- NodeUnpublishVolume、NodeExpandVolume、NodeGetVolumeStats：分别承担卸载、扩容、容量上报（kubelet 驱逐与 PV 容量监控使用）。

## 6. CSINode 与迁移翻译

### CSINode 维护
- `csi_node_updater.go` 与 `nodeinfomanager/`：NodeGetInfo 得到 driver 的 NodeID 与 topology keys，写入/更新 `CSINode` 对象（driver 列表、allocatable 卷数），scheduler 的卷拓扑与卷数插件据此过滤。

### CSI Migration 翻译
**源码：** `[staging] csi-translation-lib/translate.go`
| 函数 | 行 | 职责 |
|---|---|---|
| `New() CSITranslator` | 48 | 构造翻译器 |
| `TranslateInTreePVToCSI` | 96 | in-tree PV 参数 → CSI 临时 PV（驱动运行时透明走 CSI） |
| `TranslateInTreeStorageClassToCSI` | 54 | StorageClass 参数翻译 |
| `TranslateInTreeInlineVolumeToCSI` | 67 | Pod inline 卷翻译 |
| `TranslateCSIPVToInTree` | 112 | 反向翻译 |
具体 in-tree 驱动映射在 `plugins/`；启用 feature gate 后旧卷插件的操作被翻译发往对应 CSI driver。

## 7. 调用关系总览
```text
driver socket 出现 → pluginwatcher → reconciler → CSI RegistrationHandler（DriversStore）
Pod 含 CSI 卷 → DesiredStateOfWorld
 ↓ reconciler 发现差异
operationExecutor
 ├── AttachVolume → GenerateAttachVolumeFunc（csi_attacher：ControllerPublish）
 └── MountVolume → GenerateMountVolumeFunc
      └── csiMountMgr.SetUpAt
           ├── newCsiDriverClient（grpc NodeClient）
           ├── NodeStageVolume（需要时）
           └── NodePublishVolume（target path 挂入 Pod 目录）
 ↓
ActualStateOfWorld 更新；CSINode 经 node updater 维护
```

## 8. 边界
本仓库只包含 kubelet 侧客户端与编排；driver 自身（Identity/Node/Controller gRPC 服务端实现、与存储后端通信）由第三方部署为 DaemonSet/StatefulSet。Controller 侧的 provision/snapshot（external-provisioner/snapshotter）同样在仓库之外。

[上一篇](19-kubeadm集群引导.md) · [总目录](README.md) · [下一篇](21-设备插件与网络边界.md)
