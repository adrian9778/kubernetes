[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# ReplicaSet 副本控制

> **层级**：跨篇机制纵深专题——以 ReplicaSet 控制器为样本，把**基础控制器范式**讲透：informer 事件入队、worker 调谐、期望计数（expectations）、慢启动批量创建、并发删除、状态更新。这是理解所有 Kubernetes 控制器的样板。
>
> **代码基准**：`adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26），Go 1.27.4。所有「符号+偏移」均在该基准上复核（文末 §7.5 核对说明）。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `rsc.manageReplicas+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[08-controller副本控制](../sourceReader/08-controller副本控制.md) 是浓缩版；本专题纵向展开副本控制的核心算法。Deployment 侧见 [Deployment滚动更新](deployment滚动更新.md)。

---

## 0. 结论速览

### 0.1 ReplicaSet 控制器做什么

把 `spec.replicas`（期望副本数）与实际活跃 Pod 数**对齐**：少了就建 Pod，多了就删 Pod。这是「**调谐循环（reconcile loop）**」范式的最小完整实现。

### 0.2 关键结论

1. **informer→queue→worker 三段式**：事件回调只入队（key），worker goroutine 取 key 后 `syncReplicaSet` 做全量调谐。
2. **expectations 抑制过早调谐**：记「已下发但 informer 还没观测到」的 create/delete 数，避免重复创建/删除。
3. **慢启动批量创建**：`slowStartBatch` 不是一次发 N 个请求，而是按指数扩批量（1→2→4…上限 burstReplicas）分批创建。
4. **并发删除**：删除时带 `deletionTimestamp` 的 Pod 仍算数（不等真正消失），避免重复删。
5. **状态回写**：`calculateStatus` 算 ready/available 副本数，`updateReplicaSetStatus` 回写。

### 0.3 核心算法一句话

```text
syncReplicaSet(key)
└── manageReplicas(rs, ...)
     ├── diff = spec.replicas - 活跃 Pod 数（考虑 expectations）
     ├── diff > 0 → slowStartBatch 创建 |diff| 个 Pod
     └── diff < 0 → getPodsToDelete 选 |diff| 个删
```

---

## 1. 生产视角：副本控制对外行为

### 1.1 用户操作与可见结果

| 操作 | 内部动作 | 可见结果 |
|---|---|---|
| `spec.replicas=3`（当前 0） | slowStartBatch 建 3 个 Pod | 3 个 Pod Pending→Running |
| `spec.replicas=1`（当前 3） | 删 2 个多余 Pod | 2 个 Pod 进 Terminating 后消失 |
| Pod 被手动删 | informer 观测到 → 重建 | 删掉的 Pod 很快被补回 |

### 1.2 可观测信号

- RS `status.replicas`/`readyReplicas`/`availableReplicas`。
- 事件：`SuccessfulCreate`/`SuccessfulDelete`。

---

## 2. 开发视角：控制器骨架（informer→queue→worker）

### 2.1 `ReplicaSetController`（replica_set.go `ReplicaSetController+0`，:101）

持有 RS/Pod lister、indexer、`queue`（rate-limiting workqueue）、`syncHandler`、`expectations`、`podControl`、`burstReplicas`。

### 2.2 构造与装配（`NewReplicaSetController+0`，:168）

```text
NewReplicaSetController(...)
├── NewBaseController(...)：
│     ├── rsc.syncHandler = rsc.syncReplicaSet   NewBaseController+70 (:296)
│     └── 构造 controller.RealPodControl{...} 传入（装配 RealPodControl）
└── 注册 informer handler（Add/Update/Delete → enqueue key）
```

### 2.3 worker 循环（replica_set.go `rsc.worker+0`，:651 → `rsc.processNextWorkItem+0`，:656）

```text
worker(ctx)                                     rsc.worker+0
└── for processNextWorkItem(ctx)               rsc.processNextWorkItem+0
     ├── key = queue.Get()
     ├── syncHandler(key)（= syncReplicaSet）
     ├── 成功 → queue.Forget(key) + Done
     └── 失败 → queue.AddRateLimited(key)（指数退避）+ Done
```

---

## 3. 开发视角：调谐核心（`syncReplicaSet`）

### 3.1 `syncReplicaSet`（replica_set.go `rsc.syncReplicaSet+0`，:808）

```text
syncReplicaSet(ctx, key)
├── 拆 namespace/name
├── rsLister.ReplicaSets(ns).Get；NotFound → 清 expectations 后返回 (+23~+28)
├── expectations.SatisfiedExpectations(key)?   （抑制过早调谐）
│     └── 不满足 → 本轮跳过（等 informer 追上）
├── listPods（按 selector + ControllerRef 筛本 RS 的 Pod）
└── manageReplicas(rs, filteredPods)            rsc.manageReplicas+0 (:678)
```

### 3.2 期望计数 expectations（controller_utils.go）

`ControlleeExpectations`（controller_utils.go `ControlleeExpectations+0`，:271）记录「已下发但 informer 尚未观测到」的 create/delete 数（`add`/`del` 原子计数）。
- `SatisfiedExpectations`（`r.SatisfiedExpectations+0`，:196）：add/del 都归零才算满足。
- **为什么必须：** informer 有延迟，刚下发 3 个 create，informer 可能还没看到——不抑制就会重复创建。

---

## 4. 开发视角：副本增删（`manageReplicas`）

### 4.1 `manageReplicas`（replica_set.go `rsc.manageReplicas+0`，:678）

```text
manageReplicas(rs, filteredPods)
├── 分类 Pod：activePods / terminatingPods
├── diff = spec.replicas - activePods（考虑 terminating 占位）
├── diff > 0（要建）：
│     └── slowStartBatch(diff, burstReplicas, fn)   slowStartBatch+0 (:944)
│           fn = podControl.CreatePods(...)          RealPodControl.CreatePods (controller_utils.go:550)
├── diff < 0（要删）：
│     ├── getPodsToDelete(filteredPods, relatedPods, |diff|)   getPodsToDelete+0 (:998)
│     │     ↑ 选删除优先级（未调度优先、Ready 低的优先）
│     └── podControl.DeletePod(...)                   RealPodControl.DeletePod (controller_utils.go:629)
└── 返回 manageReplicasErr（供 calculateStatus）
```

### 4.2 慢启动批量创建（`slowStartBatch+0`，:944）

```text
slowStartBatch(count, initialBatchSize, fn)
├── remaining = count
├── 并发度从 initialBatchSize 起，每成功一批翻倍（1→2→4→…上限 burstReplicas）
├── 每批调 fn 并发建 Pod
└── 任一失败 → 停止后续批次（剩余不建，等下轮调谐）
```
**为什么慢启动：** 避免一次性建大量 Pod 打爆 apiserver；初期小批量探测（如有调度问题早发现），稳定后放大并发。

### 4.3 删除选择（`getPodsToDelete+0`，:998）

按优先级选要删的 Pod：
- 未调度（无 nodeName）的优先删。
- PodPhase 低（Pending < Unknown < Running）的优先删。
- Ready 时间晚的优先删（保住已经 Ready 最久的）。

### 4.4 Pod 创建/删除的实际实现（`RealPodControl`）

`RealPodControl`（controller_utils.go `RealPodControl+0`，:492）：
- `CreatePods+0`（:550）：构造 Pod（template + ControllerRef）→ POST apiserver。
- `DeletePod+0`（:629）：DELETE apiserver（带优雅删除）。

---

## 5. 开发视角：状态回写

### 5.1 `calculateStatus`（replica_set_utils.go `calculateStatus+0`，:96）

算 `replicas`（活跃）、`readyReplicas`、`availableReplicas`（满足 minReadySeconds），以及 `conditions`（如 `ReplicaFailure`）。

### 5.2 `updateReplicaSetStatus`（replica_set_utils.go `updateReplicaSetStatus+0`，:41）

把新 status 回写 apiserver（仅当变化时 PATCH）。`NewReplicaSetCondition+0`（:147）构造 condition。

---

## 6. 全链路时序（扩容 0→3）

```text
spec.replicas=3
└── informer 观测 RS → enqueue key
     └── worker (replica_set.go:651) → processNextWorkItem (:656)
          └── syncReplicaSet (:808)
               ├── expectations.SatisfiedExpectations (controller_utils.go:196)
               ├── listPods → 0 个活跃
               └── manageReplicas (:678)
                    └── diff=+3 → slowStartBatch (:944) 指数批量
                         └── RealPodControl.CreatePods (controller_utils.go:550) ×3
                              └── 3 个 Pod 创建 → 下轮调谐 calculateStatus (:96) 回写 status
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在基准 `adrian@6bf0bfd5` 上复核（详见 §7.5）：
- `ReplicaSetController+0`（replica_set.go:101）、`NewReplicaSetController+0`（:168）、`NewBaseController+70`（:296，syncHandler 赋值）
- `rsc.worker+0`（:651）、`rsc.processNextWorkItem+0`（:656）、`rsc.manageReplicas+0`（:678）、`rsc.syncReplicaSet+0`（:808）
- `slowStartBatch+0`（:944）、`getPodsToDelete+0`（:998）
- `updateReplicaSetStatus+0`（replica_set_utils.go:41）、`calculateStatus+0`（:96）、`NewReplicaSetCondition+0`（:147）
- `ControlleeExpectations+0`（controller_utils.go:271）、`r.SatisfiedExpectations+0`（:196）、`RealPodControl+0`（:492）、`CreatePods+0`（:550）、`DeletePod+0`（:629）

### 7.2 生产实测

**未实测** `[待运行验证]`。慢启动实际批次节奏、expectations 抑制窗口、删除选择优先级均为代码推断，未在真实集群观测创建时序与选择结果。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| getPodsToDelete 完整优先级 | 未逐行读 | 各删除理由（scale-down、node 缩容、Pod 被驱逐等）权重细节未展开 |
| scheduling 集成（PodGroup） | 未深挖 | workload/PodGroup lister 的 gang 调度集成未读 |
| consistencyStore | 未深挖 | OnWrite 一致性记录未展开 |
| minReadySeconds | 未深挖 | available 判定的 minReadySeconds 细节未读 |

### 7.4 修改联动

- 改创建节奏 → `slowStartBatch`，同步本文 §4.2。
- 改删除选择 → `getPodsToDelete`，同步 §4.3。
- 改抑制逻辑 → `SatisfiedExpectations`，同步 §3.2。

### 7.5 基准核对说明（2026-11-26）

旧基准 `master@782c049`（2026-11-19）→ 新基准 `adrian@6bf0bfd5`（2026-11-26）。经 MCP `codegraph_explore` 抽查，replicaset/replication 副本控制链路在漂移区间**零改动**，全部锚点 def 行与本文记法一致，故只更新基准、不改锚点语义（不为改而改）。

抽查锚点及 def 行（`codegraph_explore` 返回的 def 行号，与 verify 工具的 `offset` 子命令一致）：

| 锚点 | 文件 | def 行 |
|---|---|---|
| `ReplicaSetController+0` | `pkg/controller/replicaset/replica_set.go` | 101 |
| `NewReplicaSetController+0` | 同上 | 168 |
| `NewBaseController+0`（`syncHandler=rsc.syncReplicaSet` 在 `+70`，即 :296） | 同上 | 226 |
| `rsc.worker+0` / `rsc.processNextWorkItem+0` | 同上 | 651 / 656 |
| `rsc.manageReplicas+0` | 同上 | 678 |
| `rsc.syncReplicaSet+0` | 同上 | 808 |
| `slowStartBatch+0` | 同上 | 944 |
| `getPodsToDelete+0` | 同上 | 998 |
| `updateReplicaSetStatus+0` / `calculateStatus+0` / `NewReplicaSetCondition+0` | `pkg/controller/replicaset/replica_set_utils.go` | 41 / 96 / 147 |
| `ControlleeExpectations+0` / `SatisfiedExpectations+0` | `pkg/controller/controller_utils.go` | 271 / 196 |
| `RealPodControl+0` / `CreatePods+0` / `DeletePod+0` | 同上 | 492 / 550 / 629 |

说明：`RealPodControl`/`podControlAdapter` 为 `sr_tools.py` 已知假阳性来源；本文锚点以 `codegraph_explore` 为准。基准更新后 `verify` 结果 ✗=0（无新增失效），遗留的 `?` 为同名符号/同名文件歧义（如 `replica_set.go` 在 `pkg/controller/replicaset/` 与 `test/e2e/apps/` 各一份、`slowStartBatch`/`calculateStatus` 跨包同名），非失效，语义归属已在正文用文件名限定。

**关联文档：** [08-controller副本控制](../sourceReader/08-controller副本控制.md)（浓缩版）、[12-deployment控制器](../sourceReader/12-deployment控制器.md)（Deployment 侧）、[deployment滚动更新](deployment滚动更新.md)（滚动）、[workqueue机制](workqueue机制.md)（控制器队列）。

[总目录](../sourceReader/README.md)
