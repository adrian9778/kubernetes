# 13. Volume、CSI、Cgroup、资源管理与驱逐

## 13.1 Volume Plugin 边界

`pkg/volume` 定义 VolumePlugin、Mounter、Unmounter、Attacher、Detacher、Provisioner 等能力。PluginMgr 探测并按 spec 匹配插件。大量 in-tree provider 已迁移 CSI，csimigration 将旧 API 名称转换到 CSI driver。

## 13.2 Volume Manager 的双状态

Kubelet Volume Manager 持有 DesiredStateOfWorld 和 ActualStateOfWorld。Desired populator 从 Pod 推导需挂载 volumes；reconciler 比较两者并调用 operation executor 执行 attach/mount/unmount/detach。

NestedPendingOperations 防同一 volume/pod/node 冲突操作，并对失败退避。实际状态不是简单 API 对象，还依赖 mount table、device path 和 plugin 查询。

## 13.3 Attach/Detach Controller

控制面 AttachDetach Controller 也维护 desired/actual state，决定某 volume 应附着哪个 Node，创建/删除 VolumeAttachment 或调用 in-tree attacher。Kubelet 与控制器通过 Node、Pod、VolumeAttachment 和实际设备状态最终一致。

## 13.4 CSI 调用链

```text
PVC -> PV/CSI source
 -> scheduler VolumeBinding
 -> external provisioner / PV binder
 -> VolumeAttachment / external attacher
 -> kubelet CSI plugin registration
 -> NodeStageVolume
 -> NodePublishVolume
 -> container mount
 -> NodeUnpublish / NodeUnstage
```

CSI sidecars 多在外部仓库，Kubernetes 本仓负责 API、scheduler/kubelet integration、migration 和 plugin registration。

## 13.5 Secret、ConfigMap 与 Projected Volume

这些 volume 由 kubelet manager 获取对象并原子更新 payload。AtomicWriter 使用版本目录和符号链接切换，减少容器看到半写文件。SubPath mount 通常不会随源对象更新，且要防路径穿越。

## 13.6 PVC Binding

Immediate mode 可在 Pod 调度前绑定；WaitForFirstConsumer 延迟到 Scheduler 选择拓扑。VolumeBinding plugin 在 PreFilter/Filter/Reserve/PreBind 协调 PV 匹配、assume 和 API bind，失败要回滚 assumed binding。

## 13.7 Cgroup 与 QoS

Container Manager 建立节点、Pod、container cgroup，应用 CPU/memory/pids 等限制。QoS class 由 requests/limits 推导，影响 cgroup 和 eviction 排序。Node allocatable 从 capacity 减 kube/system reserved 和 eviction reservation。

## 13.8 CPU、Memory 与 Topology Manager

CPU Manager 为 Guaranteed containers 等分配独占 CPU 并 checkpoint；Memory Manager 管理 NUMA memory assignment；Device Manager 管理 device plugin resources；Topology Manager 汇总 hint providers，在 NUMA 策略下选择亲和结果。

各 manager 的 checkpoint 让 Kubelet 重启后恢复分配。checkpoint 损坏通常不能静默忽略，否则可能重复分配独占资源。

## 13.9 DRA 节点侧

DRA plugin manager 发现节点插件；allocation manager/checkpointer 记录 claim 和 device prepare 状态。Kubelet 在启动容器前确认 claim 已分配并执行 PrepareResources，终止后按使用关系 Unprepare。插件 socket 重连和 Kubelet 重启必须可恢复。

## 13.10 Eviction Manager

观察 memory.available、nodefs/imagefs/containerfs available/inodes、pid.available 等信号。Soft threshold 带 grace period，hard threshold 立即触发。Manager 更新 Node pressure conditions，按 QoS、超量使用和优先级选择 Pod，执行本地 eviction。

## 13.11 Node Shutdown

Graceful Node Shutdown 监听 OS shutdown，按 priority/grace 配置终止 Pods，并设置状态。它与普通 API 删除、eviction 和突然断电不同，需要 checkpoint/状态避免重启后混乱。

## 13.12 重建建议

先实现纯内存 DSW/ASW reconciler和串行 operation executor；再接 fake mount/device；加入 checkpoint 和故障注入；最后接 CSI gRPC、cgroup 和真实系统调用。所有清理动作必须幂等。

