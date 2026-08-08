# 10. Scheduler：队列、缓存、插件周期与绑定

## 10.1 输入输出

传统输入是 `spec.nodeName` 为空且 schedulerName 匹配的 Pod，输出是 Binding/Pod nodeName。当前版本还可按 feature gate处理 PodGroup 等调度实体。Scheduler 不启动容器。

关键文件：[scheduler.go](../../../pkg/scheduler/scheduler.go)、[schedule_one.go](../../../pkg/scheduler/schedule_one.go)、[schedule_one_podgroup.go](../../../pkg/scheduler/schedule_one_podgroup.go)、[backend/queue](../../../pkg/scheduler/backend/queue)、[backend/cache](../../../pkg/scheduler/backend/cache)、[framework/runtime](../../../pkg/scheduler/framework/runtime)。

## 10.2 Scheduler struct

Scheduler 聚合 Cache、SchedulingQueue、Profiles、NextPod/NextEntity、FailureHandler、API dispatcher、extenders 和调度函数。Profile 由 schedulerName 选择，包含一组 framework plugins。

## 10.3 队列状态机

- activeQ：立即可尝试。
- backoffQ：近期失败，等待退避。
- unschedulable：当前事件集合下不可行，等待相关集群事件或超时。

QueueingHint 让插件判断 Node、PVC、ResourceClaim 等事件是否可能使某实体重新可调度，减少无关唤醒。Add/Update/Delete handler 同时维护 queue 与 cache。

## 10.4 Cache 与 Snapshot

Scheduler Cache 持有 NodeInfo、Pods、assumed Pods，以及当前版本的 PodGroup 状态。Informer 增量更新 Cache；每轮调度通过 UpdateSnapshot 形成稳定读视图。NodeInfo 预计算资源、端口、镜像、affinity 等。

## 10.5 单 Pod scheduling cycle

```text
Pop
 -> PreEnqueue（入队前）
 -> PreFilter
 -> Filter nodes
 -> PostFilter / preemption（无可行节点）
 -> PreScore
 -> Score + Normalize + weight
 -> select host
 -> Assume
 -> Reserve
 -> Permit
 -> PreBind
 -> Bind
 -> PostBind
```

Scheduling cycle 决策通常串行，但 Filter/Score 可并行遍历节点；binding cycle 可与后续 scheduling cycle 并行。失败必须按阶段执行 Unreserve 和 Forget。

## 10.6 Status 语义

Success、Unschedulable、UnschedulableAndUnresolvable、Error、Wait、Skip 等状态影响 preemption、队列和重试。把系统错误返回为 Unschedulable 会让 Pod 等不到正确重试；把永久约束当 Error 会形成热循环。

CycleState 是一次周期的插件共享存储，插件 key 必须唯一，StateData clone 语义正确。它不是跨 Pod 全局缓存。

## 10.7 Filter 与 Score

Filter 包括资源、NodeAffinity、TaintToleration、NodePorts、VolumeBinding、InterPodAffinity、TopologySpread、DRA 等。Scheduler 可在达到 `percentageOfNodesToScore` 对应可行节点数后停止扫描。

Score 输出限定范围，经 Normalize 和配置权重求和。同分选择应避免长期固定偏置。插件必须高效，复杂度近似节点数乘插件数。

## 10.8 Assume 与异步 Bind

选定节点后先在 Cache Assume，使后续 Pod 立即看到资源占用，再异步调用 Bind。API 成功后 informer 观察真实绑定；失败则 Forget assumed 状态并交 FailureHandler 重入队。这是吞吐与最终一致性的核心窗口。

## 10.9 Permit 与 WaitingPod

Permit 可 Allow、Reject 或 Wait。等待实体保存在 framework waiting map，插件稍后 Allow/Reject 或 timeout。此时 Reserve 已执行，失败必须 Unreserve。

## 10.10 Preemption

DefaultPreemption 在无可行节点时模拟移除低优先级 Pod，筛选候选节点和 victims，考虑 PDB，并可能设置 nominatedNodeName。Nominated 不是绑定承诺，后续状态变化会重新评估。

## 10.11 API Dispatcher

当前 scheduler 将部分 API 调用交给 backend/api_dispatcher，使用并发限制和 relevance 信息，减少绑定/状态写阻塞主调度循环。阅读失败路径时同时检查调度结果、dispatcher 和 informer 回流。

## 10.12 插件扩展

Out-of-tree plugin 通过命令 Option/Registry 注入工厂，配置写入 KubeSchedulerConfiguration profile。进程内 Framework 插件性能高、生命周期丰富；HTTP Extender 是兼容路径，边界更独立但成本和能力受限。

## 10.13 最小实现

实现 active/backoff/unschedulable queue、Node cache/snapshot、Filter/Score/Bind、Assume/Forget 和 event-driven requeue。再加入 Reserve/Permit/Preemption、多 profile、并行化、metrics 和 leader election。

