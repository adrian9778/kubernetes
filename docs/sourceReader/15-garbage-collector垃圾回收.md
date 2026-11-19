[上一篇](14-daemonset控制器.md) · [总目录](README.md) · [下一篇](16-kube-proxy网络代理.md)

# Garbage Collector 垃圾回收
> **层级**：第四层——函数级源码解析。
> **范围**：owner/dependent 依赖图维护、孤儿与悬挂引用判定、Foreground/Background/Orphan 三种级联删除。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **锚点记法**：符号+偏移，函数定义行=+0；偏移为相对该符号定义的源码行差。

## 0. 生产视角

### 0.1 GC 对外行为
Garbage Collector（下称 GC）是 `kube-controller-manager` 内的一个通用控制器，**不做任何业务分类**，只做一件事：**回收「所有者已不存在」的对象**。它不关心对象是什么资源，只依赖两样元数据——

- **ownerReferences**：对象上的 `ownerReferences` 字段声明「我属于谁」；
- **finalizers**：对象上的 `finalizers` 字段声明「我还有什么收尾工作没做」。

GC 的对外可见行为只有三类：

| 行为 | 可见结果 |
|---|---|
| 孤儿回收 | owner 已被删除（或引用悬挂）的 dependent 被 GC 删除 |
| 级联删除 | 删除 owner 时按策略连带删除/保留其 dependents |
| 引用修复 | 对「仍被 solid owner 持有」的对象，GC 只摘除已失效的 owner 引用，不删对象 |

> **大白话**：GC 是 K8s 的「环卫工」。用户删除一个对象时，它负责判断「这个对象下面还有没有需要一起清掉的孩子」，并把「引用已死的爸爸但自己还活着」的对象处理掉。

### 0.2 级联删除可见结果（用户视角）
三种级联删除策略是用户删除对象时显式/隐式指定的，其可见结果差异：

| 策略 | 删除发起后用户看到什么 | 最终结果 |
|---|---|---|
| **Foreground**（前台） | owner 先进入「删除中」（`deletionTimestamp` 置位），**不立即消失**；GC 先把 owner 的所有 blocking dependent 删光，再摘掉 owner 的 `foregroundDeletion` finalizer，owner 才真正消失 | 调用方（如 kubectl）**同步等待**：owner 消失时其 dependents 已全部消失 |
| **Background**（后台） | owner **立即消失**；dependents 由 GC 在后台逐个删除 | 调用方不等待；dependents 稍后消失 |
| **Orphan**（孤儿化） | owner 保留（带 `orphan` finalizer）；GC 把**所有 dependent 的 ownerReferences 中的 owner 引用摘除** | owner 保留；dependents 变成「无主对象」，各自独立存活 |

**判据要点**：`kubectl delete` 未显式指定 `--cascade` 时，apiserver 会按对象是否已有 finalizer 决定策略。GC 在 `attemptToDeleteItem` 里读**对象当前 finalizer** 来选策略（`hasOrphanFinalizer`/`hasDeleteDependentsFinalizer`），默认 Background。

---

## 1. 开发视角：模块级

| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/garbagecollector/` |
| 文件 | `garbagecollector.go`（worker 与删除判定）、`graph_builder.go`（监视器、依赖图装配）、`graph.go`（node）、`operations.go`、`patch.go`、`uid_cache.go`；`monitor.go`/`node.go` **不存在** |
| 依赖 | 全资源动态监视器、metadata client（PartialObjectMetadata）、restMapper、三个 `*node`/`*event` 泛型队列、absentOwnerCache |
| 数据流 | informer 事件 → graphChanges → processGraphChanges 改依赖图 → 节点入 attemptToDelete/attemptToOrphan → worker 核实 owner 状态后删除/patch |

> **文件归属修正**：`GetGraphResources` 是 `(*GraphBuilder)` 方法，定义于 **graph_builder.go**（`GetGraphResources+0`，:1016），**不在** garbagecollector.go；`event`（graph_builder.go:69）、`monitor`（:124）、`monitors`（:139）同样**定义于 graph_builder.go**（旧文把三者列在 garbagecollector.go 名下，属归类偏差）。

## 2. 开发视角：类型级

| 类型 | 文件 | 锚点（符号+偏移） | 职责 |
|---|---|---|---|
| `GarbageCollector` | `garbagecollector.go` | `GarbageCollector+0`（:61） | 持有两个 worker 队列（attemptToDelete/attemptToOrphan）、dependencyGraphBuilder、absentOwnerCache、metadataClient、restMapper |
| `GraphBuilder` | `graph_builder.go` | `GraphBuilder+0`（:82） | 全资源监视器与依赖图的唯一写者；持有 graphChanges 队列、uidToNode、attemptToDelete/attemptToOrphan 引用 |
| `event` | `graph_builder.go` | `event+0`（:69） | 监视器事件：virtual、eventType（add/update/delete）、obj/oldObj、gvk |
| `monitor` | `graph_builder.go` | `monitor+0`（:124） | 单个 GVR 的 controller + store + stopCh |
| `monitors` | `graph_builder.go` | `monitors+0`（:139） | `map[GVR]*monitor` 类型 |
| `node` | `graph.go` | `node+0`（:63） | 图节点：identity（objectReference）、owners、dependents（带锁）、virtual、beingDeleted、deletingDependents |
| `concurrentUIDToNode` | `graph.go` | `concurrentUIDToNode+0`（:239） | UID→node 并发 map（processGraphChanges 为单线程读写，注释说明实际无竞争） |
| `ReferenceCache` | `uid_cache.go` | `ReferenceCache+0`（:24） | 已确认不存在的 owner identity 的 LRU 缓存（上限 500） |

## 3. 开发视角：启动与 worker 函数级

### 3.1 函数：NewGarbageCollector / NewComposedGarbageCollector
**源码：** `garbagecollector.go:NewGarbageCollector+0`（:80）/ `NewComposedGarbageCollector+0`（:93）
**执行流程：** NewGarbageCollector 先建 GraphBuilder；Composed 版经 `GraphBuilder.GetGraphResources+0`（graph_builder.go:1016）取出两队列与 absentOwnerCache 完成 GC 装配。接线处在 `cmd/kube-controller-manager/app/core.go`。

### 3.2 函数：Run
**源码：** `garbagecollector.go:GarbageCollector.Run+0`（:129）
**执行流程：** 启动 GraphBuilder → `WaitForNamedCacheSyncWithContext`（超时仅告警继续）→ 启动 N 个 delete worker（`runAttemptToDeleteWorker`）与 N 个 orphan worker；关停时先关 graphChanges 再关两 worker 队列。

### 3.3 函数：processAttemptToDeleteWorker / attemptToDeleteWorker
**源码：** `garbagecollector.go:GarbageCollector.processAttemptToDeleteWorker+0`（:294）/ `attemptToDeleteWorker+0`（:319）
**流程：** `attemptToDeleteWorker` 取 `*node` → 与图中最新节点比对（`!n.isObserved()` 时查 uidToNode，节点已不存在或已被观测则 forgetItem）→ `attemptToDeleteItem`。返回值改用 `workQueueItemAction` 枚举：`enqueuedVirtualDeleteEventErr`、`namespacedOwnerOfClusterScopedObjectErr`、`*restMappingError`（转 `requeueItem` 重试）其余错误 `requeueItem`；处理动作在 `processAttemptToDeleteWorker` 里执行 `Forget`/`AddRateLimited`。

### 3.4 函数：Sync
**源码：** `garbagecollector.go:GarbageCollector.Sync+0`（:187）
**职责：** 周期跑 `ServerPreferredResources`，资源集合变化时 `restMapper.Reset()` 并 `resyncMonitors`。

## 4. 开发视角：依赖图函数级

### 4.1 函数：controllerFor
**源码：** `graph_builder.go:GraphBuilder.controllerFor+0`（:192）
**职责：** 为单个 GVR 从 sharedInformers 取 informer，注册 Add/Update/Delete（Delete 拆 DeletedFinalStateUnknown），统一构造 `*event` 入 graphChanges 队列。

### 4.2 函数：GraphBuilder.Run / runProcessGraphChanges / processGraphChanges
**源码：** `graph_builder.go:GraphBuilder.Run+0`（:391）/ `runProcessGraphChanges+0`（:692）/ `processGraphChanges+0`（:710）

**processGraphChanges 处理：**
```text
processGraphChanges+0
├── virtual 节点首次被观测 → markObserved（identity 不一致则 clone 替换）(+58)
├── add/update 新节点：建 node（beingDeleted/deletingDependents 由删除戳与 finalizer 推出）
│   → insertNode(+69) → processTransitions(+72)
├── add/update 已有节点：
│   referencesDiffs(+75，graph_builder.go:577) → addUnblockedOwnersToDeleteQueue(+79，:650)
│   → setOwners(+81)/addDependentToOwners(+83，:450，缺 owner 建 virtual 节点并入删除队列)
│   → markBeingDeleted(+90) → processTransitions
└── delete 事件：removeNode，dependents 入删除队列，
    唤醒 isDeletingDependents 的 owner (+93 ～ end)
```

### 4.3 函数：processTransitions
**源码：** `graph_builder.go:GraphBuilder.processTransitions+0`（:675）
**职责：** 两种状态迁移：
- `startsWaitingForDependentsOrphaned`（:644）→ 入 attemptToOrphan
- `startsWaitingForDependentsDeleted`（:638，判定 `deletionStartsWithFinalizer`，:601）→ `markDeletingDependents`，全部 dependents 与自身入 attemptToDelete

### 4.4 node 方法（graph.go）
| 方法 | 锚点（符号+偏移） | 职责 |
|---|---|---|
| `markObserved`/`isObserved` | `node.markObserved+0`（:119）/`node.isObserved+0`（:124） | virtual→实际节点 |
| `markBeingDeleted`/`isBeingDeleted` | `node.markBeingDeleted+0`（:107）/`node.isBeingDeleted+0`（:113） | 删除戳状态 |
| `markDeletingDependents`/`isDeletingDependents` | `node.markDeletingDependents+0`（:130）/`node.isDeletingDependents+0`（:136） | foreground 删除状态 |
| `setOwners`/`getOwners` | `node.setOwners+0`（:142）/`node.getOwners+0`（:148） | owner 引用 |
| `addDependent`/`deleteDependent`/`getDependents` | `node.addDependent+0`（:154）/`node.deleteDependent+0`（:160）/`node.getDependents+0`（:175） | dependent 集合 |
| `blockingDependents` | `node.blockingDependents+0`（:191） | 取出 BlockOwnerDeletion=true 的 dependent |

## 5. 开发视角：删除判定函数级

### 5.1 函数：attemptToDeleteItem
**源码：** `garbagecollector.go:GarbageCollector.attemptToDeleteItem+0`（:500）
**职责：** 单节点删除的完整判定。
**执行流程：**
```text
attemptToDeleteItem+0
├── 1. beingDeleted 且非 deletingDependents → 直接返回（等最终删除）(+9)
├── 2. getObject（operations.go:getObject+0，:82）拉最新对象；
│      NotFound/UID 不匹配 → enqueueVirtualDeleteEvent (+18)
├── 3. deletingDependents → processDeletingDependentsItem（:661）(+43)
├── 4. 无 ownerReferences → 不删 (+49)
├── 5. classifyReferences（:461）逐 owner 判定 (+56)
│      isDangling（:380）：先查 absentOwnerCache 再 metadata Get；
│      owner 正 deleting 且带 delete-dependents finalizer → waitingForDependentsDeletion；
│      其余为 solid
├── 6. 存在 solid owner → 保留对象，patch 移除 dangling/waiting 的 ownerRef (+85)
├── 7. owner 在 waiting 且自身有 dependents → 环检测，
│      unblockOwnerReferences patch（patch.go:127）后 Foreground 删除 (+93 ～ +125)
└── 8. 按现有 finalizer 选 policy 后 deleteObject (+125 前台分支 / +151 默认分支)
```
> **偏移修正**：后半段（环检测/删除分支）相对旧文整体**前移约 9～35 行**：环检测 `unblockOwnerReferences` 现为 **+93～+125**（旧 +119～+158），前台 `deleteObject` 现为 **+125**（旧 +160～+177），默认分支现为 **+151**（旧 +160～+177）；前半段（+9～+56）基本吻合（±5 内）。

#### classifyReferences 三分类与删除分支深度拆解

`attemptToDeleteItem` 的核心是把对象的每个 ownerReference 归为三类，再据此决定「保留 / 摘引用 / 前台删 / 后台删」。

**classifyReferences（偏移：+461）逐 owner 三分类：**
| 分类 | 判定条件 | 含义 |
|---|---|---|
| **dangling** | `isDangling`（:380）为真——先查 `absentOwnerCache` 再 metadata Get，owner 确实不存在 | owner 已消失，该引用是悬空的 |
| **waitingForDependentsDeletion** | owner 存在、`GetDeletionTimestamp() != nil` 且带 `hasDeleteDependentsFinalizer` | owner 正在 Foreground 删除、等 dependents 先删 |
| **solid** | 其余（owner 存在且未在删） | owner 还活着，对象必须保留 |

**三分支决策（attemptToDeleteItem 后半段）：**
1. **`len(solid) != 0`**（至少一个 owner 还活着）→ **不回收对象**；若同时有 dangling/waiting 引用，patch 把它们从 `ownerReferences` 移除（`GenerateDeleteOwnerRefStrategicMergeBytes` + `deleteOwnerRefJSONMergePatch`，+88 ～ +93）。注释（+84 ～ +87）强调：**waitingForDependentsDeletion 也必须摘掉**，否则被引用对象会永久卡在 `FinalizerDeleteDependents` 上无法删除。
2. **`len(waitingForDependentsDeletion) != 0 && item.dependentsLength() != 0`**（owner 在等 dependents、对象自己也有 dependents）→ **环检测 + Foreground 删除**（+96 ～ +125）：若某个 owner 自身 `isDeletingDependents()`，则用 `unblockOwnerReferencesStrategicMergePatch` 把 ownerReferences 改成非阻塞（防止环死锁），再以 `DeletePropagationForeground` 删除。注释（+99 ～ +102）坦承**环检测有误报**、多 worker 并发下存在 race，必要时需更严格检测。删除事件会被 graphBuilder 观察到，对象再次进入 `processDeletingDependentsItem`。
3. **default（无 solid owner，也无 owner 在等 dependents）→ 真正回收**（+128 起）：按对象现有 finalizer 选 propagationPolicy——
   - `hasOrphanFinalizer(latest)` → `DeletePropagationOrphan`（尊重已有 orphan finalizer）。
   - `hasDeleteDependentsFinalizer(latest)` → `DeletePropagationForeground`。
   - 否则 → `DeletePropagationBackground`（默认）。

**三类 owner 的语义对照：**

| 分类 | 对象命运 | 动作 |
|---|---|---|
| solid ≥ 1 | 保留 | 摘掉 dangling/waiting 引用（若有） |
| 无 solid，有 waiting + 有 dependents | Foreground 删除（先解环） | `unblockOwnerReferences` + `deleteObject(Foreground)` |
| 无 solid，无 waiting | 后台/前台/orphan 删除 | 按现有 finalizer 选 policy → `deleteObject` |

**设计要点（源码可证）：**
1. **solid 优先**：只要有任一 owner 活着就绝不回收，避免多 owner 场景下一个 owner 删除就连带删掉仍被需要的对象。
2. **摘引用而非删对象**：dangling/waiting 引用通过 patch 移除，而不是删被引用对象——保持「owner 引用」与「对象存亡」解耦。
3. **环检测务实但有误报**：Foreground 依赖链可能成环，GC 用「把 ownerReferences 改非阻塞」打破环，注释明确接受误报以换取简单性。
4. **policy 由 finalizer 反推**：默认分支不硬编码策略，而是读对象上已有的 orphan/foreground finalizer 决定，保证与 apiserver 侧设置的删除语义一致。

### 5.2 函数：processDeletingDependentsItem
**源码：** `garbagecollector.go:GarbageCollector.processDeletingDependentsItem+0`（:661）
**职责：** Foreground 主流程：`blockingDependents` 为空 → 摘除 `FinalizerDeleteDependents` 完成最终删除；非空则把这些 dependent 入队删除。

### 5.3 函数：deleteObject
**源码：** `operations.go:GarbageCollector.deleteObject+0`（:53）
**职责：** 实际删除调用（**本包无 `deleteWithPropagationPolicy` 符号**，全包 grep 0 命中）：带 UID（必有）与 ResourceVersion（可选）前置条件，`DeleteOptions{PropagationPolicy}` 调 metadata client Delete；冲突且 ownerRefs 未变时去 RV 重试（冲突判断 +13，重试调用 +23，:76）。

### 5.4 函数：orphan worker / orphanDependents
**源码：** `garbagecollector.go:GarbageCollector.attemptToOrphanWorker+0`（:746）/ `orphanDependents+0`（:680）
**职责：** Orphan 主流程：快照 dependents → 并发对每个 dependent patch 删 ownerRef（以 dependent UID 为前置条件）→ `removeFinalizer(FinalizerOrphanDependents)`（operations.go:removeFinalizer+0，:104，RetryOnConflict）。失败则 `requeueItem`。

## 6. 开发视角：三种级联删除策略

| 策略 | apiserver 加的 finalizer | GC 行为 |
|---|---|---|
| Foreground | `foregroundDeletion`（`metav1.FinalizerDeleteDependents`，apimachinery types.go:126） | owner 先"删除中"，GC 删完 blockingDependents 再摘 finalizer，调用方同步等待 |
| Background | 无阻塞 finalizer | Delete 立即返回；owner 消失事件触发 dependents 各自后台删除 |
| Orphan | `orphan`（`metav1.FinalizerOrphanDependents`，types.go:125） | GC patch 掉全部 dependent 的 ownerRef，对象本身保留 |

**policy 选择逻辑**在 `attemptToDeleteItem`（+151 默认分支）：对象已有 orphan/foreground finalizer 时沿用对应策略（`hasOrphanFinalizer`/`hasDeleteDependentsFinalizer`），默认 Background。对应 `DeletionPropagation` 常量：`DeletePropagationOrphan`（apimachinery types.go:556）、`DeletePropagationBackground`（:559）、`DeletePropagationForeground`（:565）。

## 7. 开发视角：动态资源监控函数级

### 7.1 函数：syncMonitors / startMonitors / stopMonitors
**源码：** `graph_builder.go:GraphBuilder.syncMonitors+0`（:250）/ `startMonitors+0`（:303）/ `stopMonitors+0`（:332）
**职责：** syncMonitors 对新发现的可删除 GVR（跳过 ignored 与 events）建 monitor，对消失的 GVR 关 stopCh；startMonitors 等 informersStarted 后启动 controller。GC 侧封装为 `garbagecollector.go:GarbageCollector.resyncMonitors+0`（:120）。

### 7.2 函数：GetDeletableResources
**源码：** `garbagecollector.go:GetDeletableResources+0`（:793）
**职责：** `ServerPreferredResources` 后用 `SupportsAllVerbs{delete/list/watch}` 过滤，展平成 GVR 集合；容忍部分 group 发现失败。

## 8. 开发视角：队列使用

| 队列 | 入队元素 | 名称 |
|---|---|---|
| graphChanges | `*event` | garbage_collector_graph_changes |
| attemptToDelete | `*node` | garbage_collector_attempt_to_delete |
| attemptToOrphan | `*node` | garbage_collector_attempt_to_orphan |

## 9. 开发视角：调用关系总览
```text
monitors → graphChanges
 ↓
processGraphChanges（依赖图唯一写者）
 ├── processTransitions
 │    ├→ attemptToOrphan
 │    └→ attemptToDelete
 ├── addDependentToOwners（virtual owner）→ attemptToDelete
 └── delete → dependents → attemptToDelete

attemptToDelete workers
 └── attemptToDeleteItem
      ├── getObject / classifyReferences（isDangling → absentOwnerCache）
      ├── patch ownerRefs（solid owner 场景）
      ├── processDeletingDependentsItem → blockingDependents
      └── deleteObject（DeleteOptions PropagationPolicy）

orphan workers → orphanDependents → removeFinalizer
```

## 10. 设计意图与验证

### 10.1 设计意图
- **单一写者**：依赖图（uidToNode）只由 `processGraphChanges` 单线程读写，`concurrentUIDToNode` 的并发安全是为将来多写者预留（注释说明实际无竞争）。
- **元数据最小化**：GC 用 metadata client（PartialObjectMetadata）而非完整对象，只取 ownerReferences/finalizers/deletionTimestamp/UID，避免解码全量资源。
- **最终一致性**：GC 对「暂不可判定」的引用（如 restMappingError、虚拟节点未观测）采用重试而非放弃，保证最终收敛。
- **环检测防御**：owner/dependent 相互引用时 `attemptToDeleteItem` 走 `unblockOwnerReferences` patch 打断环，避免死锁。

### 10.2 验证
- **源码锚点**：本稿全部符号+偏移经 grep/read 在当前基准（master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`）逐一核对，证据见 `.progress/evidence/15.md`。
- **关键修正已落实**：`GetGraphResources` 归属 graph_builder.go:1016；`event`/`monitor`/`monitors` 归属 graph_builder.go；`attemptToDeleteItem` 后半段偏移前移 9～35；`deleteWithPropagationPolicy` 无（0 命中）。
- **零漂移锚点**：`GarbageCollector+0`（:61）、`GraphBuilder+0`（:82）、`node+0`（:63）、`concurrentUIDToNode+0`（:239）、`ReferenceCache+0`（:24）及全部 node 方法、garbagecollector.go/graph_builder.go/operations.go 各函数定义行均与旧文一致。
- **未确认项**：无。

[上一篇](14-daemonset控制器.md) · [总目录](README.md) · [下一篇](16-kube-proxy网络代理.md)
