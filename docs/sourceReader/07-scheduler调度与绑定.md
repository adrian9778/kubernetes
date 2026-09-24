[上一篇](06-apiserver请求处理与etcd.md) · [总目录](README.md) · [下一篇](08-controller副本控制.md)

# scheduler 调度与绑定
> **层级**：第四层——函数级源码解析。
> **范围**：调度器构造与运行、调度周期（PreFilter/Filter/PostFilter/Score）、assume、绑定周期（Permit/PreBind/Bind）、队列交互。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/kube-scheduler`（main）、`cmd/kube-scheduler/app`（NewSchedulerCommand、Run、Options.Config → scheduler.New） |
| 核心 | `pkg/scheduler`：Scheduler、SchedulingAlgorithm、scheduleOne、framework 装配、backend/queue |
| 框架接口 | `pkg/scheduler/framework`（Framework、CycleState、插件接口与状态码）、`framework/runtime`（frameworkImpl 执行器） |
| 依赖 | `[staging] client-go`（informer、client）、`[staging] k8s.io/kube-scheduler`（配置类型） |
| 数据流 | Pod informer → PriorityQueue → ScheduleOne → 调度周期(快照/过滤/打分) → assume → 绑定周期(写 apiserver) |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Scheduler` | `pkg/scheduler/scheduler.go:61` | 顶层调度器：持有 SchedulingQueue、algorithm、client、Profiles、Cache |
| `SchedulingAlgorithm` | `pkg/scheduler/algorithm.go` | 同步算法：SchedulePod（PreFilter/Filter/Score 的编排） |
| `SchedulingQueue`（interface） | `pkg/scheduler/backend/queue/scheduling_queue.go:98` | 待调度实体队列；实现 `PriorityQueue`（:199） |
| `Framework`（interface） | `pkg/scheduler/framework/interface.go` | 插件运行时接口：RunPreFilter/RunFilter/RunPostFilter/RunScore/RunPermit/RunPreBind/RunBind |
| `QueuedPodInfo` | `pkg/scheduler/framework/types.go` | 队列中的 Pod 包装（Pod、Attempts、InitialAttemptTimestamp） |
| `ScheduleResult` | `pkg/scheduler/schedule_one.go` | SuggestedHost、EvaluatedNodes、FeasibleNodes、nominatingInfo |
| `CycleState` | `pkg/scheduler/framework/interface.go` | 单个调度周期内插件共享的状态 KV |

## 3. 函数级

### 函数：New
**源码：** `pkg/scheduler/scheduler.go:278`
**职责：** 装配 Scheduler：构造 Pod/PodGroup informer 的事件处理、队列、framework profiles、Cache、algorithm。
**返回值：** `(*Scheduler, error)`。
**关键动作：** 事件处理器（`eventhandlers.go`）把 Pod 的 add/update/delete 映射为队列操作（AddPod/UpdatePod/DeletePod）。

### 函数：(*Scheduler).Run
**源码：** `pkg/scheduler/scheduler.go:456`
**执行流程：**
1. `SchedulingQueue.Run`（+2）：启动 heap 搬运与调度周期计数
2. `APIDispatcher.Run`（+4 ～ +6，可选）
3. `go wait.UntilWithContext(ctx, sched.ScheduleOne, 0)`（+14）：独立 goroutine 跑 worker（注释说明同 goroutine 会在 shutdown 时与队列关闭形成死锁，+6 ～ +13）
4. 阻塞在 `<-ctx.Done()`（+16）
5. 关闭顺序：dispatcher → queue.Close → Profiles.Close（+17 ～ +24）
**状态变化：** 从装配态进入持续 Pop/调度循环。

### 函数：(*Scheduler).ScheduleOne
**源码：** `pkg/scheduler/schedule_one.go:66`
**职责：** 单个 worker 的循环体：取实体并分派。
**执行流程：**
1. `sched.NextEntity(logger)`：阻塞 Pop（+2）
2. nil/错误检查（+3 ～ +10）
3. type switch（+12 ～ +22）：`QueuedPodInfo` → `scheduleOnePod`；`QueuedPodGroupInfo` → PodGroup 路径（本文档不展开）
**错误处理：** Pop 错误经 HandleError 记录后继续下一轮，不退出 worker。

### 函数：(*Scheduler).scheduleOnePod
**源码：** `pkg/scheduler/schedule_one.go:91`
**职责：** 单个 Pod 的端到端调度编排。
**执行流程：**
```text
scheduleOnePod (+0)
├── 1. frameworkForPod 选 profile (+10 ～ +19)
├── 2. skipPodSchedule（已带 nodeName 等场景）(+20 ～ +24)
├── 3. NewCycleState + 采样标记 + PodsToActivate (+29 ～ +36)
├── 4. schedulingCycle（同步，+42）
│      失败 → FailureHandler 后返回 (+43 ～ +46)
└── 5. go runBindingCycle（异步，+73）
```
**关键并发设计：** 绑定周期放独立 goroutine；正确性依赖第 4 步中的 assume——调度缓存已先认为 Pod 占用节点，后续 Pod 调度不会超额分配同一资源（+38 ～ +39 注释）。

### 函数：(*Scheduler).schedulingCycle
**源码：** `pkg/scheduler/schedule_one.go:168`
**执行流程：** `Cache.UpdateSnapshot`（+8）刷新节点快照 → `schedulingAlgorithm`（+11）→ `prepareForBindingCycle`（+14，assume + Permit）。

### 函数：(*Scheduler).schedulingAlgorithm
**源码：** `pkg/scheduler/schedule_one.go:249`
**职责：** 执行 SchedulePod 并处理失败：无节点/FitError → PostFilter（抢占）。
**关键分支：**
- ErrNoNodesAvailable → UnschedulableAndUnresolvable（+5 ～ +8）
- FitError 且有 PostFilter 插件 → `RunPostFilterPlugins` 尝试抢占，返回 nominatingInfo（+31 ～ +49）；无 PostFilter 直接 Unschedulable（+29 ～ +31）
- PodGroup 周期跳过 PostFilter（+18 ～ +23）
**返回值：** ScheduleResult 带 nominatingInfo；状态非成功时上层走 FailureHandler（回 Active/Backoff 队列）。

### 函数：(*SchedulingAlgorithm).SchedulePod
**源码：** `pkg/scheduler/algorithm.go:144`
**职责：** 同步算法主体。
**执行流程：**
1. 快照无节点 → ErrNoNodesAvailable（+5 ～ +6）
2. `findNodesThatFitPod`：PreFilter + Filter（+9）
3. 无可行节点 → FitError（带 diagnosis，+15 ～ +21）
4. 仅 1 个可行节点：直接返回，跳过打分（+24 ～ +34）
5. 多节点：`prioritizeNodes`（+36）→ `NewSortedScoredNodes(...).Pop()` 取最高分（+42 ～ +43）
6. （可选）opportunistic batching 存储结果（+27、+46）
**返回值：** ScheduleResult{SuggestedHost, EvaluatedNodes, FeasibleNodes}。

### 函数：(*SchedulingAlgorithm).findNodesThatFitPod / prefilterNodes
**源码：** `pkg/scheduler/algorithm.go:327 / :228`
**职责：** PreFilter 缩小候选集（可能返回节点子集），Filter 逐节点执行 `RunFilterPluginsWithNominatedPods`（:457）。
**关键数据结构：** `preFilterOutcome`（result 候选子集、allNodes 全量快照、diagnosis），保留全量列表用于维护 nextStartNodeIndex 防止节点饥饿（:210 ～ +216 注释）。
**并行：** Filter 在 framework runtime 内可并行评估节点；结果按节点聚合进 diagnosis。

### 函数：prioritizeNodes
**源码：** `pkg/scheduler/schedule_one.go:565`
**职责：** `RunScorePlugins`（:594）对可行节点打分，归一化并汇总 extender 分数（:655 ～ +668）。
**返回值：** `[]NodeScore`。

### 函数：(*Scheduler).prepareForBindingCycle
**源码：** `pkg/scheduler/schedule_one.go:193`
**执行流程：** `algorithm.AssumeAndReserveInCache`（+12，改 nodeName 写入调度缓存）→ `RunPermitPlugins`（+22）：Wait → AddWaitingPod；Reject → Unreserve 回滚并返回 FitError（+24 ～ +42）。

### 函数：(*Scheduler).runBindingCycle / bindingCycle
**源码：** `pkg/scheduler/schedule_one.go:144 / :314`
**职责：** 绑定周期全流程。
**bindingCycle 执行流程：**
```text
bindingCycle (+0)
├── 1.（可选）RunPreBindPreFlights；必要时先写 NominatedNodeName (+12 ～ +31)
├── 2. WaitOnPermit (+33 ～ +48)
├── 3. SchedulingQueue.Done(UID)：尽早释放队列中事件 (+56)
├── 4. AddPodInPreBind（抢占优化）(+63 ～ +69)
├── 5. RunPreBindPlugins (+72 ～ +74)
├── 6. 校验未在 PreBind 期间被抢占 (+77 ～ +81)
├── 7. bind (+84)
└── 8. 成功指标/日志 (+89 ～ +end)
```
**状态语义（注释 :363 ～ +368）：** Permit 之后的失败归入"可重试"（BackoffQ），不再标记为 Unschedulable。

### 函数：(*Scheduler).bind
**源码：** `pkg/scheduler/schedule_one.go:681`
**职责：** 执行实际绑定。
**优先级：** 先 `extendersBinding`（:687）；未绑定则 `RunBindPlugins`（:690）。
**默认 bind 插件：** 向 apiserver POST Pod 的 binding 子资源（`/api/v1/namespaces/<ns>/pods/<name>/binding`），apiserver 据此持久化 `spec.nodeName`。
**defer：** `finishBinding` 处理绑定收尾（成功/失败的缓存一致性）。

## 4. 状态与缓存一致性
| 时刻 | 调度缓存 | apiserver/etcd |
|---|---|---|
| 调度周期完成 | 无变化 | Pod nodeName 空 |
| Assume | Pod 已占节点（SuggestedHost） | Pod nodeName 空 |
| Bind 成功 | 与 etcd 一致 | spec.nodeName 已更新 |
| Bind 失败 | Unreserve/Forget 撤销 assume | 无变化 |

## 5. 错误处理汇总
| 失败点 | 处理 |
|---|---|
| 无节点 / Filter 全失败 | PostFilter 抢占 → 回队列（Unschedulable，带 nominating） |
| Permit Reject | Unreserve 撤销 assume，FitError |
| PreBind/Bind 失败 | 撤销 assume，Pod 进 BackoffQ 指数退避重试 |
| 写 NominatedNodeName 失败 | 记录日志，不中断绑定（非关键路径，:340 ～ +343） |
| PreBind 期间被抢占 | context.Cause 上抛，绑定中止 |

[上一篇](06-apiserver请求处理与etcd.md) · [总目录](README.md) · [下一篇](08-controller副本控制.md)
