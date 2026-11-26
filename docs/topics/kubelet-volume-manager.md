[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# kubelet volume manager

> **层级**：跨篇机制纵深专题——把 kubelet **volume manager** 的卷挂载调谐讲透：desired/actual state 双状态模型、reconciler 调谐循环（unmount→mountOrAttach→expand→unmountDetach）、operationexecutor 执行卷操作。
>
> **代码基准**：`adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，工作区仅 `.codegraph/` 未跟踪），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `rc.reconcile+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[09-kubelet同步循环](../sourceReader/09-kubelet同步循环.md) 提到 volumeManager；本专题纵向展开卷挂载调谐的双状态模型与 reconciler 语义。

---

## 0. 结论速览

### 0.1 volume manager 解决什么问题

Pod 声明的卷（Volume）需要**先 attach**（挂到节点）**再 mount**（挂到容器）。volume manager 负责这个两阶段调谐：比对「期望卷状态」（Pod 声明）与「实际卷状态」（节点现状），执行 attach/detach/mount/unmount。

### 0.2 关键结论

1. **双状态模型**：`desiredStateOfWorld`（Pod 声明的期望卷）vs `actualStateOfWorld`（节点实际挂载状态）——reconciler 驱动两者收敛。
2. **reconciler 调谐循环**：周期性 unmount 多余卷 → mount/attach 缺失卷 → expand 扩容卷 → unmount/detach 已删卷。
3. **两阶段挂载**：attach（卷挂到节点）→ mount（卷挂到容器），detach/unmount 反向。
4. **populator 异步填充 desired**：`desiredStateOfWorldPopulator` 周期从 Pod manager 同步期望卷到 desiredStateOfWorld。
5. **operationexecutor 执行**：实际 attach/mount/unmount/detach 由 operationexecutor 执行（封装 CSI/in-tree 卷插件）。

---

## 1. 生产视角：卷挂载对外行为

### 1.1 Pod 卷挂载的可见结果

| 阶段 | 动作 | 可见结果 |
|---|---|---|
| attach | 卷挂到节点（如挂云盘） | VolumeAttachment 对象 |
| mount | 卷挂到容器路径 | 容器内可访问卷数据 |
| unmount/detach | Pod 删除时反向 | 卷释放 |

### 1.2 可观测信号

- Pod 事件：`FailedMount`/`FailedAttachVolume`。
- Pod condition：`ContainersReady` 卡住（卷未就绪）。

---

## 2. 开发视角：volume manager 骨架

### 2.1 `NewVolumeManager`（volume_manager.go `NewVolumeManager+0`，:195）

构造 volumeManager，持有 desiredStateOfWorld、actualStateOfWorld、reconciler、desiredStateOfWorldPopulator、operationExecutor。

### 2.2 `Run`（volume_manager.go `vm.Run+0`，:326）

启动 reconciler（`go rc.Run`）+ desiredStateOfWorldPopulator（异步填充 desired）。

### 2.3 对 kubelet 的接口

- `WaitForAttachAndMount+0`（:429）：SyncPod 前等待 Pod 的卷 attach+mount 完成。
- `GetMountedVolumesForPod+0`（:351）：取 Pod 已挂载的卷（容器创建用）。

---

## 3. 开发视角：双状态模型

### 3.1 desiredStateOfWorld（期望卷）

`desiredStateOfWorld`（cache/desired_state_of_world.go）记录「Pod 声明需要哪些卷」：
- `AddPodToVolume+0`（:261）：Pod 声明需要某卷。
- `PodExistsInVolume+0`（:526）：Pod 是否已在某卷的挂载集合。

### 3.2 actualStateOfWorld（实际卷）

`actualStateOfWorld`（cache/actual_state_of_world.go）记录「节点实际挂载了哪些卷」：
- `AddPodToVolume+0`（:841）：标记某卷已挂到某 Pod。
- `PodExistsInVolume+0`（:1082）：Pod 是否真的挂在某卷上。

**收敛目标：** reconciler 驱动 actual → desired（多挂的卸载，缺的挂上）。

### 3.3 populator 异步填充 desired

`desiredStateOfWorldPopulator`（populator/desired_state_of_world_populator.go `NewDesiredStateOfWorldPopulator+0`，:96）：周期从 Pod manager 取 Pod 声明，同步到 desiredStateOfWorld（ReconcilePodVolumes）。

---

## 4. 开发视角：reconciler 调谐循环

### 4.1 `reconciler.Run` → `reconcile`（reconciler.go `rc.Run+0`，:26 → `rc.reconcile+0`，:33）

```text
Run(ctx)                                          rc.Run+0
└── for 周期:
     └── reconcile(ctx)                           rc.reconcile+0
          ├── unmountVolumes(logger)              rc.unmountVolumes+0 (reconciler_common.go:155)
          │     ↑ 卸载不该挂的卷（Pod 删除/卷移除）
          ├── mountOrAttachVolumes(logger)        rc.mountOrAttachVolumes+0 (:176)
          │     ↑ 对缺失的卷：attach（未 attach）或 mount（已 attach 未 mount）
          ├── expandVolume(...)                   rc.expandVolume+0 (:201)
          │     ↑ 卷扩容（在线扩容）
          ├── mountAttachedVolumes(...)           rc.mountAttachedVolumes+0 (:214)
          │     ↑ 已 attach 但未 mount 的卷 → mount
          ├── waitForVolumeAttach(...)            rc.waitForVolumeAttach+0 (:239)
          └── unmountDetachDevices(logger)        rc.unmountDetachDevices+0 (:280)
                ↑ 卸载并 detach 已删除 Pod 的卷
```

### 4.2 两阶段挂载（attach → mount）

| 阶段 | 操作 | 执行者 |
|---|---|---|
| attach | 卷挂到节点 | operationexecutor.AttachVolume（卷插件 Attach） |
| mount | 卷挂到容器 | operationexecutor.MountVolume（卷插件 SetUp） |
| unmount | 从容器卸载 | operationexecutor.UnmountVolume |
| detach | 从节点卸载 | operationexecutor.DetachVolume |

operationexecutor（pkg/volume/util/operationexecutor/）封装卷插件调用，执行实际 attach/mount/unmount/detach。

### 4.3 调谐顺序（先卸后挂）

reconcile 先 `unmountVolumes`（清理多余的）再 `mountOrAttachVolumes`（补缺失的）——先释放资源再申请，避免资源冲突。

---

## 5. 全链路时序（Pod 卷挂载）

```text
Pod 调度到节点 → kubelet SyncPod → WaitForAttachAndMount (volume_manager.go:429)
└── populator 同步 Pod 声明到 desiredStateOfWorld
     └── reconciler.reconcile (reconciler.go:33) 周期调谐
          ├── desired 有、actual 无 → mountOrAttachVolumes (:176)
          │    └── operationexecutor.AttachVolume → 卷挂节点
          │         └── operationexecutor.MountVolume → 卷挂容器
          ├── actual 有、desired 无 → unmountVolumes (:155) 卸载
          └── Pod 删除 → unmountDetachDevices (:280) 卸载 + detach
               └── 卷就绪 → SyncPod 创建容器
```

---

## 6. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| 双状态收敛 | `rc.reconcile+0`（desired vs actual） | 实际挂载向期望收敛 |
| 先卸后挂 | `rc.unmountVolumes+0` → `rc.mountOrAttachVolumes+0` | 先释放再申请，避免冲突 |
| 两阶段挂载 | attach → mount（operationexecutor） | 卷先挂节点再挂容器 |
| 异步填充 desired | `NewDesiredStateOfWorldPopulator+0` | 期望卷与 Pod 声明同步 |
| SyncPod 等待卷就绪 | `vm.WaitForAttachAndMount+0` | 容器创建前卷已挂好 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- volume_manager.go：`NewVolumeManager+0`（:195）、`vm.Run+0`（:326）、`vm.GetMountedVolumesForPod+0`（:351）、`vm.WaitForAttachAndMount+0`（:429）
- reconciler.go：`rc.Run+0`（:26）、`rc.reconcile+0`（:33）
- reconciler_common.go：`rc.unmountVolumes+0`（:155）、`rc.mountOrAttachVolumes+0`（:176）、`rc.expandVolume+0`（:201）、`rc.mountAttachedVolumes+0`（:214）、`rc.waitForVolumeAttach+0`（:239）、`rc.unmountDetachDevices+0`（:280）
- cache：`dsw.AddPodToVolume`（desired_state_of_world.go:261）、`dsw.PodExistsInVolume`（:526）、`asw.AddPodToVolume`（actual_state_of_world.go:841）、`asw.PodExistsInVolume`（:1082）
- populator：`NewDesiredStateOfWorldPopulator+0`（:96）

### 7.2 生产实测

**未实测** `[待运行验证]`。reconciler 调谐周期、attach/mount 实际时序、卷扩容行为均为代码推断，未在真实节点观测卷挂载过程。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| operationexecutor 全文 | 未深挖 | attach/mount/unmount/detach 各操作的状态机未逐行读 |
| 卷插件接口 | 未深挖 | CSI/in-tree 卷插件的 Attach/SetUp 细节未展开 |
| 卷扩容 | 未深挖 | `expandVolume` 的在线扩容流程未读 |
| reconstruct | 未深挖 | kubelet 重启后重建 actual state（reconstruct.go）未读 |
| SELinux metrics | 未深挖 | SELinux 卷指标未读 |

### 7.4 修改联动

- 改调谐循环 → `rc.reconcile`/`unmountVolumes`/`mountOrAttachVolumes`，同步本文 §4。
- 改双状态 → desired/actual state cache，同步 §3。
- 改卷执行 → operationexecutor，同步 §4.2。

**关联文档：** [09-kubelet同步循环](../sourceReader/09-kubelet同步循环.md)（kubelet 主循环）、[kubelet同步循环与PLEG](kubelet同步循环与PLEG.md)（SyncPod 等卷）、[20-CSI存储插件](../sourceReader/20-CSI存储插件.md)（CSI 卷）。

[总目录](../sourceReader/README.md)
