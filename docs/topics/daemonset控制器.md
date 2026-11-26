[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# DaemonSet 控制器

> **层级**：跨篇机制纵深专题——把 DaemonSet 控制器讲透：**节点-Pod 一一对应**、节点准入判定、节点亲和性注入、surge 滚动更新。
>
> **代码基准**：`adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `dsc.syncDaemonSet+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[14-daemonset控制器](../sourceReader/14-daemonset控制器.md) 是浓缩版；本专题纵向展开节点准入、亲和注入与 surge 语义。

---

## 0. 结论速览

### 0.1 DaemonSet 解决什么问题

DaemonSet 保证**每个（符合条件的）节点上恰好跑一个 Pod 副本**——用于日志采集、监控 agent、存储守护进程等「每节点一个」的场景。

### 0.2 关键结论

1. **节点-Pod 一一对应**：每个符合准入条件的节点上一个 Pod；新节点加入自动建 Pod，节点移除自动删 Pod。
2. **绑定靠节点亲和性注入**：DaemonSet 控制器给 Pod 注入 `kubernetes.io/hostname` 的 `RequiredDuringScheduling` 亲和性（`ReplaceDaemonSetPodNodeNameNodeAffinity`），由**默认调度器**把 Pod 固定到目标节点——不设自定义 schedulerName。
3. **三层准入条件**：nodeSelector/required 亲和性、taint/toleration（含 NoExecute）、模板 `nodeName`。
4. **surge 滚动更新**：`RollingUpdate` 按 maxSurge/maxUnavailable 控制节奏（与 Deployment 不同，DaemonSet 按节点数算）。
5. **节点准入即时响应**：节点 taint/标签变化会触发 Pod 增删（`podsShouldBeOnNode`）。

### 0.3 DaemonSet vs Deployment 滚动更新

| | DaemonSet | Deployment |
|---|---|---|
| 单位 | 按**节点数** | 按**副本数** |
| 目标 | 每符合条件节点一个新 Pod | 总副本数达标 |
| surge | maxSurge 按节点数 | maxSurge 按副本数 |

---

## 1. 生产视角：对外行为

### 1.1 用户操作与可见结果

| 操作 | 结果 |
|---|---|
| 创建 DaemonSet | 每个符合准入条件的节点上一个 Pod |
| 新节点加入（且满足准入） | 自动在新节点建 Pod |
| 节点加 taint（无对应 toleration） | 该节点 Pod 被删 |
| 改 Pod 模板 | 滚动更新（RollingUpdate/OnDelete） |

### 1.2 可观测信号

- DaemonSet `status.desiredNumberScheduled`/`currentNumberScheduled`/`numberReady`。
- 事件：`SuccessfulCreate`/`SuccessfulDelete`。

---

## 2. 开发视角：控制器骨架

### 2.1 构造与运行（daemon_controller.go）

`NewDaemonSetsController+0`（:158）注册四类 informer handler（Node 事件进 `nodeUpdateQueue`，DS/Pod/ControllerRevision 进主 queue），赋值 `syncHandler=syncDaemonSet`。`dsc.Run+0`（:359）起 worker。

### 2.2 worker 循环（`dsc.processNextWorkItem+0`，:401）

```text
processNextWorkItem
├── key = queue.Get()
└── syncHandler(key)（= syncDaemonSet）
```

---

## 3. 开发视角：调谐核心（`syncDaemonSet`）

### 3.1 `syncDaemonSet`（daemon_controller.go `dsc.syncDaemonSet+0`，:1281）

```text
syncDaemonSet(ctx, key)                              dsc.syncDaemonSet+0
├── 取 DaemonSet；取节点列表
├── getNodesToDaemonPods(ctx, ds, ...)               dsc.getNodesToDaemonPods+0 (:796)
│     ↑ 建节点→Pod 映射（含已删除 terminal Pod 可选）
├── constructHistory(...)                            dsc.constructHistory+0 (update.go:299)
│     ↑ 构造/取 ControllerRevision 历史
├── updateDaemonSet(ctx, ds, nodeList, hash, key, old)  dsc.updateDaemonSet+0 (:973)
│     ↑ 滚动更新（若 RollingUpdate）
└── manage(ctx, ds, nodeList, hash)                  dsc.manage+0 (:1003)
      ↑ 逐节点比对，算增删
```

### 3.2 `manage`——逐节点比对（`dsc.manage+0`，:1003）

```text
manage(ctx, ds, nodeList, hash)
├── for each node:
│     └── podsShouldBeOnNode(node, ds)               dsc.podsShouldBeOnNode+0 (:849)
│          ↑ 该节点是否应有 Daemon Pod
├── 算 podsToDelete（不该在的节点上的 Pod）+ nodesNeedingDaemonPods（缺 Pod 的节点）
└── syncNodes(ctx, ds, podsToDelete, nodesNeedingDaemonPods, hash)   dsc.syncNodes+0 (:1040)
      ↑ 实际创建/删除 Pod
```

---

## 4. 开发视角：节点准入判定

### 4.1 `podsShouldBeOnNode`（daemon_controller.go `dsc.podsShouldBeOnNode+0`，:849）

```text
podsShouldBeOnNode(node, ds)
└── nodeShouldRunDaemonPod(logger, node, ds, tolerations, requiredNodeAffinity)
     ↑ nodeShouldRunDaemonPod+0 (:1413)
     shouldRun, shouldContinueRunning = ...
```

### 4.2 `nodeShouldRunDaemonPod`——三层准入（daemon_controller.go `nodeShouldRunDaemonPod+0`，:1413）

```text
nodeShouldRunDaemonPod(node, ds, tolerations, requiredNodeAffinity)
├── 1. nodeSelector / requiredDuringScheduling 亲和性
├── 2. taint/toleration（含 NoExecute；无对应 toleration → 不跑/驱逐）
└── 3. 模板 nodeName（若指定则必须等于该节点名）
└── 返回 (shouldRun, shouldContinueRunning)
```
**即时响应：** 节点 taint/标签变化 → `nodeUpdateQueue` → 重新调谐 → 该节点 Pod 增删。

### 4.3 节点亲和性注入（`ReplaceDaemonSetPodNodeNameNodeAffinity`）

`ReplaceDaemonSetPodNodeNameNodeAffinity+0`（daemonset_util.go :176）：创建 Daemon Pod 时把 `kubernetes.io/hostname=<node>` 写成 `RequiredDuringScheduling` 亲和性（替换已有 nodeName 亲和性）。
**意义：** Pod 由**默认调度器**绑定到目标节点（非 DaemonSet 直接绑），保留调度器的一致行为。

---

## 5. 开发视角：surge 滚动更新（`rollingUpdate`）

### 5.1 `rollingUpdate`（update.go `dsc.rollingUpdate+0`，:46）

```text
rollingUpdate(ctx, ds, nodeList, hash)              dsc.rollingUpdate+0
├── updatedDesiredNodeCounts(...)                   dsc.updatedDesiredNodeCounts+0 (:590)
│     ↑ 算 maxSurge / maxUnavailable / desiredNumberScheduled
├── maxSurge == 0（默认 OnDelete 外的 RollingUpdate）:
│     └── 删足够旧 Pod，使 unavailable < maxUnavailable
├── maxSurge > 0:
│     └── 允许超节点数建新 Pod（surge），再缩旧
└── 按 maxUnavailable 预算逐步推进
```

### 5.2 历史管理

- `constructHistory+0`（update.go :299）：构造/取 ControllerRevision。
- `snapshot+0`（update.go :528）：给当前模板拍 revision 快照。
- 每个 revision 对应一个 ControllerRevision，用于回滚。

---

## 6. 全链路时序（新节点加入）

```text
新节点加入（满足准入）
└── Node informer → nodeUpdateQueue → syncDaemonSet (daemon_controller.go:1281)
     ├── getNodesToDaemonPods (:796)  节点→Pod 映射
     └── manage (:1003)
          ├── podsShouldBeOnNode (:849) → nodeShouldRunDaemonPod (:1413) 判应跑
          └── syncNodes (:1040)
               └── ReplaceDaemonSetPodNodeNameNodeAffinity (daemonset_util.go:176) 注入 hostname 亲和
                    └── 创建 Daemon Pod → 默认调度器绑定到该节点
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在基准 `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 上复核（上一基准 `782c049`，2026-11-19）：
- `NewDaemonSetsController+0`（daemon_controller.go:158）、`dsc.Run+0`（:359）、`dsc.processNextWorkItem+0`（:401）
- `dsc.getNodesToDaemonPods+0`（:796）、`dsc.podsShouldBeOnNode+0`（:849）、`nodeShouldRunDaemonPod+0`（:1413）、`dsc.updateDaemonSet+0`（:973）、`dsc.manage+0`（:1003）、`dsc.syncNodes+0`（:1040）、`dsc.syncDaemonSet+0`（:1281）
- `ReplaceDaemonSetPodNodeNameNodeAffinity+0`（daemonset_util.go:176）
- `dsc.rollingUpdate+0`（update.go:46）、`dsc.updatedDesiredNodeCounts+0`（:590）、`dsc.constructHistory+0`（:299）、`dsc.snapshot+0`（:528）

### 7.2 生产实测

**未实测** `[待运行验证]`。surge 滚动节奏（按节点数）、节点准入即时响应时序、亲和注入后调度器绑定行为均为代码推断，未在真实集群观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| syncNodes 全文 | 未逐行读 | :1040 起的创建/删除并发与限流细节未展开 |
| updatedDesiredNodeCounts | 未深挖 | maxSurge/maxUnavailable 按节点数换算细节未读 |
| OnDelete 更新 | 未深挖 | 手动删除触发的更新路径未展开 |
| 历史清理 | 未深挖 | `cleanupHistory`（update.go:356）的 revisionHistoryLimit 清理未读 |

### 7.4 修改联动

- 改准入判定 → `nodeShouldRunDaemonPod`/`podsShouldBeOnNode`，同步本文 §4。
- 改亲和注入 → `ReplaceDaemonSetPodNodeNameNodeAffinity`，同步 §4.3。
- 改 surge → `rollingUpdate`/`updatedDesiredNodeCounts`，同步 §5。

### 7.5 基准同步核对（782c049 → 6bf0bfd5）

`782c049..6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 抽查结论：**本专题锚点零漂移**，仅更新头部基准，未改写任何符号锚点。

| 核对项 | 结果 |
|---|---|
| `pkg/controller/daemon/**`（含 `daemon_controller.go`、`update.go`、`util/daemonset_util.go`） | 漂移区间内 **0 个 `.go` 变更**，全部符号定义行与上一基准一致 |
| `DaemonSetsController` / `syncDaemonSet` / `manage` / `podsShouldBeOnNode` / `getNodesToDaemonPods` / `updateDaemonSet` / `syncNodes` / `nodeShouldRunDaemonPod` | codegraph 抽查定义行不变（如 `syncDaemonSet` 仍 1281、`manage` 仍 1003、`podsShouldBeOnNode` 仍 849） |
| `rollingUpdate` / `updatedDesiredNodeCounts` / `constructHistory` / `snapshot` / `cleanupHistory`（update.go） | surge/maxUnavailable 语义与定义行均未变 |
| `ReplaceDaemonSetPodNodeNameNodeAffinity`（daemonset_util.go） | 定义行不变（仍 176） |
| `Const:StaleControllerConsistencyDaemonSet`（`pkg/features/kube_features.go`） | **上一基准已存在**（原 1116 行，现 1157 行，仅因其他 gate 新增而整体下移）。版本化规格未变：1.36 起 Beta + 默认 `true`，且依赖 `clientfeatures.AtomicFIFO`。区间内新增的 gate（`CgroupNamespacesSupport`、`ContainerScopedProbes`、`KubeServingCertificatesSigner` 等）均与 DaemonSet 无关 |

> 说明：`syncDaemonSet` 体内的 `dsc.consistencyStore.EnsureReady(...)`（读己之写前置校验，失败经 `metrics.DaemonsetRequeueSkips` 计数后 requeue）即上述 gate 的落地路径，本次基准下该逻辑已在上一基准存在，非新增能力，故不另立小节。

**关联文档：** [14-daemonset控制器](../sourceReader/14-daemonset控制器.md)（浓缩版）、[replicaset副本控制](replicaset副本控制.md)（控制器范式）、[deployment滚动更新](deployment滚动更新.md)（Deployment 滚动对照）、[workqueue机制](workqueue机制.md)（控制器队列）。

[总目录](../sourceReader/README.md)
