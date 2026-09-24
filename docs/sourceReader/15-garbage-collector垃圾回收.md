[上一篇](14-daemonset控制器.md) · [总目录](README.md) · [下一篇](16-kube-proxy网络代理.md)

# Garbage Collector 垃圾回收
> **层级**：第四层——函数级源码解析。
> **范围**：owner/dependent 依赖图维护、孤儿与悬挂引用判定、Foreground/Background/Orphan 三种级联删除。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/garbagecollector/` |
| 文件 | `garbagecollector.go`（worker 与删除判定）、`graph_builder.go`（监视器、依赖图装配，本分支无 monitor.go）、`graph.go`（node，本分支无 node.go）、`operations.go`、`patch.go`、`uid_cache.go` |
| 依赖 | 全资源动态监视器、metadata client（PartialObjectMetadata）、restMapper、三个 `*node`/`*event` 泛型队列、absentOwnerCache |
| 数据流 | informer 事件 → graphChanges → processGraphChanges 改依赖图 → 节点入 attemptToDelete/Orphan → worker 核实 owner 状态后删除/patch |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `GarbageCollector` | `garbagecollector.go:61` | 持有两个 worker 队列、dependencyGraphBuilder、absentOwnerCache、metadataClient、restMapper |
| `GraphBuilder` | `graph_builder.go:82` | 全资源监视器与依赖图的唯一写者，持有 graphChanges 队列、uidToNode、attemptToDelete/Orphan 引用 |
| `event` | :69 | 监视器事件：virtual、eventType（add/update/delete）、obj/oldObj、gvk |
| `monitor` | :124 | 单个 GVR 的 controller + store + stopCh；`monitors map[GVR]*monitor`（:139） |
| `node` | `graph.go:63` | 图节点：identity（objectReference）、owners、dependents（带锁）、virtual、beingDeleted、deletingDependents |
| `concurrentUIDToNode` | :239 | UID→node 并发 map（processGraphChanges 为单线程读写，注释说明实际无竞争） |
| `ReferenceCache` | `uid_cache.go:24` | 已确认不存在的 owner identity 的 LRU 缓存（上限 500） |

## 3. 启动与 worker 函数级

### 函数：NewGarbageCollector / NewComposedGarbageCollector
**源码：** `garbagecollector.go:80 / :93`
**执行流程：** NewGarbageCollector 先建 GraphBuilder；Composed 版经 `GetGraphResources`（:1016）取出两队列与 absentOwnerCache 完成 GC 装配。接线处在 `cmd/kube-controller-manager/app/core.go`。

### 函数：Run
**源码：** `garbagecollector.go:129`
**执行流程：** 启动 GraphBuilder → `WaitForNamedCacheSyncWithContext`（超时仅告警继续）→ 启动 N 个 delete worker（runAttemptToDeleteWorker）与 N 个 orphan worker；关停时先关 graphChanges 再关两 worker 队列。

### 函数：processAttemptToDeleteWorker / attemptToDeleteWorker
**源码：** `garbagecollector.go:294 / :319`
**流程：** Get `*node` → 与图中最新节点比对 → attemptToDeleteItem；错误分类：`enqueuedVirtualDeleteEventErr`、`namespacedOwnerOfClusterScopedObjectErr` 直接 Forget，其余错误 AddRateLimited。

### 函数：Sync
**源码：** `garbagecollector.go:187`
**职责：** 周期跑 ServerPreferredResources，资源集合变化时 `restMapper.Reset()` 并 resyncMonitors。

## 4. 依赖图函数级

### 函数：controllerFor
**源码：** `graph_builder.go:192`
**职责：** 为单个 GVR 从 sharedInformers 取 informer，注册 Add/Update/Delete（Delete 拆 DeletedFinalStateUnknown），统一构造 `*event` 入 graphChanges 队列。

### 函数：GraphBuilder.Run / runProcessGraphChanges / processGraphChanges
**源码：** `graph_builder.go:391 / :692 / :710`
**processGraphChanges 处理：**
```text
processGraphChanges
├── virtual 节点首次被观测 → markObserved（identity 不一致则 clone 替换）(+25 ～ +59)
├── add/update 新节点：建 node（beingDeleted/deletingDependents 由删除戳与 finalizer 推出）
│   → insertNode → processTransitions (+60 ～ +100)
├── add/update 已有节点：
│   referencesDiffs（:577）→ addUnblockedOwnersToDeleteQueue（:650）
│   → setOwners/addDependentToOwners（:450，缺 owner 建 virtual 节点并入删除队列）
│   → markBeingDeleted → processTransitions
└── delete 事件：removeNode，dependents 入删除队列，
    唤醒 isDeletingDependents 的 owner (+94 ～ end)
```

### 函数：processTransitions
**源码：** `graph_builder.go:675`
**职责：** 两种状态迁移：
- `startsWaitingForDependentsOrphaned` → 入 attemptToOrphan
- `startsWaitingForDependentsDeleted`（:638，判定 FinalizerDeleteDependents，:601）→ markDeletingDependents，全部 dependents 与自身入 attemptToDelete

### node 方法（graph.go）
| 方法 | 行 | 职责 |
|---|---|---|
| `markObserved/isObserved` | 119/124 | virtual→实际节点 |
| `markBeingDeleted/isBeingDeleted` | 107/113 | 删除戳状态 |
| `markDeletingDependents/isDeletingDependents` | 130/136 | foreground 删除状态 |
| `setOwners/getOwners` | 142/148 | owner 引用 |
| `addDependent/deleteDependent/getDependents` | 154/160/175 | dependent 集合 |
| `blockingDependents` | 191 | 取出 BlockOwnerDeletion=true 的 dependent |

## 5. 删除判定函数级

### 函数：attemptToDeleteItem
**源码：** `garbagecollector.go:500`
**职责：** 单节点删除的完整判定。
**执行流程：**
```text
attemptToDeleteItem (+0)
├── 1. beingDeleted 且非 deletingDependents → 直接返回（等最终删除）(+4)
├── 2. getObject（operations.go:82）拉最新对象；
│      NotFound/UID 不匹配 → enqueueVirtualDeleteEvent (+12 ～ +35)
├── 3. deletingDependents → processDeletingDependentsItem（:661）(+44)
├── 4. 无 ownerReferences → 不删 (+51)
├── 5. classifyReferences（:461）逐 owner 判定 (+55 ～ +80)
│      isDangling（:380）：先查 absentOwnerCache 再 metadata Get；
│      owner 正 deleting 且带 delete-dependents finalizer → waitingForDependentsDeletion；
│      其余为 solid
├── 6. 存在 solid owner → 保留对象，patch 移除 dangling/waiting 的 ownerRef（:583）(+83 ～ +115)
├── 7. owner 在 waiting 且自身有 dependents → 环检测，
│      unblockOwnerReferences patch（patch.go:127）后 Foreground 删除 (+119 ～ +158)
└── 8. 按现有 finalizer 选 policy 后 deleteObject (+160 ～ +177)
```

### 函数：processDeletingDependentsItem
**源码：** `garbagecollector.go:661`
**职责：** Foreground 主流程：blockingDependents 为空 → 摘除 FinalizerDeleteDependents 完成最终删除；非空则把这些 dependent 入队删除。

### 函数：deleteObject
**源码：** `operations.go:53`
**职责：** 实际删除调用（本包无 deleteWithPropagationPolicy 符号）：带 UID（必有）与 ResourceVersion（可选）前置条件，`DeleteOptions{PropagationPolicy}` 调 metadata client Delete；冲突且 ownerRefs 未变时去 RV 重试（:65 ～ +77）。

### 函数：orphan worker / orphanDependents
**源码：** `garbagecollector.go:746 / :680`
**职责：** Orphan 主流程：快照 dependents → 并发对每个 dependent patch 删 ownerRef（以 dependent UID 为前置条件）→ `removeFinalizer(FinalizerOrphanDependents)`（operations.go:104，RetryOnConflict）。

## 6. 三种级联删除策略
| 策略 | apiserver 加的 finalizer | GC 行为 |
|---|---|---|
| Foreground | `foregroundDeletion`（`metav1.FinalizerDeleteDependents`，apimachinery types.go:126） | owner 先"删除中"，GC 删完 blockingDependents 再摘 finalizer，调用方同步等待 |
| Background | 无阻塞 finalizer | Delete 立即返回；owner 消失事件触发 dependents 各自后台删除 |
| Orphan | `orphan`（`FinalizerOrphanDependents`，types.go:125） | GC patch 掉全部 dependent 的 ownerRef，对象本身保留 |

policy 选择逻辑在 attemptToDeleteItem：对象已有 orphan/foreground finalizer 时沿用对应策略，默认 Background。

## 7. 动态资源监控函数级

### 函数：syncMonitors / startMonitors / stopMonitors
**源码：** `graph_builder.go:250 / :303 / :332`
**职责：** syncMonitors 对新发现的可删除 GVR（跳过 ignored 与 events）建 monitor，对消失的 GVR 关 stopCh；startMonitors 等 informersStarted 后启动 controller。GC 侧封装为 garbagecollector.go:120 resyncMonitors。

### 函数：GetDeletableResources
**源码：** `garbagecollector.go:793`
**职责：** `ServerPreferredResources` 后用 `SupportsAllVerbs{delete/list/watch}` 过滤，展平成 GVR 集合；容忍部分 group 发现失败。

## 8. 队列使用
| 队列 | 入队元素 | 名称 |
|---|---|---|
| graphChanges | `*event` | garbage_collector_graph_changes |
| attemptToDelete | `*node` | garbage_collector_attempt_to_delete |
| attemptToOrphan | `*node` | garbage_collector_attempt_to_orphan |

## 9. 调用关系总览
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

[上一篇](14-daemonset控制器.md) · [总目录](README.md) · [下一篇](16-kube-proxy网络代理.md)
