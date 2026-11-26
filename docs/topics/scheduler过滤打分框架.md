[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# scheduler 过滤打分框架

> **层级**：跨篇机制纵深专题——把 kube-scheduler 调度周期里的**过滤（Filter）与打分（Score）插件框架**讲透：扩展点接口、frameworkImpl 执行器、候选集缩小→逐节点过滤→打分归一化→选节点的完整链路。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上用 `grep -n`/`sed -n` 复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `f.RunFilterPlugins+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[07-scheduler调度与绑定](../sourceReader/07-scheduler调度与绑定.md) §2.3 是浓缩版；本专题纵向展开过滤打分的插件执行语义。抢占见 [调度器抢占-preemption](调度器抢占-preemption.md)（PostFilter 扩展点）。

---

## 0. 结论速览

### 0.1 调度周期两阶段

一次 `SchedulePod`（algorithm.go `SchedulePod+0`，:162）分两阶段：
1. **过滤（Filter）**：从全部节点里筛出**可行节点集**（资源/端口/亲和/污点/卷都满足）。对应扩展点 PreFilter → Filter → PostFilter。
2. **打分（Score）**：给可行节点打分，选**最高分**节点。对应扩展点 PreScore → Score。

### 0.2 关键结论

1. **框架驱动**：过滤打分不是硬编码，而是**插件框架**——每个维度（资源、端口、亲和…）是一个插件，实现对应扩展点接口，frameworkImpl 执行器统一调度。
2. **PreFilter 可缩小候选集**：PreFilter 可返回节点子集（如 volume node affinity 限定），后续 Filter 只跑子集。
3. **过滤可并行**：`findNodesThatPassFilters` 用 `parallelize` 并行评估节点，结果聚合进 diagnosis。
4. **打分 = 插件分 × 权重 求和**：每个 Score 插件返回 0-100 分，按配置权重加权，`TotalScore` 求和，选最高分（平局随机）。
5. **单可行节点跳过打分**：只有 1 个可行节点（或 firstFit）时直接返回，跳过打分阶段。

### 0.3 扩展点一览（本专题覆盖 ★）

| 扩展点 | 接口 | 阶段 | 作用 |
|---|---|---|---|
| PreFilter ★ | `PreFilterPlugin`（:539） | 过滤前 | 一次预处理 + 可缩小候选集 |
| Filter ★ | `FilterPlugin`（:568） | 过滤 | 逐节点判断可行/不可行 |
| PostFilter ★ | `PostFilterPlugin`（:597） | 过滤失败后 | 抢占（见抢占专题） |
| PreScore ★ | `PreScorePlugin`（:657） | 打分前 | 一次预处理（如算节点亲和权重） |
| Score ★ | `ScorePlugin`（:678） | 打分 | 逐节点打 0-100 分 |
| Reserve | `ReservePlugin`（:695） | 选定后 | 预留资源（失败回滚） |
| Permit | `PermitPlugin`（:739） | 绑定前 | 批准/拒绝/等待 |
| PreBind | `PreBindPlugin`（:711） | 绑定前 | 预绑定准备（如 PV 绑定） |
| Bind | `BindPlugin`（:752） | 绑定 | 写 apiserver binding |
| PostBind | `PostBindPlugin`（:728） | 绑定后 | 清理/通知 |

---

## 1. 生产视角：一次调度可见什么

### 1.1 调度的输入输出

- **输入**：一个待调度 Pod（来自 PriorityQueue）+ 当前节点快照（`SnapshotSharedLister`）。
- **输出**：一个选定的 nodeName，经 `bind` 插件 POST `/api/v1/namespaces/<ns>/pods/<name>/binding` 写入 apiserver（持久化 `spec.nodeName`）。
- **失败**：无可行节点 → `FitError`（含 diagnosis）→ 触发 PostFilter（抢占）或标 Unschedulable。

### 1.2 可观测信号

- 事件 `Scheduled`（reason=Binding）/ `FailedScheduling`（Warning）。
- `FitError` 的 message 列出所有节点被哪些 Filter 插件拒绝（排障关键）。

---

## 2. 开发视角：过滤阶段

### 2.1 入口 `SchedulePod`（algorithm.go `SchedulePod+0`，:162）

```text
SchedulePod(ctx, fw, state, podInfo)
├── findNodesThatFitPod(...)          过滤：筛可行节点集
├── 无可行节点 → FitError（触发 PostFilter/抢占）
├── 1 个可行节点 → 直接返回（跳过打分）
└── prioritizeNodes(...)              打分：选最高分节点
```

### 2.2 `findNodesThatFitPod`（algorithm.go `a.findNodesThatFitPod+0`，:351）

```text
findNodesThatFitPod(ctx, fw, state, podInfo)
├── prefilterNodes（+2）：
│     └── RunPreFilterPlugins（framework.go f.RunPreFilterPlugins+0，:1119）
│           └── 可返回节点子集（缩小候选集）
└── findNodesThatPassFilters（+44）→ 见 §2.3
```

### 2.3 `findNodesThatPassFilters`（algorithm.go `a.findNodesThatPassFilters+0`，:449）

```text
findNodesThatPassFilters(ctx, fw, state, podInfo, nodes, diagnosis)
├── parallelize over nodes（并行评估）
│     └── RunFilterPluginsWithNominatedPods（调用点 algorithm.go:506，函数内偏移 +57）
│           ↑ 定义 frameworkImpl.RunFilterPluginsWithNominatedPods+0（framework.go:1467）
├── 每节点聚合 Filter 结果（可行 / 被哪个插件拒绝）
└── 结果进 diagnosis（FitError 时列出拒绝原因）
```

### 2.4 `RunFilterPluginsWithNominatedPods`（framework.go `f.RunFilterPluginsWithNominatedPods+0`，:1467）

带 nominated pod 重跑 Filter（抢占场景下，节点上可能有「假定被驱逐」的 pod，需模拟其移除后重判）。内部调 `RunFilterPlugins`（framework.go `f.RunFilterPlugins+0`，:1290）逐个跑 Filter 插件：
```text
RunFilterPlugins(ctx, state, pod, nodeInfo)
└── for _, pl := range filterPlugins:
     ├── st = pl.Filter(ctx, state, pod, nodeInfo)   逐插件判断
     └── 任一插件返回 Unschedulable → 该节点不可行
```

---

## 3. 开发视角：打分阶段

### 3.1 `prioritizeNodes`（schedule_one.go `prioritizeNodes+0`，:644）

```text
prioritizeNodes(ctx, sched, fw, state, pod, nodes, minRank)
├── 按 minRank 过滤最低排名节点（+14～+27）
├── RunPreScorePlugins（framework.go f.RunPreScorePlugins+0，:1547）（+43）
├── RunScorePlugins（framework.go f.RunScorePlugins+0，:1598）（+49）
│     └── 返回 nodesScores（每节点每插件分 + TotalScore）
├── 归一化 + 汇总 extender 分数（+49～）
└── 返回 nodesScores（供 selectHost 选最高分）
```

### 3.2 `RunScorePlugins`（framework.go `f.RunScorePlugins+0`，:1598）

```text
RunScorePlugins(ctx, state, pod, nodes) → []NodePluginScores
├── for each Score plugin (并行):
│     ├── pl.Score(ctx, state, pod, nodeInfo) → 0-100 分 per node
│     └── NormalizeScore（可选归一化到 0-100）
└── 汇总：每节点 TotalScore = Σ (插件分 × 权重)
```

**打分公式：** `TotalScore(node) = Σ_plugin (Score_plugin(node) × Weight_plugin)`。选 `TotalScore` 最大的节点；平局用 `Randomizer`（rand.Int）打破。

### 3.3 打分阶段的关键点

- **PreScore 可提前算权重**：如 `NodeAffinity` 的 Preferred 表达式权重在 PreScore 一次性算好，Score 时复用。
- **归一化**：Score 插件原始分可能不在 0-100，`NormalizeScore` 统一到 0-100 再加权。
- **跳过打分**：单可行节点直接返回（schedule_one.go :122 注释）。

---

## 4. 扩展点接口（插件怎么写）

| 扩展点 | 接口定义 | 核心方法 |
|---|---|---|
| PreFilter | `PreFilterPlugin`（interface.go:539） | `PreFilter(ctx, state, pod) → (PreFilterResult, Status)` |
| Filter | `FilterPlugin`（:568） | `Filter(ctx, state, pod, nodeInfo) → Status` |
| PostFilter | `PostFilterPlugin`（:597） | `PostFilter(ctx, state, pod, filteredNodeStatusMap) → (PostFilterResult, Status)` |
| PreScore | `PreScorePlugin`（:657） | `PreScore(ctx, state, pod, nodes) → Status` |
| Score | `ScorePlugin`（:678） | `Score(...) → (int64, Status)` + `NormalizeScore` |
| Reserve | `ReservePlugin`（:695） | `Reserve` / `Unreserve` |
| Permit | `PermitPlugin`（:739） | `Permit → (Code, duration)` |
| PreBind | `PreBindPlugin`（:711） | `PreBind(ctx, state, pod, nodeName) → Status` |
| Bind | `BindPlugin`（:752） | `Bind(ctx, state, pod, nodeName) → Status` |
| PostBind | `PostBindPlugin`（:728） | `PostBind(...)` |

**插件共享状态**：`CycleState`（cycle_state.go）是单个调度周期内插件间传状态的 KV——如 PreFilter 算的中间结果，Filter 阶段复用。

---

## 5. 全链路时序（一次过滤+打分）

```text
SchedulePod (algorithm.go:162)
├── [过滤阶段]
│    findNodesThatFitPod (algorithm.go:351)
│    ├── prefilterNodes → RunPreFilterPlugins (framework.go:1119)   缩小候选集
│    └── findNodesThatPassFilters (algorithm.go:449)
│         └── parallelize → RunFilterPluginsWithNominatedPods (:1467) 逐节点
│              └── RunFilterPlugins (:1290) → Filter 插件逐个判断
├── 无可行 → FitError → PostFilter（抢占）/ Unschedulable
├── 单可行 → 直接返回
└── [打分阶段]
     prioritizeNodes (schedule_one.go:644)
     ├── RunPreScorePlugins (framework.go:1547)
     ├── RunScorePlugins (framework.go:1598) → nodesScores（分×权重求和）
     └── selectHost → 最高分节点（平局随机）
          └── Reserve → Permit → PreBind → Bind（写 apiserver）→ PostBind
```

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `SchedulePod+0`（algorithm.go:162）、`a.findNodesThatFitPod+0`（:351）、`a.findNodesThatPassFilters+0`（:449）、过滤调用点（:506，+57）
- `prioritizeNodes+0`（schedule_one.go:644）、打分调用点 RunScorePlugins（:693，+49）
- `f.RunPreFilterPlugins+0`（framework.go:1119）、`f.RunFilterPlugins+0`（:1290）、`f.RunFilterPluginsWithNominatedPods+0`（:1467）、`f.RunPreScorePlugins+0`（:1547）、`f.RunScorePlugins+0`（:1598）
- 扩展点接口：`PreFilterPlugin`（interface.go:539）、`FilterPlugin`（:568）、`PostFilterPlugin`（:597）、`PreScorePlugin`（:657）、`ScorePlugin`（:678）、`ReservePlugin`（:695）、`PreBindPlugin`（:711）、`PostBindPlugin`（:728）、`PermitPlugin`（:739）、`BindPlugin`（:752）

### 6.2 生产实测

**未实测** `[待运行验证]`。并行过滤的并行度、打分归一化实际数值、平局随机行为均为代码推断，未在真实集群观测单次调度的插件耗时/分数分布。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| 各默认插件实现 | 未深挖 | NodeResourcesFit/NodeAffinity/InterPodAffinity 等具体 Filter/Score 插件的打分公式未逐行读 |
| parallelize 并行度 | 未深挖 | `parallelize` 的 goroutine 池大小与调度未展开 |
| extender 打分 | 未深挖 | HTTP extender 的分数合并未读 |
| selectHost | 未深挖 | 选节点与 `NodeWithRank` 排序细节未逐行读 |

### 6.4 修改联动

- 加/改 Filter 插件 → 实现 `FilterPlugin`（:568），注册到 profile，同步本文 §2.4。
- 加/改 Score 插件 → 实现 `ScorePlugin`（:678）+ 配权重，同步 §3.2。
- 改抢占 → PostFilter，同步 [调度器抢占-preemption](调度器抢占-preemption.md)。

**关联文档：** [07-scheduler调度与绑定](../sourceReader/07-scheduler调度与绑定.md)（调度全流程）、[调度器抢占-preemption](调度器抢占-preemption.md)（PostFilter）、[workqueue机制](workqueue机制.md)（PriorityQueue 侧）。

[总目录](../sourceReader/README.md)
