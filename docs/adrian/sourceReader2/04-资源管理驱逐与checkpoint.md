# 资源管理、驱逐与 Checkpoint

## 1. Container Manager 是聚合门面

[`pkg/kubelet/cm/container_manager.go`](../../../pkg/kubelet/cm/container_manager.go) 的 `ContainerManager` 聚合 cgroup/QoS、CPU Manager、Memory Manager、Device Manager、DRA、Topology Manager 和 podresources API。Linux 实现在 [`container_manager_linux.go`](../../../pkg/kubelet/cm/container_manager_linux.go)，负责发现 cgroup 子系统、创建 node allocatable/QoS 层级、计算 system/kube reserved，并启动子 manager。

Pod admission 期间，Topology Manager 先收集各 HintProvider 的 NUMA hints，再由 policy 合并；通过后 CPU/内存/设备 manager 执行 Allocate。容器创建后 `AddContainer` 建立 containerID 到 Pod/container 的映射；删除后 `RemoveContainer` 回收状态。

## 2. Topology Manager

[`topologymanager/topology_manager.go`](../../../pkg/kubelet/cm/topologymanager/topology_manager.go) 定义 `HintProvider` 和 `TopologyHint{NUMANodeAffinity, Preferred}`。CPU、memory、device 都可以提供一个资源到多个可行 NUMA mask 的映射。

[`policy.go`](../../../pkg/kubelet/cm/topologymanager/policy.go) 的 `HintMerger` 做笛卡尔积并比较组合。策略语义：none 不约束；best-effort 尽量选择 preferred；restricted 拒绝非 preferred；single-numa-node 要求单 NUMA。scope 可为 container 或 pod。合并结果存入 affinity store，后续 provider Allocate 必须遵守同一结果，否则“准入成功”和“实际分配”会分裂。

## 3. CPU 与内存 Manager

[`cpumanager/cpu_manager.go`](../../../pkg/kubelet/cm/cpumanager/cpu_manager.go) 的 static policy 为满足条件的 Guaranteed 整核容器分配独占 CPU。`Start` 从 `cpu_manager_state` 恢复 default CPUSet 和 assignments，调用 policy.Start 校验拓扑/保留 CPU，然后周期 `reconcileState` 调 CRI `UpdateContainerResources` 把 cpuset 压回运行时。仅写 checkpoint 不足以防外部程序改 cgroup，所以 reconcile 必不可少。

[`memorymanager/memory_manager.go`](../../../pkg/kubelet/cm/memorymanager/memory_manager.go) 保存按 NUMA 和 memory type 划分的 blocks。static policy 对符合条件的容器做保证分配；当前工作树还包含 best-effort policy。两者都在 admission 前 Allocate，并通过 checkpoint 在重启后恢复。

CPU/内存 checkpoint 初始化失败时错误明确要求 drain node 并处理 state file，这是有意 fail closed：丢失独占资源账本后继续分配可能把同一 CPU/内存块交给两个 workload。

## 4. Device Manager 与 DRA

[`devicemanager/manager.go`](../../../pkg/kubelet/cm/devicemanager/manager.go) 维护 device plugin 注册端点、健康设备、allocatedDevices、PodDevices 和 reusable init devices。Allocate 调 device plugin，保存返回的 device IDs、NUMA、mount/env/CDI 信息，再写 checkpoint。`checkpoint/checkpoint.go` 的数据由 checksum 保护。

[`dra/manager.go`](../../../pkg/kubelet/cm/dra/manager.go) 管理 ResourceClaim 的 Prepare/Unprepare 和 claim cache。源码特意先持久化关键状态再推进外部动作，或在注释中说明 checkpoint 成功前重启的语义。DRA 还把设备健康写入 `AllocatedResourcesStatus`。

## 5. 通用 checkpoint 机制

[`checkpointmanager/checkpoint_manager.go`](../../../pkg/kubelet/checkpointmanager/checkpoint_manager.go) 只做四件事：Marshal 后原子写文件；读文件并 Unmarshal；VerifyChecksum；删除/列举。领域格式、版本兼容和 checksum 字段由具体 checkpoint 类型负责。

正确恢复顺序是：读取并校验；与当前机器拓扑和 policy 名校验；恢复内存 state；扫描 CRI 容器建立 ID 映射；清理只属于已确认不存在 Pod 的陈旧记录；启动 reconcile。不能在 sources 尚未 ready 时清掉“看起来孤儿”的分配，否则 apiserver Pod 还没 list 完就会误释放资源。

## 6. Eviction Manager 控制环

[`pkg/kubelet/eviction/eviction_manager.go`](../../../pkg/kubelet/eviction/eviction_manager.go) 的 `synchronize` 是主循环：

1. 从 Summary Provider 取得 memory、nodefs、imagefs、containerfs、PID 数据。
2. 把配置 threshold 转为绝对 quantity，与 available 比较。
3. 记录 `thresholdsFirstObservedAt`，软阈值等待 grace period。
4. 维护 node condition 最近观察时间，使用 transition period 防抖。
5. 对尚未达到 `minReclaim` 的旧压力继续视为未解决。
6. 先做节点级 reclaim，例如 container GC、image GC。
7. 仍有压力则按 signal 选 Pod 排序函数。
8. 每轮最多驱逐一个 Pod，然后等待 Pod cleanup。

Linux 内存阈值还可由 [`threshold_notifier_linux.go`](../../../pkg/kubelet/eviction/threshold_notifier_linux.go) 和 `memory_threshold_notifier.go` 基于 cgroup notifier 提前唤醒，而不是只等 housekeeping interval。

## 7. 排序和终止语义

[`helpers.go`](../../../pkg/kubelet/eviction/helpers.go) 为不同 signal 选择不同 ranking：内存压力综合是否超过 request、priority、使用量；磁盘压力按相关 filesystem 上的使用量；PID 压力按 PID 使用。imagefs/containerfs 是否独立决定统计和 reclaim 函数映射。

hard threshold 的 grace 为 0，kill override 通常接近立即终止；soft threshold 使用 `MaxPodGracePeriodSeconds` 与 Pod 自身 grace 的较小值。critical/static Pod 受保护。`evictPod` 通过 `killPodFunc` 进入正常 podWorkers 状态机，同时把 phase/reason/message/condition 写成 Evicted，而不是绕过生命周期直接调用 CRI。

本地临时存储还有三条直接检查：emptyDir sizeLimit、Pod 总 ephemeral-storage limit、单容器 ephemeral-storage limit。它们满足时可立即驱逐，不必等待节点整体 DiskPressure。

## 8. v1.37 资源侧注意点

- `ContainerManager` 已暴露 Pod 级分配和状态更新路径，不能只按 container 建模。
- `kuberuntime_manager.go` 的 `podActions` 包含 Pod/Container CPU、内存及 emptyDir resize；CRI 更新失败有回滚，成功后更新 actuated checkpoint。
- cgroup v1 明确弃用；最小复现优先实现 cgroup v2 unified hierarchy。
- checkpoint 是节点本地正确性协议，不是普通缓存。checksum 或 policy 不匹配应阻止不安全分配。

