[上一篇](12-deployment控制器.md) · [总目录](README.md) · [下一篇](14-daemonset控制器.md)

# Job 与 CronJob 控制器

> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区 `.go` 干净），Go 1.27.4。
> **层级**：第四层——函数级源码解析。
> **范围**：Job 的 Pod 调谐与 tracking finalizer、完成/失败计数、退避；CronJob 的周期调度与并发策略。
> **记法**：函数定义行记 `符号+0`，`符号+N` 表示自该函数定义行起第 N 行（仅 Job/CronJob 控制器函数）。类型与常量无偏移语义，记 `符号 @ 文件:行号`。

## 0. 生产视角（先看外部行为与可见结果）

本节讲「用户不读源码也能观察到的行为」，为后面开发视角铺垫。

### Job 对外行为
- Job 是「运行一组 Pod 直到达到目标完成数」的工作单元：`Completions=nil` 时任一 Pod 成功即视为整 Job 完成（余下 Pod 只需跑完、其失败被忽略）；`Completions` 指定时须累计达到该成功数。
- 控制器保证：按 `parallelism` 维持活跃 Pod 数、失败 Pod 按 `backoffLimit` 退避重试、超过 `activeDeadlineSeconds` 终止整 Job、完成/失败计数写回 `status`。
- **可见结果**：`status.active`/`ready`/`succeeded`/`failed`，`status.conditions`（`FailureTarget`→`Failed`、`SuccessCriteriaMet`→`Complete`、`Suspended`），`status.startTime`。
- **tracking finalizer**：Pod 创建前注入 `JobTrackingFinalizer`，保证「一次 Pod 结束」被精确统计；Pod 不会被提前彻底删除，控制器能数到每一次退出。

### CronJob 对外行为
- CronJob 按 cron 表达式周期性地创建 Job，受 `Suspend`/`TimeZone`/`startingDeadlineSeconds` 控制。
- 并发策略决定重叠处理：`Allow` 允许并发、`Forbid` 已有活跃 Job 则本轮跳过、`Replace` 删除活跃 Job 再创建。
- 保留最近 N 个 Job（`successfulJobsHistoryLimit`/`failedJobsHistoryLimit`），超限由 `cleanupFinishedJobs` 清理。
- **可见结果**：`status.active`、`status.lastScheduleTime`、`status.lastSuccessfulTime`。

## 第一部分：Job

### 1. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/job/` |
| 主文件 | `job_controller.go`（`Controller`、`syncJob`、`manageJob`）、`tracking_utils.go`（finalizer/UID 追踪）、`backoff_utils.go`（`backoffStore`）、`indexed_job_utils.go`、`success_policy.go` |
| 依赖 | Job/Pod informer、expectations、两个队列（主 queue + orphanQueue）、`podBackoffStore`、`consistencyStore` |
| 数据流 | Job/Pod 事件 → queue → `syncJob`：枚举 Pod → 计数 succeeded/failed → `manageJob` 增删 Pod → `trackJobStatusAndRemoveFinalizers` 写状态/摘 finalizer |

### 2. 类型级
| 类型 | 锚点 | 职责 |
|---|---|---|
| `Controller` | `Controller @ +0` | Job 控制器主体（本分支无 Job 后缀，不存在 `JobController` 符号；已确认 grep 无匹配） |
| `syncJobCtx` | `syncJobCtx @ +0` | 一次 sync 的聚合上下文：`job`、`pods`、`finishedCondition`、`activePods`、`succeeded`/`failed`、indexes、`newBackoffRecord`、`expectedRmFinalizers`、`uncounted`、`podsWithDelayedDeletionPerIndex`、`terminating`、`ready`、`podGroup`（仅 workload 集成 Job 设置，见 `shouldManageWorkloadForJob`） |
| `uidTrackingExpectations` | `uidTrackingExpectations @ +0` | 记录「已请求摘除但 informer 尚未观测」的 tracking finalizer（UID 集合） |
| `podBackoffStore`（字段） | `podBackoffStore @ +51`（字段声明）；`backoffStore` 类型定义 `backoffStore+0` | 保存各 Job 的失败退避时间点 |

### 3. 函数级

#### 函数：NewController / Run
**锚点：** `NewController+0 @ +0`（内部 `newControllerWithClock+0 @ :219`）、`Run+0 @ :338`
**执行流程：** `NewController` 注册 Job/Pod handler；`Run` 在 `WaitForNamedCacheSync` 后同时启动 `jm.worker` 与 `jm.orphanWorker`（孤儿 Pod 单独队列）。

#### 函数：worker / processNextWorkItem
**锚点：** `worker+0 @ :748` / `processNextWorkItem+0 @ :753`
**流程：** Get key → syncHandler（`syncJob`）→ 成功 `Forget`；失败 `AddRateLimited`。孤儿侧：`orphanWorker+0 @ :772` / `processNextOrphanPod+0 @ :777`。

#### 函数：syncJob
**锚点：** `syncJob+0 @ +0`
**职责：** Job 单次调谐主体。
**执行流程：**
```text
syncJob (+0 @ :929)
├── 1. consistencyStore.EnsureReady (+18 :947)       写后未观测则退避，防读不一致
├── 2. jobLister.Get (+29 :958)；NotFound 清 expectations/backoff/consistency 后返回
├── 3. managedByExternalController (+52 :981) 跳过   被外部控制器管理的 Job 不调谐
├── 4. SatisfiedExpectations (+104 :1033)            期望未满足则本轮不计数
├── 5. getPodsForJob (+106 :1035)                    枚举/认领 Job 的 Pod
├── 6. workload/podGroup 块 (+111～+124 :1040-1053)   eligible Job 建 Workload+PodGroup，schedulingBlockedErr 记录
├── 7. FilterActivePods (+126 :1055)；countReadyPods (+131 :1060)；terminating 计数
├── 8. getNewFinishedPods (+138 :1067)               本轮新 succeeded/failed
├── 9. 汇总 succeeded/failed（含 uncounted/ignored 计数）(+139～+140 :1068-1069)
├── 10. newBackoffRecord；exceedsBackoffLimit 局部变量 (+158 :1087)
├── 11. 终态 evaluate 块 (+175～+188 :1104-1117)     BackoffLimit/ActiveDeadline/StartTime 重排队
├── 12.（Indexed）succeeded/failed indexes、SuccessPolicy (+190～+211 :1119-1140)
├── 13. finished → deleteActivePods (+215 :1144)；否则 manageJob (+231 :1160)；整块 +214～+286（含 suspended 条件分支、StartTime 重置）
└── 14. trackJobStatusAndRemoveFinalizers (+290 :1219)
```
**关键判定（全部在 `syncJob` 内联）：**
- `exceedsBackoffLimit = jobCtx.failed > *job.Spec.BackoffLimit`（`syncJob+158`，**局部变量，非独立函数**；旧文档误标为独立函数，本次已更正）。
- `pastBackoffLimitOnFailure+0 @ :1685`：按 Pod 的失败次数与退避时间判定（restart OnFailure 场景）。
- `pastActiveDeadline+0 @ :1712`：Job 活跃时长超 `activeDeadlineSeconds`。
- `newFailureCondition`（:1227）产出 `FailureTarget` 中间态条件；`trackJobStatusAndRemoveFinalizers` 内再转 `Failed`。

#### 函数：getPodsForJob
**锚点：** `getPodsForJob+0 @ :880`
**职责：** 用 `NewPodControllerRefManager`（variadic 参数含 `batch.JobTrackingFinalizer`）从 podStore 过滤、认领（adopt）属于该 Job 的 Pod；发现缺 tracking finalizer 的已属 Pod 时补 patch。

#### 函数：manageJob
**锚点：** `manageJob+0 @ :1771`
**职责：** 计算本轮活跃 Pod 目标数并增删。
**执行流程：**
1. suspended：`activePodsForRemoval` + `ExpectDeletions` + `deleteJobPods`。
2. 算 `wantActive`：
   - `Completions=nil`：成功过一次则维持现状，否则取 `parallelism`；
   - `Completions` 指定：`min(completions-succeeded, parallelism)`，下限 0。
3. 删除优先：`activePodsForRemoval` 取 `rmAtLeast` 个（受 `MaxPodCreateDeletePerSync` 限速），`deleteJobPods`（一轮只做删或增一种）。
4. `diff = wantActive - terminating - active > 0` 时准备创建：全局 backoff 未结束则 `enqueueSyncJobWithDelay`。
5. `ExpectCreations`、`appendJobCompletionFinalizerIfNotFound`（模板注入 tracking finalizer）。
6. slow-start 分批，`podControl.CreatePodsWithGenerateName` 实际创建。
**返回值：** active 数、sync action 指标标签、error。

#### 函数：trackJobStatusAndRemoveFinalizers
**锚点：** `trackJobStatusAndRemoveFinalizers+0 @ :1328`
**职责：** 写 Job status（Active/Ready/Succeeded/Failed、conditions）；Job 完成时摘除 Pod 上的 tracking finalizer。
**摘 finalizer 链路：** `canRemoveFinalizer`（:1467）→ `flushUncountedAndRemoveFinalizers`（:1496）→ `removeTrackingFinalizerFromPods`（:1573，patch 用 `$deleteFromPrimitiveList/finalizers`）；收尾 `enactJobFinished`（:1617）/`recordJobFinished`（:1641）。

#### 其余函数锚点
| 函数 | 锚点 | 职责 |
|---|---|---|
| `countReadyPods` | `countReadyPods+0 @ :2243` | 统计 `ready` 计数（写入 `syncJobCtx.ready`） |
| `getNewFinishedPods` | `getNewFinishedPods+0 @ :1751` | 从本轮 Pod 变更中筛出新 succeeded/failed |
| `deleteActivePods` | `deleteActivePods+0 @ :1241` | Job 失败/完成时删除活跃 Pod 并更新计数 |

#### 失败退避（backoff）机制深度拆解

Job 失败后的重试受两层机制约束：**指数退避**（控制「多久后再创建替换 Pod」）与 **BackoffLimit**（控制「累计失败多少次就放弃整个 Job」）。二者数据源都是 `podBackoffStore`。

**退避记录（`backoff_utils.go`）：**
- `Struct：backoffRecord 偏移：+33`：`key`（Job key）、`failuresAfterLastSuccess int32`（自上次成功以来的失败数）、`lastFailureTime *time.Time`。
- `backoffStore`（偏移：+39）用 `cache.Store` 按 Job key 存取；`newBackoffStore`（:70 附近）以 `backoffRecordKeyFunc` 为 keyFunc。
- `updateBackoffRecord`（偏移：+43）：key 不存在则 `Add`，存在则**就地更新**（复用旧对象指针，只改字段）。
- `removeBackoffRecord`：Job 完成/删除时清理退避记录。

**退避记录的累计规则（`newBackoffRecord` 偏移：:100 起）：**
- 无 succeeded、有 failed：`failuresAfterLastSuccess += len(newFailedPods)`，`lastFailureTime` 取**最晚失败**的完成时间。
- 有 succeeded 且无 failed：计数清零、`lastFailureTime=nil`（成功即重置退避）。
- succeeded 与 failed 并存：`failuresAfterLastSuccess` 清零后，**只累计发生在最后一次成功之后的失败**——从最晚 failed 往前遍历，遇到 `failedTime <= lastSuccessTime` 即 break。这是「一次成功重置退避」的精确实现。

**指数退避计算（`getRemainingTimeForFailuresCount` 偏移：+255）：**
```text
backoffDuration = DefaultJobPodFailureBackOff (10s)
for i := 1; i < failuresCount; i++ {
    backoffDuration *= 2
    if backoffDuration >= MaxJobPodFailureBackOff (10min) { break }  // 封顶
}
remaining = backoffDuration - clock.Since(lastFailureTime)
// remaining < 0 → 0（已可创建替换 Pod）
```
- 常量：`DefaultJobPodFailureBackOff = 10s`（:77）、`MaxJobPodFailureBackOff = 10min`（:79），均 `job_controller.go` 导出供测试。
- 即失败 1 次退避 10s、2 次 20s、3 次 40s……封顶 10min。
- Indexed Job 走 `getRemainingTimePerIndex`（偏移：+245）：从最后一个失败 Pod 的 `job-index-failure-count` 注解取该 index 的绝对失败数，按 index 独立退避。

**BackoffLimit 两套判定：**
| 判定 | 锚点 | 语义 |
|---|---|---|
| `exceedsBackoffLimit` | `syncJob+158`（**局部变量，非函数**） | 本轮 `failed > *BackoffLimit`：累计失败 Pod 数超限 |
| `pastBackoffLimitOnFailure` | `pastBackoffLimitOnFailure+0 @ :1685` | `RestartPolicy=OnFailure` 场景：累加 Running/Pending Pod 的 InitContainer+Container `RestartCount`，`BackoffLimit==0` 时 `>0` 即超限，否则 `>=BackoffLimit` 超限 |

**关键边界（源码可证）：**
- `pastBackoffLimitOnFailure` **只对 `RestartPolicy=OnFailure` 生效**（:1688 非 OnFailure 直接 false）；`Never` 场景靠 `failed` 计数（`exceedsBackoffLimit`）判定。
- `pastActiveDeadline`（紧随其后）：`ActiveDeadlineSeconds` 已设且未挂起时，`now - StartTime >= allowedDuration` 即超时终止；**挂起（suspended）的 Job 恒为 false**。
- 退避未结束时不创建替换 Pod，而是 `enqueueSyncJobWithDelay` 延迟重排——避免 crashloop Pod 高频重建。

**设计要点（源码可证）：**
1. **成功重置退避**：`failuresAfterLastSuccess` 只统计「自上次成功以来」的失败，一次成功即归零——避免长期运行的 Job 因早期偶发失败被累积惩罚。
2. **指数退避 + 上限封顶**：10s 起倍增封顶 10min，在「快速重试瞬态失败」与「避免打爆 API/镜像仓库」间取平衡。
3. **两套 BackoffLimit 对应两种 RestartPolicy**：Never 看 Pod 计数、OnFailure 看容器 restartCount 累加，各自贴合其重启语义。

#### Job 完成判定与落定流程深度拆解（第二层补充视角）

Job 怎么从「Pod 跑完」走到「Job 标记 Complete/Failed」？这不是一步完成的——中间有**未计数 Pod 门控**、**terminating 门控**、**两段 condition 转换**。本节把这条落定流程拆开。

**finishedCondition 的诞生（syncJob，jm.syncJob+156 ～ :1143）：**
1. `jobCtx.finishedCondition = hasSuccessCriteriaMetCondition(&job)`（:1089）：先看是否已有 SuccessCriteriaMet condition（Indexed Job 的 successPolicy 满足）。
2. 若无：把 FailureTarget condition 转成 Failed（:1097），或按 podFailurePolicy 新建 `JobFailureTarget`（:1101）。
3. **BackoffLimit / Deadline 兜底**（:1105 ～ :1111）：`BackoffLimitExceeded`（:1109）、`DeadlineExceeded`（:1111）。
4. **Indexed Job 的 failed indexes**（:1125 ～ :1129）：`MaxFailedIndexesExceeded`、`FailedIndexes`。
5. **SuccessCriteriaMet**（:1135 ～ :1137）：成功策略满足 → `JobSuccessCriteriaMet` condition。

**落定门控（`enactJobFinished`，jm.enactJobFinished+0）：** 即使 finishedCondition 已定，也不立即落定——有两个门控：
1. **未计数 Pod 门控**：`UncountedTerminatedPods` 里还有 succeeded/failed Pod 未被计数（`count > 0`）→ 延迟落定（:1635 ～ :1641）。这保证每个终止 Pod 都被精确计数一次（配合 tracking finalizer）。
2. **terminating 门控**：`*jobCtx.terminating > 0`（还有正在删除的 Pod）→ 延迟落定（:1644 ～ :1649）。

**两个门控都通过后：**
- `ensureJobConditionStatus` 把 finishedCondition 写入 `job.Status.Conditions`（:1652）。
- `finishedCond.Type == JobComplete` 时设 `job.Status.CompletionTime`（:1654 ～ :1656）。

**condition 的两段转换（syncJob :1085 注释）：**
```text
FailureTarget (ConditionTrue)  ──(所有相关 Pod 终止且计数)──▶  Failed
SuccessCriteriaMet (ConditionTrue) ──(同上)──▶  Complete
```
即先立一个「意向」condition（FailureTarget / SuccessCriteriaMet），等门控通过后再转成终态（Failed / Complete）。这是「精确计数」与「终态原子性」的关键：终态只在所有 Pod 都被统计后才立。

**完成事件（`recordJobFinished`，:1673 附近）：**
- JobComplete → `Completed` 事件（Normal）；若 `Succeeded > Completions` 发 `TooManySucceededPods` 警告。
- JobFailed → `Failed` 事件。
- 递增 `job_finished_total` 指标（按 completionMode 区分）。

**完成流程时间线：**

| 时刻 | 动作 | 可见结果 |
|---|---|---|
| 所有 Pod 终止 | `getNewFinishedPods` 筛出新 succeeded/failed | `UncountedTerminatedPods` 累积 |
| 逐个计数 | `trackJobStatusAndRemoveFinalizers` 计数 + 摘 tracking finalizer | `UncountedTerminatedPods` 清空 |
| 全部计数 + 无 terminating | `enactJobFinished` 通过门控 | Job condition 转 Complete/Failed + `CompletionTime` |
| 落定 | `recordJobFinished` 发事件 + 指标 | `kubectl get job` STATUS = Complete/Failed |

**设计要点（源码可证）：**
1. **两段 condition 而非一步终态**：先立 FailureTarget/SuccessCriteriaMet「意向」，等精确计数与 terminating 门控通过后再转终态——避免「Pod 还在删就 premature 标 Complete」。
2. **未计数门控配合 tracking finalizer**：tracking finalizer 保证 kubelet 不会在 Job 计数前彻底删掉 Pod；未计数门控保证 Job 不在计数前落定。两者配合实现「恰好计数一次」。
3. **CompletionTime 取 finishedCondition 的 LastTransitionTime**（:1655），使 Complete 时刻精确对应「最后一个 Pod 终止且被计数」的时刻。

### 4. tracking finalizer 机制
- 常量 `JobTrackingFinalizer @ staging/src/k8s.io/api/batch/v1/types.go:45`（`labelPrefix + "job-tracking"`）：Pod 创建前注入 PodTemplate；带 finalizer 的 Pod 在 apiserver 上不会被提前彻底删除，保证 Job 控制器能统计到每一次 Pod 结束。
- `tracking_utils.go`：`uidSet @ :43`、`hasJobTrackingFinalizer @ :127`、`recordFinishedPodWithTrackingFinalizer @ :131`、`isFinishedPodWithTrackingFinalizer @ :144`；finalizer 摘除的 expect/observed（`expectFinalizersRemoved @ :79` / `finalizerRemovalObserved @ :99`）防止摘除事件丢失导致 Pod 计数泄漏。

## 第二部分：CronJob

### 5. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/cronjob/` |
| 主文件 | `cronjob_controllerv2.go`（`ControllerV2`）、`utils.go`、`injection.go`（`jobControlInterface`）；schedule 解析在 `pkg/util/parsers` |
| 依赖 | CronJob informer、Job informer（按 ControllerRef UID 加索引 `jobControllerUIDIndex`）、`jobControl`/`cronJobControl` |
| 数据流 | CronJob/Job 事件 → queue → `sync`：枚举子 Job → 清理过期历史 → `syncCronJob` 判定本轮是否创建 Job → UpdateStatus |

### 6. 类型级
| 类型 | 锚点 | 职责 |
|---|---|---|
| `ControllerV2` | `ControllerV2 @ +0` | 持有 queue、`jobControl`、`cjControl`、`cronJobLister`、`jobIndexer`、`now func() time.Time` |
| `jobControlInterface` | `jobControlInterface @ +0` | `GetJob`/`CreateJob`/`DeleteJob` 的抽象（**定义在 injection.go，不在 cronjob_controllerv2.go**；旧文档未注文件，本次已更正） |

### 7. 函数级

#### 函数：NewControllerV2 / Run
**锚点：** `NewControllerV2+0 @ :89` / `Run+0 @ :158`
**注册：** Job 与 CronJob 两类 handler（加 `jobControllerUIDIndex`）。

#### 函数：worker / processNextWorkItem / sync
**锚点：** `worker+0 @ :188` / `processNextWorkItem+0 @ :193` / `sync+0 @ :214`
**sync 步骤：** lister 取 CronJob → `getCronJobJobsByIndexer`（`+0 @ :263`，枚举子 Job）→ `cleanupFinishedJobs`（`+0 @ :705`，history limit 超限走 `removeOldestJobs+0 @ :751`）→ `syncCronJob` → UpdateStatus。

#### 函数：syncCronJob
**锚点：** `syncCronJob+0 @ +0`（多行签名 :449-452）
**执行流程：**
```text
syncCronJob (+0 @ :449)
├── 1. 遍历子 Job：inActiveList（inActiveList+0）核对 Active；
│      finished → deleteFromActiveList（deleteFromActiveList+0）并更新 LastSuccessfulTime (+8～+46 :457-495)
├── 2. Active 中已不存在的 Job（GetJob NotFound，MissingJob）摘除 (+48～+70 :497-519)
├── 3. DeletionTimestamp 直接返回；校验 timeZone；spec.Suspend 直接返回 (+72～+91 :521-540)
├── 4. parsers.ParseCronScheduleWithPanicRecovery（经 formatSchedule 包装）(+93 :542)
├── 5. nextScheduleTime（nextScheduleTime+0）(+102 :551)；无 unmet 则返回下次时长
├── 6. startingDeadlineSeconds：tooLate=scheduledTime+deadline<now → MissSchedule (+120～+137 :569-586)
├── 7. inActiveListByName 防重名 (+138 :587)
├── 8. 并发策略（内联，无独立函数）:
│      ForbidConcurrent 且 Active 非空 → JobAlreadyActive (+147～+158 :596-609)
│      ReplaceConcurrent → 遍历 Active deleteJob (+160～+175 :611-624)
├── 9. getJobFromTemplate2（getJobFromTemplate2+0）构造 Job (+178 :627)
└── 10. jobControl.CreateJob（AlreadyExists 且被本 CJ 拥有则补 Active）；
         追加 Active、设 LastScheduleTime (+183～+224 :632-673)
```
**关键判定全部内联：** concurrencyPolicy 与 startingDeadlineSeconds 没有独立函数，以 `syncCronJob` 内的条件分支实现。

### 8. 调用关系
```text
CronJob sync
 ├── getCronJobJobsByIndexer
 ├── cleanupFinishedJobs → removeOldestJobs
 └── syncCronJob
      ├── ParseCronScheduleWithPanicRecovery → cron.ParseStandard
      ├── nextScheduleTime / mostRecentScheduleTime（schedule.Next）
      ├── deleteJob（Replace）
      ├── getJobFromTemplate2
      └── jobControl.CreateJob
```
**cron 解析锚点：** `ParseCronScheduleWithPanicRecovery @ pkg/util/parsers/parsers.go`（内部 `cron.ParseStandard`，robfig/cron；`jm.syncCronJob+93` 调用，经 `formatSchedule` 包装）。
**utils.go 锚点：** `inActiveList @ inActiveList+0`、`deleteFromActiveList @ :79`、`mostRecentScheduleTime @ :100`、`nextScheduleTime @ :210`、`getJobFromTemplate2 @ :244`。

## 9. 两控制器的错误处理
| 场景 | 行为 |
|---|---|
| Job Pod 失败但未超 backoffLimit | `manageJob` 按 backoff 延时后补 Pod |
| Pod 超过 backoffLimit/activeDeadline | Job 标记 Failed，`deleteActivePods` |
| Job 调度被阻塞（schedulingBlockedErr） | 不调 `manageJob`（除非 suspended），`syncJob` 末尾 `errors.Join(manageJobErr, schedulingBlockedErr)` |
| CronJob 超过 startingDeadlineSeconds | 跳过本轮（MissSchedule），等下次事件唤醒 |
| Forbid 时已有 Active Job | 不创建，仅更新 status |
| Job 已被外部控制器管理 | 跳过 sync，不做任何操作 |
| CreateJob 返回 AlreadyExists | 若被本 CronJob 拥有且不在 Active 列表则补回 Active 并更新 status |

## 10. 设计意图与验证
### 设计意图
- **Job**：幂等调谐——每轮以 informer 一致性（`consistencyStore.EnsureReady`）与 expectations 为前置，先计数后增删；`tracking finalizer` 让「每次 Pod 结束」可精确记账，避免 Pod 被提前删除导致计数泄漏；失败退避与 activeDeadline 在 `syncJob` 内联判定并转 `FailureTarget`→`Failed`。
- **CronJob**：确定性调度——以「Job 名 = 哈希(scheduledTime)」作锁，防重启后重复创建；`now func() time.Time` 注入便于测试；并发策略与 deadline 全部内联，保持单次调谐语义清晰。

### 验证
- 证据索引：`docs/sourceReader/.progress/evidence/13.md`（2026-11-18 21:17+08:00 生成）。
- 本轮核对：HEAD `8893923938107170c86eb4b8cc2b7225b4e44e9b`；Job 17 个函数锚点、CronJob 14 个符号锚点逐行回源码验证成立；`exceedsBackoffLimit` 确认为 `syncJob` 内局部变量（:1087）；`jobControlInterface` 确认在 `jobControlInterface+0`；`syncJobCtx` 确认含 `podGroup`/`ready`/`terminating`/`podsWithDelayedDeletionPerIndex` 新增字段。
- 未确认项：无（本文件全部锚点均已回源码验证，无 `[源码未确认]` 项）。

[上一篇](12-deployment控制器.md) · [总目录](README.md) · [下一篇](14-daemonset控制器.md)
