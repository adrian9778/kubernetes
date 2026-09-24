# Scheduler：Queue、Cache、Snapshot、Framework、Preemption 与 Binding

## 1. 构造与运行

核心类型 [`Scheduler`](../../../pkg/scheduler/scheduler.go#L72)，构造函数 [`New`](../../../pkg/scheduler/scheduler.go#L234)，运行入口 [`Run`](../../../pkg/scheduler/scheduler.go#L544)。

`New` 创建：in-tree plugin registry、profiles、Cache、Snapshot、SchedulingQueue、waitingPods、podsInPreBind、event handlers，以及 feature gate 开启时的 DRA manager、APIDispatcher、PodGroup/CompositePodGroup lister。

当前 `Scheduler.NextEntity` 返回 `QueuedEntityInfo`。[`ScheduleOne`](../../../pkg/scheduler/schedule_one.go#L67) 从队列 Pop 后按实体类型调用 [`scheduleOnePod`](../../../pkg/scheduler/schedule_one.go#L93) 或 [`scheduleOnePodGroup`](../../../pkg/scheduler/schedule_one_podgroup.go#L81)。这是 v1.37 相比经典“NextPod”模型的结构变化。

## 2. SchedulingQueue 状态机

接口 [`SchedulingQueue`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L98)，实现 [`PriorityQueue`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L207)。核心容器：

```text
activeQ             可立即 Pop 的实体，QueueSort 排序
backoffQ            失败后尚未到 backoff 截止时间
unschedulableEntities 当前集群事件尚不足以改变失败结论
inFlightPods        已 Pop、尚未 Done 的 Pod
inFlightEvents      in-flight 期间发生的集群事件
workload forest     PodGroup/CompositePodGroup 层级与成员
```

`PriorityQueue` 用一把主 mutex 保护跨容器原子移动，condition variable 在 activeQ 为空时阻塞 Pop。不要分别给三个队列加锁后任意嵌套，否则很容易制造锁序死锁。

[`Pop`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L1335) 从 activeQ 取最高优先级实体、增加 attempts、记录 in-flight。Pod 调度结束必须 [`Done`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L1341)，失败时 [`AddUnschedulablePodIfNotPresent`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L1073) 根据失败插件和期间事件决定进入 activeQ、backoffQ 或 unschedulable pool。

[`MoveAllToActiveOrBackoffQueue`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L1884) 不再粗暴唤醒全部 Pod。Framework plugin 通过 `EventsToRegister` 和 QueueingHintFn 声明哪些资源变化可能修复哪类失败；只有命中该 Pod 的 `UnschedulablePlugins/PendingPlugins` 才移动。定时 flush 仍是防漏兜底。

PodGroup 使用 [`AddAttemptedPodGroupIfNeeded`](../../../pkg/scheduler/backend/queue/scheduling_queue.go#L1166) 整体结束一次尝试。组内剩余 Pod、原 timestamp 和是否立即触发抢占都要一起维护，不能把组成员当互不相关的 Pod 重新入队。

## 3. Cache 与 Snapshot

实时 cache 实现 [`cacheImpl`](../../../pkg/scheduler/backend/cache/cache.go#L61)，所有 nodeInfo、podStates、assumedPods 和 PodGroup 状态受 cache mutex 保护。关键方法：[`AssumePod`](../../../pkg/scheduler/backend/cache/cache.go#L447)、[`ForgetPod`](../../../pkg/scheduler/backend/cache/cache.go#L477)、[`AddPod`](../../../pkg/scheduler/backend/cache/cache.go#L609)、[`UpdateSnapshot`](../../../pkg/scheduler/backend/cache/cache.go#L206)。

Assume 的原因：绑定 API 调用和 informer 回流有延迟。若 Pod A 已选择节点但 cache 不立即扣资源，Pod B 可能也使用同一份资源。Assume 先把 Pod 放进 nodeInfo 并记为 assumed；informer 后续 AddPod 使它“确认”，绑定失败则 ForgetPod 释放。

[`Snapshot`](../../../pkg/scheduler/backend/cache/snapshot.go#L84) 是一次调度周期的稳定读视图。UpdateSnapshot 增量复制发生变化的 NodeInfo，并更新 affinity、anti-affinity、storage、PodGroup 层级索引。Filter/Score 并行读取 snapshot，不直接读正在被 informer 修改的 cache。

v1.37 Snapshot 还支持 mutation 区间和 placement：[`StartMutations`](../../../pkg/scheduler/backend/cache/snapshot.go#L298)、[`AssumePod`](../../../pkg/scheduler/backend/cache/snapshot.go#L581)、[`AssumePlacement`](../../../pkg/scheduler/backend/cache/snapshot.go#L755)。PodGroup 尝试多个 placement 时可在快照内暂存并回滚，不污染全局 cache。

## 4. 单 Pod 调度与绑定周期

[`scheduleOnePod`](../../../pkg/scheduler/schedule_one.go#L93) 的高层路径：

```text
检查 Pod 是否已删除/已绑定/找不到 profile
  -> schedulingCycle
       UpdateSnapshot
       PreFilter -> Filter -> PostFilter(失败时)
       PreScore -> Score -> 选择节点
  -> assumeAndReserve
       Cache.AssumePod
       Reserve plugins
       Permit plugins
  -> goroutine bindingCycle
       WaitOnPermit
       PreBind
       Bind
       PostBind
       finishBinding
```

[`schedulingCycle`](../../../pkg/scheduler/schedule_one.go#L169) 串行执行一个实体的决策，因此主 scheduling loop 可快速开始下一个 Pod；[`bindingCycle`](../../../pkg/scheduler/schedule_one.go#L399) 可并发。并发安全依赖 Assume 已经扣除资源。

失败补偿遵循逆序：Reserve 之后失败必须 `Unreserve`；Assume 之后失败必须 `ForgetPod`；Permit wait 被拒绝也走同一路径。[`assume`](../../../pkg/scheduler/schedule_one.go#L1060) 只修改 Pod 副本的 `spec.nodeName` 并写 cache，不提交 API。

[`bind`](../../../pkg/scheduler/schedule_one.go#L1100) 调用 Bind plugins。默认插件 [`DefaultBinder.Bind`](../../../pkg/scheduler/framework/plugins/defaultbinder/default_binder.go#L56) 创建 `v1.Binding`；启用异步 API 时经 APICacher，未启用时直接请求 Pod binding 子资源。

## 5. Framework 扩展点

公共插件接口已经位于 staging 的 [`k8s.io/kube-scheduler/framework/interface.go`](../../../staging/src/k8s.io/kube-scheduler/framework/interface.go#L438)，仓库内 orchestration 接口在 [`pkg/scheduler/framework/interface.go`](../../../pkg/scheduler/framework/interface.go#L193)。

| 扩展点 | 目的 | 失败后补偿 |
|---|---|---|
| PreEnqueue | Pod 是否可进入 activeQ | 留在不可调度池 |
| QueueSort | activeQ 排序 | 无 |
| PreFilter | 预计算 CycleState、缩小节点集合 | 中止周期 |
| Filter | 单节点可行性 | 记录失败插件/原因 |
| PostFilter | 尝试抢占等修复 | 通常重新排队 |
| PreScore/Score | 节点评分、归一化、权重 | 中止周期 |
| Reserve | 外部资源暂存 | 逆序 Unreserve |
| Permit | Allow/Reject/Wait | Reject 后 Unreserve+Forget |
| PreBind | 绑定前 API/资源准备 | Unreserve+Forget |
| Bind | 提交 Pod->Node | 失败补偿 |
| PostBind | 成功通知，不能改变绑定结果 | 无回滚 |

每个调度周期有独立 `CycleState`。插件把 PreFilter 计算结果放入 state，后续 Filter/Score 读取；并行节点检查要求 StateData 正确实现 Clone，或内部保证只读/同步。

## 6. Preemption

抢占入口 [`Evaluator.Preempt`](../../../pkg/scheduler/framework/preemption/preemption.go#L181)。流程：

```text
确认 preemptor 有资格
  -> 从 Unschedulable 节点筛 potentialNodes
  -> DryRunPreemption 并行模拟移除低优先级 victims
  -> 重新运行 Filter，逐个加回可保留 victim
  -> 统计 PDB violations
  -> extenders 过滤（如配置）
  -> pickOneNodeForPreemption
  -> Executor 删除/拒绝 victims
  -> 返回 nominatedNodeName
```

候选排序优先最少 PDB violation，再比较最高优先级 victim、victim 优先级总和、victim 数量等。抢占只让 Pod“未来可能可调度”，不会在当前周期直接绑定到目标节点。

[`Executor`](../../../pkg/scheduler/framework/preemption/executor.go#L72) 能在内存中抢占 WaitingPod，取消处于 PreBind 的 Pod，或通过 API 给 victim 添加 `DisruptionTarget` condition 后删除。异步抢占用 `preempting` 和 `lastVictimsPendingPreemption`（mutex 保护）解决“最后一个 victim 删除事件先于 goroutine 清状态”造成 preemptor 卡死的竞态。

GenericWorkload 开启时，victim 可按 disruption mode 聚合为跨节点 PodGroup domain；PodGroup 抢占和单 Pod 抢占共用 Executor，但 preemptor UID、priority 和 Pods 集合不同。

## 7. v1.37 PodGroup 与异步 API

[`scheduleOnePodGroup`](../../../pkg/scheduler/schedule_one_podgroup.go#L81) 先 UpdateSnapshot，再把队列中的组对象替换为 snapshot 版本，验证 schedulerName、priority、preemptionPolicy 和层级形状。每个 Pod 的试探调度通过 [`assumeAndReserveWithRevert`](../../../pkg/scheduler/schedule_one_podgroup.go#L693) 返回回滚闭包；组或父 CompositePodGroup 失败时逆序执行全部 Unreserve/Forget。

placement 插件可生成多种整组布局，`PlacementFeasible` 允许在部分成员完成时返回 Wait/Unschedulable/Success；最终只提交满足组约束的 placement。它本质上是两阶段提交：快照内试排，整组成功后才进入真实 cache assume 和 binding。

[`APIDispatcher`](../../../pkg/scheduler/backend/api_dispatcher/api_dispatcher.go#L32) 在 `SchedulerAsyncAPICalls` 开启时排队执行 API call。callQueue 按相关性处理同对象调用、与 informer 对象做双向同步，`onFinish` 唤醒等待者。它改变的是 API 执行与缓存同步方式，不改变 Bind 成功才表示提交完成的语义。

