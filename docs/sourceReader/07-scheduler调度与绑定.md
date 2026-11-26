[上一篇](06-apiserver请求处理与etcd.md) · [总目录](README.md) · [下一篇](08-controller副本控制.md)

# scheduler 调度与绑定

> **层级**：第四层——函数级源码解析。
> **范围**：调度器构造与运行、调度周期（PreFilter/Filter/PostFilter/Score）、assume、绑定周期（Permit/PreBind/Bind）、队列交互。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18），Go 1.27.4。
> **记法**：源码锚点用「符号+偏移」；函数定义行 = +0，函数内动作偏移 = 相对定义行。类型用「类型名 文件:行号」。

## 1. 生产视角

### 1.1 调度对外角色

`kube-scheduler` 是集群控制面里唯一负责「为未绑定 Pod 选择节点」的组件（代码推断：`cmd/kube-scheduler/app` 的 `NewSchedulerCommand` → `Options.Config` → `scheduler.New`）。它只产出决策结果，不运行 Pod 容器：

- **输入**：监听 apiserver 中 `spec.nodeName` 为空的 Pod 与全量 Node（Pod informer → `PriorityQueue`）。
- **输出**：经由 apiserver 的 binding 子资源持久化 `spec.nodeName`（`bind` 内 `RunBindPlugins`，默认 DefaultBinder 插件 POST `/api/v1/namespaces/<ns>/pods/<name>/binding`）。
- **副作用**：调度缓存先行占位（assume），使同周期内其他 Pod 不会超额分配同一节点资源；必要时执行抢占（PostFilter），把低优先级 Pod 挤出以腾出资源。

### 1.2 一次调度可见结果

| 结局 | 对 Pod 的可见变化 | 对队列/缓存的变化 |
|---|---|---|
| 成功 | `spec.nodeName` 被写入（binding 子资源），Pod 进入目标节点运行准备 | 调度缓存已 assume（Pod 占节点），`Done` 释放队列中事件 |
| 不可调度（无节点/Filter 全失败） | 无 `nodeName` 写入；状态带原因回队列 | Pod 进 Unschedulable 队列（带 nominatingInfo 供抢占候选）；有 PostFilter 则尝试抢占 |
| 绑定阶段失败（Permit 后） | 无 `nodeName` 写入 | assume 被 Unreserve/Forget 撤销，Pod 进 BackoffQ 指数退避重试 |

## 2. 开发视角

### 2.1 模块级

| 项 | 内容 |
|---|---|
| 入口 | `cmd/kube-scheduler`（main）、`cmd/kube-scheduler/app`（NewSchedulerCommand、Run、Options.Config → scheduler.New） |
| 核心 | `pkg/scheduler`：Scheduler、SchedulingAlgorithm、scheduleOne、framework 装配、backend/queue |
| 框架接口 | `pkg/scheduler/framework`（Framework、CycleState、插件接口与状态码）、`framework/runtime`（frameworkImpl 执行器） |
| 依赖 | `[staging] client-go`（informer、client）、`[staging] k8s.io/kube-scheduler`（配置类型） |
| 数据流 | Pod informer → PriorityQueue → ScheduleOne → 调度周期(快照/过滤/打分) → assume → 绑定周期(写 apiserver) |

### 2.2 类型级

| 类型 | 文件 | 职责 |
|---|---|---|
| `Scheduler` | `pkg/scheduler/scheduler.go:67` | 顶层调度器：持有 Cache、NextEntity、FailureHandler、SchedulePod、SchedulingQueue、APIDispatcher、Profiles、algorithm |
| `SchedulingAlgorithm` | `pkg/scheduler/algorithm.go:54` | 同步算法：SchedulePod（PreFilter/Filter/Score 的编排）、assume/unreserve |
| `SchedulingQueue`（interface） | `pkg/scheduler/backend/queue/scheduling_queue.go:99` | 待调度实体队列；实现 `PriorityQueue`（`:204`） |
| `Framework`（interface） | `pkg/scheduler/framework/interface.go:381` | 插件运行时接口：RunPreFilter/RunFilter/RunPostFilter/RunScore/RunPermit/RunPreBind/RunBind |
| `QueuedPodInfo` | `pkg/scheduler/framework/types.go:740` | 队列中的 Pod 包装（Pod、Attempts、InitialAttemptTimestamp） |
| `ScheduleResult` | `pkg/scheduler/scheduler.go:183` | SuggestedHost、EvaluatedNodes、FeasibleNodes、nominatingInfo |
| `CycleState` | `pkg/scheduler/framework/cycle_state.go:29` | 单个调度周期内插件共享的状态 KV |

### 2.3 函数级

#### 函数：New

**源码：** `New+0`
**职责：** 装配 Scheduler：构造 Pod/PodGroup informer 的事件处理、队列、framework profiles、Cache、algorithm。
**返回值：** `(*Scheduler, error)`。
**关键动作：** 事件处理器（`eventhandlers.go`）把 Pod 的 add/update/delete 映射为队列操作（AddPod/UpdatePod/DeletePod）。

#### 函数：(*Scheduler).Run

**源码：** `sched.Run+0`
**执行流程：**
1. `SchedulingQueue.Run`（+2）：启动 heap 搬运与调度周期计数
2. `APIDispatcher.Run`（+5，可选；`SchedulerAsyncAPICalls` 特性门开启时非 nil）
3. `go wait.UntilWithContext(ctx, sched.ScheduleOne, 0)`（+14）：独立 goroutine 跑 worker（注释说明同 goroutine 会在 shutdown 时与队列关闭形成死锁，+8 ～ +13）
4. 阻塞在 `<-ctx.Done()`（+16）
5. 关闭顺序：dispatcher → queue.Close → Profiles.Close（+18 ～ +23）
**状态变化：** 从装配态进入持续 Pop/调度循环。

#### 函数：(*Scheduler).ScheduleOne

**源码：** `sched.ScheduleOne+0`
**职责：** 单个 worker 的循环体：取实体并分派。
**执行流程：**
1. `sched.NextEntity(logger)`：阻塞 Pop（+2）
2. nil/错误检查（+3 ～ +10）：err → HandleError 记录后返回；nil → 队列已关闭直接返回
3. type switch（+12 ～ +22）：`QueuedPodGroupInfo` → `scheduleOnePodGroup`；`QueuedPodInfo` → `scheduleOnePod`；default 记录非法实体
**错误处理：** Pop 错误经 HandleError 记录后继续下一轮，不退出 worker。

#### 函数：(*Scheduler).scheduleOnePod

**源码：** `sched.scheduleOnePod+0`
**职责：** 单个 Pod 的端到端调度编排。
**执行流程：**
```text
scheduleOnePod (+0)
├── 1. frameworkForPod 选 profile (+10 ～ +11)
├── 2. skipPodSchedule（已带 nodeName 等场景）(+18 ～ +22)
├── 3. NewCycleState + 采样标记 + PodsToActivate (+28 ～ +36)
├── 4. schedulingCycle（同步，+41）
│      失败 → runFailureHandler 后返回 (+42 ～ +44)
└── 5. go runBindingCycle（异步，+48）
```
**关键并发设计：** 绑定周期放独立 goroutine；正确性依赖第 4 步中的 assume——调度缓存已先认为 Pod 占用节点，后续 Pod 调度不会超额分配同一资源（+47 注释）。

#### 函数：(*Scheduler).schedulingCycle

**源码：** `sched.schedulingCycle+0`
**执行流程：** `Cache.UpdateSnapshot`（+8）刷新节点快照 → `refreshResizingPodFromCache`（+12，deferred resize Pod 从缓存快照回填最新资源）→ `schedulingAlgorithm`（+16）→ `prepareForBindingCycle`（+21，assume + Permit）。

#### 函数：(*Scheduler).schedulingAlgorithm

**源码：** `sched.schedulingAlgorithm+0`
**职责：** 执行 SchedulePod 并处理失败：无节点/FitError → PostFilter（抢占）。
**关键分支：**
- defer 延迟指标（+7 ～ +13）：deferred resize Pod 走 `ResizeSchedulingAlgorithmLatency`，其余走 `SchedulingAlgorithmLatency`
- `SchedulePod`（+18）
- ErrNoNodesAvailable → UnschedulableAndUnresolvable（+20 ～ +23）
- FitError 且有 PostFilter 插件 → `RunPostFilterPlugins` 尝试抢占，返回 nominatingInfo（+40 ～ +59）；无 PostFilter 直接 Unschedulable（+40 ～ +42）
**返回值：** ScheduleResult 带 nominatingInfo；状态非成功时上层走 FailureHandler（回 Active/Backoff 队列）。

#### 函数：(*SchedulingAlgorithm).SchedulePod

**源码：** `a.SchedulePod+0`
**职责：** 同步算法主体。
**执行流程：**
1. 快照无节点 → ErrNoNodesAvailable（+5 ～ +6）
2. `findNodesThatFitPod`：PreFilter + Filter（+9）
3. 无可行节点 → FitError（带 diagnosis，+15 ～ +21）
4. 仅 1 个可行节点（或 firstFit）：直接返回，跳过打分（+24 ～ +40）
5. 多节点：`prioritizeNodes`（+42）→ `NewSortedScoredNodes(...).Pop()` 取最高分（+47 ～ +48）
6. （可选）opportunistic batching 存储结果（+32 ～ +34、+51 ～ +53）
**返回值：** ScheduleResult{SuggestedHost, EvaluatedNodes, FeasibleNodes}。

#### 函数：(*SchedulingAlgorithm).findNodesThatFitPod / prefilterNodes

**源码：** `a.findNodesThatFitPod+0 / :252`
**职责：** PreFilter 缩小候选集（可能返回节点子集），Filter 逐节点执行过滤。
**关键数据结构：** `preFilterOutcome`（result 候选子集、allNodes 全量快照、diagnosis），保留全量列表用于维护 nextStartNodeIndex 防止节点饥饿（`preFilterOutcome+0` 注释）。
**关键子函数：** `findNodesThatPassFilters`（`a.findNodesThatPassFilters+0`）：内部对每个节点调用 `RunFilterPluginsWithNominatedPods`（`a.findNodesThatPassFilters+57`，+57），不再直接位于 findNodesThatFitPod。过滤可并行评估节点（`parallelize`）；结果按节点聚合进 diagnosis。
**调用链：** `findNodesThatFitPod`（+2 prefilterNodes）→ 候选集 → `findNodesThatPassFilters`（+44）→ `RunFilterPluginsWithNominatedPods`（定义 `framework/runtime/framework.go:f.RunFilterPluginsWithNominatedPods+0`）。

#### 函数：prioritizeNodes

**源码：** `prioritizeNodes+0`
**职责：** 打分阶段：先按 minRank 过滤最低排名节点（+14 ～ +27），`RunPreScorePlugins`（+43）→ `RunScorePlugins`（+49）对可行节点打分，归一化并汇总 extender 分数。
**返回值：** `[]NodeScore`。
**调用链：** `RunScorePlugins` 调用在 `prioritizeNodes+49`（prioritizeNodes 内 +49），定义 `framework/runtime/framework.go:f.RunScorePlugins+0`。

#### 函数：(*Scheduler).prepareForBindingCycle

**源码：** `sched.prepareForBindingCycle+0`
**执行流程：** `algorithm.AssumeAndReserveInCache`（+8，改 nodeName 写入调度缓存）→ `RunPermitPlugins`（+15）：Wait → AddWaitingPod；Reject → Unreserve 回滚并返回 FitError（+20 ～ +35）。

#### 函数：(*Scheduler).runBindingCycle / bindingCycle

**源码：** `sched.runBindingCycle+0 / :352`
**职责：** 绑定周期全流程。
**runBindingCycle 执行流程：** `bindingCycle`（+14）；失败 → `handleBindingCycleError`（+16）。
**bindingCycle 执行流程：**
```text
bindingCycle (+0)
├── 1.（可选）RunPreBindPreFlights；必要时先写 NominatedNodeName (+15 ～ +31)
├── 2. WaitOnPermit (+35 ～ +49)
├── 3. SchedulingQueue.Done(UID)：尽早释放队列中事件（deferred resize 延迟到 bind 后）(+60)
├── 4. AddPodInPreBind（抢占优化）(+69)
├── 5. RunPreBindPlugins (+72 ～ +74)
├── 6. 校验未在 PreBind 期间被抢占 (+77 ～ +81)
├── 7. bind (+84)
└── 8. 成功指标/日志 → RunPostBindPlugins (+92 ～ +106)
```
**状态语义（注释 +51 ～ +53）：** Permit 之后的失败归入"可重试"（BackoffQ），不再标记为 Unschedulable。

#### 函数：(*Scheduler).bind

**源码：** `sched.bind+0`
**职责：** 执行实际绑定。
**优先级：** 先 `extendersBinding`（+6）；未绑定则 `RunBindPlugins`（+10）。
**默认 bind 插件：** 向 apiserver POST Pod 的 binding 子资源（`/api/v1/namespaces/<ns>/pods/<name>/binding`），apiserver 据此持久化 `spec.nodeName`。
**defer：** `finishBinding`（+2 ～ +4）处理绑定收尾（成功/失败的缓存一致性）。

### 2.4 状态与缓存一致性

| 时刻 | 调度缓存 | apiserver/etcd |
|---|---|---|
| 调度周期完成 | 无变化 | Pod nodeName 空 |
| Assume | Pod 已占节点（SuggestedHost）；deferred resize Pod 跳过 assume | Pod nodeName 空 |
| Bind 成功 | 与 etcd 一致 | spec.nodeName 已更新 |
| Bind 失败 | Unreserve/Forget 撤销 assume | 无变化 |

**assume 语义（`algorithm.go`）：**
- `AssumeAndReserveInCache`（`:770`）：deferred resize Pod（`InPlacePodVerticalScalingSchedulerPreemption` 特性门）跳过，直接 DeepCopy（+4 ～ +7）；否则 `AssumePod` 写缓存（+11）+ 运行 Reserve 插件（+21）；失败回滚 `UnreserveAndForgetFromCache`（+22）。
- `UnreserveAndForgetFromCache`（`:801`）：deferred resize Pod 直接返回（+3 ～ +6）；否则 `RunReservePluginsUnreserve`（+9）+ `ForgetPod`（+12）。

### 2.5 错误处理汇总

| 失败点 | 处理 |
|---|---|
| 无节点 / Filter 全失败 | PostFilter 抢占 → 回队列（Unschedulable，带 nominating） |
| Permit Reject | Unreserve 撤销 assume，FitError |
| PreBind/Bind 失败 | 撤销 assume，Pod 进 BackoffQ 指数退避重试 |
| 写 NominatedNodeName 失败 | 记录日志，不中断绑定（非关键路径，bindingCycle +28 ～ +31） |
| PreBind 期间被抢占 | context.Cause 上抛，绑定中止 |

### 2.6 抢占（Preemption）深度拆解

抢占是 Filter 全失败后的兜底路径：当所有节点都因资源/亲和不足判 Unschedulable 时，调度器选一批「低优先级受害者 Pod」驱逐，为高优先级 Pod 腾出空间。入口是 PostFilter 扩展点。

**触发点：** `sched.schedulingAlgorithm+46` 调 `RunPostFilterPlugins(ctx, state, pod, fitError.Diagnosis.NodeToStatus)`；`RunPostFilterPlugins` 定义在 `framework/runtime/framework.go:f.RunPostFilterPlugins+0`。默认 PostFilter 插件为 `DefaultPreemption`（`plugins/defaultpreemption/default_preemption.go`，`Name = names.DefaultPreemption` +46，`var _ fwk.PostFilterPlugin` +105，同时实现 `fwk.PodGroupPostFilterPlugin` +107）。

**完整链路（`pkg/scheduler/framework/preemption/` + `plugins/defaultpreemption/`）：**

```text
PostFilter (pl.PostFilter+0)
  └─ Evaluator.Preempt (ev.Preempt+0)
       ├─ evaluate (ev.evaluate+0)
       │    ├─ 0) PodLister 取最新 preemptor；deferred resize 的新鲜度校验
       │    │     + NominatedNodeName 内存修正（防重复抢占）
       │    ├─ 1) PodEligibleToPreemptOthers (pl.PodEligibleToPreemptOthers+0) 资格检查
       │    ├─ 2) findCandidates (ev.findCandidates+0)
       │    │     ├─ NodesForStatusCode(Unschedulable) 筛出"删点 Pod 就能放下"的节点
       │    │     ├─ getPodDisruptionBudgets 取 PDB
       │    │     ├─ GetOffsetAndNumCandidates (pl.GetOffsetAndNumCandidates+0) 采样
       │    │     └─ DryRunPreemption (ev.DryRunPreemption+0) 并行试算受害者
       │    ├─ 3) callExtenders (ev.callExtenders+0) 让扩展器增删候选
       │    └─ 4) SelectCandidate (ev.SelectCandidate+0) → pickOneNodeForPreemption (:383)
       └─ 5) ActuatePodPreemption (e.ActuatePodPreemption+0) 真正驱逐
            ├─ EnableAsyncPreemption → prepareCandidateAsync (e.prepareCandidateAsync+0, KEP-4832)
            └─ 否则 prepareCandidate (e.prepareCandidate+0) 同步驱逐
       返回 NewPostFilterResultWithNominatedNode(bestCandidate.Name())
```

**关键函数：**

| 函数 | 位置 | 职责 |
|---|---|---|
| `PostFilter` | `pl.PostFilter+0` | 扩展点入口；递增 `PreemptionAttempts`（deferred resize 走 `ResizePreemptionAttempts`）；转调 `Evaluator.Preempt` |
| `Evaluator.Preempt` | `ev.Preempt+0` | 编排 evaluate→actuate；成功返回带 NominatedNode 的 PostFilterResult |
| `Evaluator.evaluate` | `ev.evaluate+0` | 0 取最新 preemptor → 1 资格 → 2 findCandidates → 3 extenders → 4 SelectCandidate |
| `PodEligibleToPreemptOthers` | `pl.PodEligibleToPreemptOthers+0` | 抢占资格：优先级、是否已 nominated、preemptionPolicy |
| `findCandidates` | `ev.findCandidates+0` | 只考虑 `NodesForStatusCode(Unschedulable)` 的节点；无候选则提前返回 |
| `DryRunPreemption` | `ev.DryRunPreemption+0` | 并行对候选节点试算「驱逐哪些 Pod 能放下 preemptor」，产出 `PreemptionCandidate` |
| `SelectCandidate` | `ev.SelectCandidate+0` | 多候选时选一个节点 |
| `pickOneNodeForPreemption` | `pickOneNodeForPreemption+0` | 打分选节点 |
| `ActuatePodPreemption` | `e.ActuatePodPreemption+0` | 同步/异步驱逐受害者 |
| `prepareCandidateAsync` | `e.prepareCandidateAsync+0` | 异步驱逐（KEP-4832）：goroutine 驱逐 + 拒绝 waitingPod + 清低优 nominatedNodeName；完成前 Pod 不重试 |

**选节点 6 条 tie-break（`pickOneNodeForPreemption` 注释 +383）：** scoreFuncs 为空时按——① PDB 违规最少 → ② 最高优先级受害者最小 → ③ 所有受害者优先级之和最小 → ④ 受害者数量最少 → ⑤ 最高优先级受害者启动时间最晚 → ⑥ 仍平则取首个节点。`OrderedScoreFuncs`（`pl.OrderedScoreFuncs+0`）可注入自定义打分。

**受害者与 PDB：** `defaultPreemptionManager.GenerateVictims`（`m.GenerateVictims+0`）、`CompareVictimSets`（`:90`）按 PDB 违规数比较（`countPDBViolations` :151）；`victims.go` 提供 `getWorkloadPreemptionVictims`（:74，Workload API 聚合）与 `getCrossNodesVictims`（:87，跨节点收集）。

**被抢占方状态：** `prepareCandidate` 驱逐受害者后，preemptor 的 `Status.NominatedNodeName` 被写为目标节点，Pod 回队列；下一轮调度 `PodEligibleToPreemptOthers` 凭该字段避免对同一节点重复抢占；若驱逐未完成，`bindingCycle` 的「PreBind 期间被抢占」分支以 `context.Cause` 中止绑定。

**设计要点（代码推断）：**
1. **抢占只在 Filter 全失败后触发**（PostFilter 扩展点），不打断常规调度；这是「先试正常调度，再抢占」的分层兜底。
2. **DryRun 先试算、Actuate 后驱逐**：先并行算出「驱逐哪些 Pod 可行」，选中后再真正驱逐，避免误删。
3. **异步驱逐（KEP-4832）**：`EnableAsyncPreemption` 下驱逐放 goroutine，`prepareCandidateAsync` 完成前 Pod 不重试，避免驱逐未落地就重复调度。

### 2.7 一次成功调度决策走读（第二层补充视角）

本节是抢占（§2.6）的成功路径对照：走一个**资源充足、可正常调度**的 Pod，看 Filter/Score/Bind 三个阶段各插件实际做了什么检查、打出什么分。抢占只在 Filter 全失败时触发；本节是 Filter 有可行节点的常态路径。

#### 注册的插件与阶段（plugin registry）

**源码：** `pkg/scheduler/framework/plugins/registry.go`
`registry.go` 按扩展点注册约 20 个内置插件，关键的 Filter/Score/Bind 插件：

| 扩展点 | 插件 | 作用 |
|---|---|---|
| QueueSort | `queuesort` | 队列内 Pod 排序（优先级/时间） |
| PreFilter | `noderesources`、`podtopologyspread`、`interpodaffinity`、`volumebinding`、`schedulinggates` | 预筛 + 写 CycleState |
| Filter | `nodename`、`nodeunschedulable`、`tainttoleration`、`nodeaffinity`、`nodeports`、`noderesources`、`volumerestrictions`、`volumezone`、`interpodaffinity`、`podtopologyspread` | 逐节点判可行 |
| PreScore / Score | `nodeaffinity`、`podtopologyspread`、`interpodaffinity`、`noderesources.BalancedAllocation`、`imagelocality` | 打分 |
| Bind | `defaultbinder` | 写 Binding |

**Filter 阶段：逐插件判「这个节点能不能放」**

`noderesources.Fit.Filter`（`noderesources/fit.go:f.Filter+0`）：
- 从 CycleState 取 `PreFilter` 算好的 `preFilterState`（Pod 的资源请求）。
- 调 `helper.FitsRequest`（`helper/resource_fit.go:FitsRequest+0`）逐资源比对：
  - **Pod 数上限**：`len(nodeInfo.GetPods()) >= allowedPodNumber` → `"Too many pods"`。
  - **CPU**：`podRequest.MilliCPU > allocatable.MilliCPU - requested.MilliCPU` → `"Insufficient cpu"`；若请求 > 节点总量则标 `Unresolvable`（抢占也救不了）。
  - **内存**：同构判断 → `"Insufficient memory"`。
  - **EphemeralStorage / ScalarResources**：同构。
- 任一资源不足 → 返回 Unschedulable（带全部失败原因，供抢占/事件用）。

`tainttoleration.Filter`（`tainttoleration/taint_toleration.go:pl.Filter+0`）：
- `FindMatchingUntoleratedTaint`（:87/:90）检查节点 taint 是否被 Pod toleration 容忍；`DoNotScheduleTaintsFilterFunc()` 只看 NoSchedule/NoExecute。

`nodeaffinity.Filter` / `nodeunschedulable.Filter` / `nodename.Filter`：分别判 required node affinity、`spec.unschedulable`、`spec.nodeName`。

**Score 阶段：逐插件给「这个节点有多好」打分**

`noderesources.BalancedAllocation.Score`（`balancedResourceScorer+0 balancedResourceScorer`）：
- 同时算「加了这个 Pod 后」与「加之前」的 CPU/内存占用均衡度（`balancedResourceScore`，按各资源 fraction 的标准差）。
- 分数 = `MaxNodeScore/2 + (MaxNodeScore/2 + scoreWithPod - scoreWithoutPod)/2`：
  - 均衡度变化小 → 约 75。
  - 均衡度改善 → 趋向 100。
  - 均衡度变差 → 趋向 50。
- 这样选出的节点是「加完 Pod 后资源最均衡」的，而非单纯最空的。

`nodeaffinity` / `podtopologyspread` / `interpodaffinity` / `imagelocality` Score：分别按 preferred affinity、拓扑分布均衡度、跨 Pod 亲和、镜像本地性加分。

**Bind 阶段：写 Binding**

`defaultbinder.Bind`（`defaultbinder/default_binder.go:b.Bind+0`）：
```text
Bind(ctx, state, pod, nodeName)
├── 构造 v1.Binding{ObjectMeta{ns,name,uid}, Target{Kind:"Node", Name:nodeName}}
├── APICacher 可用 → APICacher().BindPod(binding) + WaitOnFinish（快路径）
└── 否则 util.BindPod(ctx, ClientSet(), binding)（POST binding 子资源）
```
**关键：** Bind 优先走 `APICacher().BindPod`——通过 apiserver 的 watch 缓存直写，绕过一次 list；不可用时回落到经典 `util.BindPod`（POST `pods/<name>/binding`）。

#### 一次成功调度的三阶段对照

| 阶段 | 插件做什么 | 通过判据 | 失败表现 |
|---|---|---|---|
| Filter | 资源够不够、污点容不容忍、亲和匹配不匹配、端口冲不冲突 | 至少一个节点全插件通过 | 全失败 → 触发抢占（§2.6） |
| Score | 资源均衡度、拓扑、亲和、镜像本地性打分 | 选出最高分节点 | 平分则 tie-break |
| Bind | 写 Binding（APICacher 快路径或 binding 子资源） | `spec.nodeName` 持久化 | Bind 失败 → 撤销 assume，回队重试 |

**设计要点（源码可证）：**
1. **Filter 与 Score 分离**：Filter 是硬约束（二值，通过/失败），Score 是软偏好（0-100 连续）。资源够但「不够均衡」的节点不会被 Filter 排除，只在 Score 上失分。
2. **BalancedAllocation 同时看前后**：不只看「加完 Pod 后多均衡」，还看「加这个 Pod 改善了多少均衡」——避免总是押注同一个最空节点。
3. **Bind 走缓存快路径**：`APICacher().BindPod` 绕过一次 list，减少 apiserver 读放大；经典路径作回落。
4. **抢占是 Filter 全失败的兜底**：本节是常态成功路径；抢占（§2.6）只在 Filter 全失败时触发，二者互补覆盖「资源够」与「资源不够」两种场景。

## 3. 设计意图与验证

### 3.1 设计意图（代码推断）

1. **调度周期同步、绑定周期异步**：`scheduleOnePod` 内 `go runBindingCycle`（+48）把绑定放独立 goroutine。正确性依赖调度周期末的 assume——调度缓存已先认为 Pod 占用节点，后续 Pod 调度不会超额分配同一资源（+47 注释）。
2. **assume 先行占位**：`AssumeAndReserveInCache` 把决策结果写入调度缓存（`AssumePod`），使同一轮调度内其他 Pod 看到的节点资源已含本次占用，避免超额分配；绑定失败时 `UnreserveAndForgetFromCache` 回滚。
3. **失败分级**：Permit 之前失败归入「不可调度」（Unschedulable，可触发抢占）；Permit 之后（PreBind/Bind）失败归入「可重试」（BackoffQ 指数退避）——bindingCycle 注释（+51 ～ +53）明确了该分界。
4. **deferred resize Pod 特殊路径**：Pod 已绑定且缓存已计数其资源，故调度/assume/绑定各阶段均跳过常规写入，仅刷新缓存快照并直接绑定（`refreshResizingPodFromCache`、`AssumeAndReserveInCache` +4 ～ +7、`UnreserveAndForgetFromCache` +3 ～ +6）。

### 3.2 验证

- **单元测试**：`make test WHAT=./pkg/scheduler GOFLAGS=-v`
- **集成测试**：`make test-integration WHAT=./test/integration/scheduler`
- **人工观测（生产实测）**：`kubectl get pod -o wide` 观察 NODE 列是否出现目标节点；`kubectl describe pod <name>` 观察 Events 中调度成功/失败记录（含插件原因与 `Scheduled` 状态）。

## 4. 与旧版差异

- **文件归属修正**：`CycleState` 现于 `framework/cycle_state.go:29`（旧误归 interface.go）；`ScheduleResult` 现于 `ScheduleResult+0`（旧误归 schedule_one.go）。
- **函数行号漂移**：全部函数定义行按当前基准重定（如 `Run` :456 → :538、`bind` :681 → :780、`prioritizeNodes` :565 → :644）。
- **函数内偏移漂移**：`scheduleOnePod` 内 `go runBindingCycle` +73 → +48；`schedulingAlgorithm` 内 `ErrNoNodesAvailable` +5 ～ +8 → +20、`FitError/PostFilter` +31/+49 → +40/+46；`SchedulePod` 内 `prioritizeNodes` +36 → +42、`Pop` +42 ～ +43 → +47 ～ +48、单节点跳过打分 +24 ～ +34 → +24 ～ +40；`prepareForBindingCycle` 内 `AssumeAndReserveInCache` +12 → +8、`RunPermitPlugins` +22 → +15；`bindingCycle` 内 `RunPreBindPreFlights` +12 ～ +31 → +15、`WaitOnPermit` +33 ～ +48 → +35 ～ +49、`Done` +56 → +60、`AddPodInPreBind` +63 ～ +69 → +69、`RunPreBindPlugins` +72 ～ +74、被抢占校验 +77 ～ +81、`bind` +84；`bind` 内 `extendersBinding` +6、`RunBindPlugins` +9 → +10。
- **PodGroup 调度整体拆出**：`scheduleOnePodGroup`（`sched.scheduleOnePodGroup+0`）、`podGroupCycle`（`:340`），本场景裸 Pod 不走该路径；旧文「schedulingAlgorithm 内 PodGroup 跳过 PostFilter」已不存在，从正文删除。
- **assume 语义更新**：`AssumeAndReserveInCache`（`a.AssumeAndReserveInCache+0`）= `AssumePod` 写缓存 + Reserve 插件，失败回滚 `UnreserveAndForgetFromCache`（`:801`）；deferred resize Pod 跳过 assume。
- **新增**：`schedulingCycle` 内插 `refreshResizingPodFromCache`（定义 `sched.refreshResizingPodFromCache+0`，调用 +12）；`schedulingAlgorithm` 顶部新增延迟指标 defer；`prioritizeNodes` 内新增 minRank 过滤与 `RunPreScorePlugins`；`RunFilterPluginsWithNominatedPods` 调用点移至 `findNodesThatPassFilters`（`a.findNodesThatPassFilters+57`）。

[上一篇](06-apiserver请求处理与etcd.md) · [总目录](README.md) · [下一篇](08-controller副本控制.md)
