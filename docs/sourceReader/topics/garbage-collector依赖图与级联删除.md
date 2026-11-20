[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# garbage-collector 依赖图与级联删除

> **层级**：跨篇机制纵深专题——把 kube-controller-manager 的 **Garbage Collector** 讲透：owner/dependent 依赖图维护、孤儿与悬挂引用判定、Foreground/Background/Orphan 三种级联删除。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `gc.attemptToDeleteItem+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[15-garbage-collector垃圾回收](../15-garbage-collector垃圾回收.md) §4-6 是浓缩版；本专题纵向展开依赖图装配与删除判定语义。

---

## 0. 结论速览

### 0.1 GC 解决什么问题

Kubernetes 里对象通过 `ownerReferences` 声明「我属于谁」（如 ReplicaSet 是 Pod 的 owner）。**当 owner 被删除时，谁负责清理它的 dependents？** GC 就是这个自动清理者：维护一张 owner→dependent 依赖图，owner 消失后按策略清理 dependents。

### 0.2 三种级联删除策略

| 策略 | 行为 | 调用方是否等待 |
|---|---|---|
| **Foreground**（前台） | owner 先进「删除中」（`deletionTimestamp` 置位 + `foregroundDeletion` finalizer），**不立即消失**；GC 先删光所有 blocking dependent，再摘 finalizer，owner 才真正消失 | **同步等待**：owner 消失时 dependents 已全消失 |
| **Background**（后台，默认） | owner **立即消失**；dependents 由 GC 后台逐个删除 | 不等待；dependents 稍后消失 |
| **Orphan**（孤儿化） | owner 保留（带 `orphan` finalizer）；GC 把所有 dependent 的 `ownerReferences` 中的 owner 引用**摘除** | owner 保留；dependents 变无主对象独立存活 |

### 0.3 关键结论

1. **策略由 finalizer 决定**：GC 在 `attemptToDeleteItem` 读对象当前 finalizer（`hasOrphanFinalizer`/`hasDeleteDependentsFinalizer`）选策略，默认 Background。
2. **依赖图是唯一真相**：`GraphBuilder` 是依赖图的**唯一写者**，informer 事件 → `graphChanges` → 改图。
3. **两个 worker 队列**：`attemptToDelete`（删 dependents）+ `attemptToOrphan`（孤儿化摘 owner 引用）。
4. **悬挂引用判定**：owner 已不存在（UID 查不到）→ dependent 是 dangling → 删除。
5. **Foreground 用 finalizer 实现同步等待**：owner 带 `foregroundDeletion` finalizer 直到 dependents 删光。

---

## 1. 生产视角：用户看到什么

### 1.1 用户操作与可见结果

| 操作 | 结果 |
|---|---|
| `kubectl delete deploy` （默认 Background） | deploy 立即消失；其 RS、Pod 稍后被 GC 删 |
| `kubectl delete deploy --cascade=foreground` | deploy 进 Terminating；RS/Pod 先删光；deploy 才消失 |
| `kubectl delete deploy --cascade=orphan` | deploy 保留；RS/Pod 变无主（ownerReferences 摘除），独立存活 |

### 1.2 可观测信号

- Foreground 删除时 owner 卡在 Terminating（`deletionTimestamp` 置位 + finalizer），直到 dependents 删光。
- 误配 ownerReferences（指向不存在或跨命名空间 owner）→ dependent 被 GC 判 dangling 删除。

---

## 2. 开发视角：依赖图装配（GraphBuilder）

### 2.1 `GraphBuilder`——依赖图唯一写者（graph_builder.go `GraphBuilder+0`，:82）

`GraphBuilder` 持有 `uidToNode`（依赖图）、`graphChanges` 队列、两个 worker 队列引用。它是**唯一写依赖图**的组件。`NewDependencyGraphBuilder+0`（:141）构造。

### 2.2 `Run`——监视器 + 处理循环（graph_builder.go `gb.Run+0`，:391）

```text
Run(ctx)
├── startMonitors（:303）：为每种资源起 informer
└── for: processGraphChanges  消费 graphChanges 队列改依赖图
```

### 2.3 `processGraphChanges`——改依赖图（graph_builder.go `gb.processGraphChanges+0`，:710）

```text
processGraphChanges
├── 出队一个 event（ADD/UPDATE/DELETE）
├── insertNode / removeNode 更新 uidToNode
├── classifyReferences（garbagecollector.go gc.classifyReferences+0，:461）：
│     把对象的 ownerReferences 分为 existing / dangling（owner 已不存在）
├── addDependentToOwners（graph_builder.go gb.addDependentToOwners+0，:450）：
│     把 dependent 挂到 owner 节点
└── processTransitions（:675）：
      owner 删除 → dependents 入 attemptToDelete / attemptToOrphan 队列
```

### 2.4 finalizer 判定函数（graph_builder.go）

| 函数 | 锚点 | 判据 |
|---|---|---|
| `hasDeleteDependentsFinalizer` | `hasDeleteDependentsFinalizer+0`（:623） | 对象带 `foregroundDeletion` finalizer |
| `hasOrphanFinalizer` | `hasOrphanFinalizer+0`（:627） | 对象带 `orphan` finalizer |
| `hasFinalizer` | `hasFinalizer+0`（:631） | 带任一 GC finalizer |
| `startsWaitingForDependentsDeleted` | `startsWaitingForDependentsDeleted+0`（:638） | 进 Foreground 等待态 |
| `startsWaitingForDependentsOrphaned` | `startsWaitingForDependentsOrphaned+0`（:644） | 进 Orphan 等待态 |

---

## 3. 开发视角：删除判定（GarbageCollector）

### 3.1 两个 worker 队列

`GarbageCollector`（garbagecollector.go `GarbageCollector+0`，:61，`NewGarbageCollector+0` :80）持有两个队列：
- **attemptToDelete**：待删除的 dependents。worker：`processAttemptToDeleteWorker+0`（:294）→ `attemptToDeleteWorker+0`（:319）。
- **attemptToOrphan**：待孤儿化的。worker：`processAttemptToOrphanWorker+0`（:728）→ `attemptToOrphanWorker+0`（:746）。

### 3.2 `attemptToDeleteItem`——核心删除判定（garbagecollector.go `gc.attemptToDeleteItem+0`，:500）

```text
attemptToDeleteItem(ctx, item)
├── 取 item 当前状态（ownerReferences、finalizers、deletionTimestamp）
├── 按 finalizer 选策略：
│   ├── hasOrphanFinalizer → attemptToOrphan 队列（孤儿化）
│   ├── hasDeleteDependentsFinalizer（Foreground）→ 删 dependents，删光后摘 finalizer
│   └── 默认 Background → 直接删
├── isDangling（gc.isDangling+0，:380）：
│     owner 已不存在（UID 查不到）→ dependent 是悬挂 → 删除 dependent
└── 执行删除 / patch
```

### 3.3 孤儿化（`orphanDependents`）

`orphanDependents+0`（garbagecollector.go :680）：把 dependents 的 `ownerReferences` 中该 owner 的引用**摘除**，通过 JSON Merge Patch：
- `deleteOwnerRefJSONMergePatch+0`（patch.go :109）：构造移除 ownerRef 的 patch。
- owner 摘光所有 dependents 的引用后，摘掉自己的 `orphan` finalizer。

---

## 4. 三种级联删除的实现路径

### 4.1 Background（默认）

```text
owner 删除（无 GC finalizer）
└── GC 判 dependents 的 owner 已消失（dangling）
     └── attemptToDelete 队列 → 逐个删 dependents
          └── owner 已立即消失（不等 dependents）
```

### 4.2 Foreground（同步等待）

```text
owner 删除（带 foregroundDeletion finalizer）
├── owner 不立即消失（deletionTimestamp 置位 + finalizer）
├── GC 删所有 blocking dependents
├── dependents 删光 → 摘 owner 的 foregroundDeletion finalizer
└── owner 才真正消失（调用方同步等待到这一刻）
```

### 4.3 Orphan（孤儿化）

```text
owner 删除（带 orphan finalizer）
├── owner 不立即消失（orphan finalizer）
├── attemptToOrphan 队列 → orphanDependents（:680）
│    └── deleteOwnerRefJSONMergePatch (patch.go:109) 摘每个 dependent 的 owner 引用
├── 所有引用摘光 → 摘 owner 的 orphan finalizer → owner 消失
└── dependents 变无主对象，独立存活
```

---

## 5. 全链路时序（Background 删除 Deployment）

```text
用户删 Deployment
└── apiserver 删 deploy 对象（无 GC finalizer → Background）
     └── GraphBuilder 监视到 deploy DELETE → processGraphChanges (:710)
          ├── deploy 节点移除
          ├── 其 dependents（RS）的 owner 消失 → 判 dangling（isDangling :380）
          └── RS 入 attemptToDelete 队列
               └── attemptToDeleteWorker (:319) → attemptToDeleteItem (:500)
                    └── RS 是 Pod 的 owner → Pod 入 attemptToDelete → 逐层级联
                         └── deploy 已消失，RS/Pod 后台逐个删光
```

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `GarbageCollector+0`（garbagecollector.go:61）、`NewGarbageCollector+0`（:80）、`Run+0`（:129）
- `processAttemptToDeleteWorker+0`（:294）、`attemptToDeleteWorker+0`（:319）、`attemptToDeleteItem+0`（:500）、`classifyReferences+0`（:461）、`isDangling+0`（:380）、`orphanDependents+0`（:680）、`processAttemptToOrphanWorker+0`（:728）、`attemptToOrphanWorker+0`（:746）
- `deleteOwnerRefJSONMergePatch+0`（patch.go:109）
- `GraphBuilder+0`（graph_builder.go:82）、`NewDependencyGraphBuilder+0`（:141）、`gb.Run+0`（:391）、`gb.addDependentToOwners+0`（:450）、`gb.insertNode+0`（:547）、`gb.processGraphChanges+0`（:710）、`gb.processTransitions+0`（:675）
- `hasDeleteDependentsFinalizer+0`（:623）、`hasOrphanFinalizer+0`（:627）、`hasFinalizer+0`（:631）、`startsWaitingForDependentsDeleted+0`（:638）、`startsWaitingForDependentsOrphaned+0`（:644）

### 6.2 生产实测

**未实测** `[待运行验证]`。三种策略的实际时序、Foreground 同步等待窗口、dangling 判定的 UID cache 行为均为代码推断，未在真实集群观测删除时序。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| blocking vs non-blocking dependent | 未深挖 | `partitionDependents`（graph_builder.go:895）区分 blocking dependents 的细节未读 |
| uid_cache | 未深挖 | `uid_cache.go` 防重复删除的 UID 缓存未展开 |
| 跨命名空间 owner 判定 | 未深挖 | cluster-scoped owner 的 namespace 处理未读 |
| 动态资源（CRD）GC | 未深挖 | CRD 资源的 GC 监视器动态注册未展开 |

### 6.4 修改联动

- 改依赖图装配 → `processGraphChanges`/`addDependentToOwners`，同步本文 §2。
- 改删除策略判定 → `attemptToDeleteItem`，同步 §3.2。
- 改孤儿化 → `orphanDependents`/`deleteOwnerRefJSONMergePatch`，同步 §4.3。

**关联文档：** [15-garbage-collector垃圾回收](../15-garbage-collector垃圾回收.md)（GC 全貌）、[12-deployment控制器](../12-deployment控制器.md)（Deployment→RS→Pod 依赖链）、[08-controller副本控制](../08-controller副本控制.md)（ReplicaSet）。

[总目录](../README.md)
