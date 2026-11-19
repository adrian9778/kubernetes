[上一篇](19-kubeadm集群引导.md) · [总目录](README.md) · [下一篇](21-设备插件与网络边界.md)

# CSI 存储插件
> **层级**：第四层——函数级源码解析（kubelet 侧 CSI）。
> **范围**：driver 注册（pluginwatcher → RegistrationHandler）、卷插件装配、CSI attacher 的 Attach/MountDevice（NodeStage）、mounter 的 SetUpAt（NodePublish）与 TearDownAt（NodeUnpublish）、CSINode 维护、CSI migration 翻译。
> **不覆盖**：driver 自身（Identity/Node/Controller gRPC 服务端）、external-attacher/provisioner/snapshotter、具体 CNI/云 provider。
> **基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。

## 0. 结论速览
CSI 在 kubelet 侧是一组"插件 + 卷插件 + attacher/mounter"：
1. **driver 发现**：第三方 driver 在 `/var/lib/kubelet/plugins_registry` 暴露 registration socket → pluginwatcher → reconciler → `RegistrationHandler.RegisterPlugin` → 写入 `DriversStore`。
2. **卷挂载是两次 gRPC**：`MountVolume` → attacher 的 `MountDevice`（NodeStage，若 driver 支持 STAGE_UNSTAGE）→ mounter 的 `SetUpAt`（NodePublish，把卷挂进 Pod 目录）。
3. **缺 driver = 瞬时错误**：`csiClientGetter.Get()` 失败一律返回 `NewTransientOperationFailure`（driver 尚未就绪的常规情况，见 issue #120268），交由 reconciler 退避重试。
4. **卸载严格由 plugin 负责**：`TearDownAt` 调 `NodeUnpublishVolume`；kubelet 只清理自己写的 json 元数据与空目录，对 buggy driver 有兜底 `removeMountDir`。
5. **CSINode**：`nodeinfomanager`/`csi_node_updater` 维护 driver 的 NodeID、topology、maxAttachLimit，供 scheduler 卷拓扑过滤。

## 1. 生产视角
| 阶段 | 触发者 | 可见结果 |
|---|---|---|
| driver 注册 | driver DaemonSet 起 socket | CSINode 出现 driver 条目；DriversStore 有 endpoint |
| Pod 挂 CSI 卷 | Pod 调谐到本节点 | Pod 目录出现 bind mount；`kubectl describe` 事件 Mount/NodePublish |
| 卷 detach | Pod 删除/驱逐 | NodeUnpublish → NodeUnstage → NodeDetach；VolumeAttachment 清理 |

## 2. 模块级
| 项 | 内容 |
|---|---|
| `pkg/volume/csi/`（本分支**无** `pkg/kubelet/csi`）：csi_plugin.go（plugin+RegistrationHandler）、csi_client.go（gRPC client）、csi_mounter.go（mounter）、csi_attacher.go（attacher）、csi_block.go（block）、csi_drivers_store.go（DriversStore）、csi_node_updater.go、nodeinfomanager/ |
| `pkg/kubelet/pluginmanager/`（watcher+reconciler）、`pkg/kubelet/volumemanager/`（desired/actual+reconciler）、`pkg/volume/util/operationexecutor/` |
| CSI gRPC 类型来自 vendor `github.com/container-storage-interface/spec v1.13.0`（alias `csipbv1`）；**staging `k8s.io/csi-api` 已移除** |
| migration：`staging/src/k8s.io/csi-translation-lib` |

## 3. 类型级
| 类型 | 文件:符号 | 职责 |
|---|---|---|
| `csiPlugin` | `csi_plugin.go Struct：csiPlugin` | volume.VolumePlugin；`CSIPluginName="kubernetes.io/csi"` 偏移：+55；`host`/`volumeAttachmentLister`/`csiDriversStore` |
| `csiDriverClient` | `csi_client.go Struct：csiDriverClient 偏移：+132` | Identity/Node/Controller v1 gRPC client |
| `csiClient`（interface） | `csi_client.go Struct：csiClient 偏移：+43` | NodeGetInfo/Publish/Stage/Unpublish/Expand/GetVolumeStats |
| `csiMountMgr` | `csi_mounter.go Struct：csiMountMgr 偏移：+64` | SetUp/TearDown 执行体 |
| `csiAttacher` | `csi_attacher.go Struct：csiAttacher` | Attach/MountDevice（NodeStage）执行体 |
| `DriversStore` | `csi_drivers_store.go Struct：DriversStore 偏移：+36` | driver name → endpoint/元数据 |
| `PluginManager`（interface） | `pluginmanager/plugin_manager.go Struct：PluginManager 偏移：+36` | socket 发现与 handler 分发 |
| `VolumeManager` | `volumemanager/volume_manager.go 函数：NewVolumeManager 偏移：+195` | desired/actual & reconciler 总装配 |

## 4. driver 注册链
`RegistrationHandler`（csi_plugin.go）：
- `RegisterPlugin` 偏移：+129：校验版本（`validateVersions` +338）→ `lockDriver`（+107）→ `installDriver`（+189）：`newCsiDriverClient` 建 client、`nodeInfoFromController`（+205）判断是否需从 controller 取 NodeInfo、`updateCSIDriver`（+261）写 CSIDriver status → 存入 DriversStore。
- `syncNodeInfoFromController` 偏移：+229；`DeRegisterPlugin` 偏移：+366：从 DriversStore 删除并更新 CSINode/CSIDriver。
- `ValidatePlugin` 偏移：+116；`VerifyExhaustedResource`（+288）/`isResourceExhaustError`（+330）：attach 失败判定是否为容量耗尽。

下游：`pluginwatcher.NewWatcher`（plugin_watcher.go 偏移：+42）→ `traversePluginDir`（+114）→ `handlePluginRegistration`（+193）→ reconciler（`reconciler.go NewReconciler 偏移：+61`、`reconcile 偏移：+110`）。kubelet 接线：`kubelet.go 函数：NewMainKubelet 偏移：+1098` 调 `pluginmanager.NewPluginManager`。

## 5. attacher：Attach 与 MountDevice（NodeStage）
`csi_attacher.go`：
- `Attach` 偏移：+63：**kubelet 不做 attach**（`if _, ok := host.(KubeletVolumeHost); ok` → 报 "attaching volumes from the kubelet is not supported"）；构造 attachID（`getAttachmentName` +595）→ 等待 `VolumeAttachment`（`waitForVolumeAttachmentWithLister` +172）。inline 迁移场景用 PV spec 填充 VA source。
- `MountDevice` 偏移：+264：
  - `newCsiDriverClient` 失败 → `NewTransientOperationFailure`（#120268）。
  - `NodeSupportsStageUnstage(ctx)` 判定 driver 是否支持 NodeStage；**不支持则跳过 MountDevice，只落元数据文件**（`dataDir` 写 volHandle/driverName/specVolID/volumeLifecycleMode，供 `UnmountDevice` 用）。
  - `getPublishContext`（k8s API）取 publishContext；`NodeStageSecretRef` 存在且支持 stage → `getCredentialsFromSecret` 取 NodeStageSecrets。
  - SELinux：feature gate `SELinuxMountReadWriteOncePod` 开启且 driver 支持时，`AddSELinuxMountOption` 注入 mountOption。
  - `MkdirAllWithPathCheck(deviceMountPath, 0750)` 建 staging 目录 → 后续 `NodeStageVolume` RPC（`StageVolume`）。

## 6. mounter：SetUpAt（NodePublish）与 TearDownAt（NodeUnpublish）
`csi_mounter.go`：
- `SetUpAt` 偏移：+103：
  1. `csiClientGetter.Get()` 失败 → **瞬时错误**（#120268）。
  2. `supportsVolumeLifecycleMode`（+536）/`getFSGroupPolicy`（+509）校验 CSIDriver.Spec 声明的能力。
  3. `getSourceFromSpec` 取 volSrc/pvSrc；算 volumeHandle/readOnly/fsType/volAttribs/nodePublishSecrets。
  4. `saveVolumeData(parentDir, volDataFileName, volData)` 落元数据；失败则 `removeMountDir` 回滚。
  5. **`csi.NodePublishVolume` RPC**：调用点在 csi_mounter.go 绝对行 301，相对 SetUpAt 偏移 **+198**（SetUpAt 定义=+0 行 103）。参数 volumeHandle/readOnly/deviceMountPath/dir/accessMode/publishContext/volAttribs/nodePublishSecrets/fsType/mountOptions/nodePublishFSGroupArg。
  6. RPC 失败：`IsOperationFinishedError && !ReconstructedVolume && !IsRemount` 才 `removeMountDir`；**remount 场景跳过删除**（#121271：卷已 publish，删目录会留 stale 内容）。
- `TearDownAt` 偏移：+437：`csiClientGetter.Get()`（失败仍为瞬时错误）→ `csi.NodeUnpublishVolume(ctx, volID, dir)` → `removeMountDir`（spec 规定 target_path 由 plugin 删；kubelet 兜底删空目录）。
- 辅助：`supportsFSGroup`（+474）/`getFSGroupPolicy`（+509）/`supportsVolumeLifecycleMode`（+536）/`podServiceAccountTokenAttrs`（+363）。

## 7. 操作执行（operationexecutor）
- `operation_executor.go AttachVolume 偏移：+773`、`MountVolume 偏移：+835`。
- `operation_generator.go GenerateAttachVolumeFunc 偏移：+248`、`GenerateMountVolumeFunc 偏移：+431`（生成 mount 执行函数，调卷插件 mounter SetUp）。
- 状态机：`volumemanager/cache` desired（`NewDesiredStateOfWorld` 偏移：+151）/actual（`actual_state_of_world.go` 偏移：+266）；reconciler（`volumemanager/reconciler/reconciler.go Run 偏移：+26`、`reconcile 偏移：+33`）驱动；失败保留 desired，下轮退避重试。

## 8. CSINode 与 migration
- `csi_node_updater.go`：`NewCSINodeUpdater` 偏移：+46、`Run` +57、`onDriverAdd/Update/Delete` +73/+86/+111、`syncDriverUpdater` +122、`runPeriodicUpdate` +179。
- `nodeinfomanager/`：`NewNodeInfoManager` 偏移：+110、`InstallCSIDriver` +126、`InstallCSIDriverRegistration` +153、`UpdateCSIDriver` +237、`UninstallCSIDriver` +249、`tryUpdateCSINode` +489。
- migration（`csi-translation-lib/translate.go`）：`New` +48、`TranslateInTreeStorageClassToCSI` +54、`TranslateInTreeInlineVolumeToCSI` +67、`TranslateInTreePVToCSI` +96、`TranslateCSIPVToInTree` +112；具体驱动映射在 `plugins/`。

## 9. 流程图
```text
driver socket 出现
  └─ pluginwatcher.NewWatcher(+42) → traversePluginDir(+114) → handlePluginRegistration(+193)
       └─ reconciler.reconcile(+110) → RegistrationHandler.RegisterPlugin(+129)
            └─ installDriver(+189) → DriversStore + CSINode/CSIDriver 更新

Pod 含 CSI 卷
  └─ DesiredStateOfWorld(+151) → reconciler(+33) 差异
       ├─ MountVolume(+835) → GenerateMountVolumeFunc(+431)
       │    ├─ csiAttacher.MountDevice(+264) → NodeStageVolume（若支持 stage）
       │    └─ csiMountMgr.SetUpAt(+103) → NodePublishVolume RPC（绝对 301，偏移 +198）
       │         └─ ActualStateOfWorld(+266) 更新
       └─ 卸载：TearDownAt(+437) → NodeUnpublishVolume → removeMountDir
CSINode：csi_node_updater(+46) / nodeInfoManager(+110) 维护 NodeID/topology/maxAttachLimit
```

## 10. 边界
本仓库只含 kubelet 侧客户端与编排。driver 服务端、external-attacher/provisioner/snapshotter、CDI 实现、CNI/IPAM、运行时内部网络管理均不在本仓库；扩展点全部以 socket/gRPC 接入。

## 11. 验证与差异
- **`pkg/kubelet/csi` 与 `staging k8s.io/csi-api` 不存在**（ls/grep 零命中）。
- 行号全部经 `grep -n` 回付当前 HEAD：`csiClient` :43、`csiDriverClient` :132、`newCsiDriverClient` :192、`NodePublishVolume` :253、`SetUpAt` :103、`MountDevice` :264、`Attach` :63、`GenerateMountVolumeFunc` :431、`AttachVolume` :773。
- 与旧版差异：旧版把 SetUpAt RPC 写成绝对行号 `:282`/`:301`（实为 SetUpAt 内偏移 +301）；`csiBlockMapper` 旧写 `csi_block.go:257` 无对应函数（实为 `publishVolumeForBlock` +207 / `SetUpDevice` +280 / `MapPodDevice` +342，block 走 `NodePublishVolume` 经 `csiBlockMapper.publishVolumeForBlock`）；`VolumeManager` :191 → :195。均已修正。

[上一篇](19-kubeadm集群引导.md) · [总目录](README.md) · [下一篇](21-设备插件与网络边界.md)
