[上一篇](12-deployment控制器.md) · [总目录](README.md) · [下一篇](14-daemonset控制器.md)

# Job 与 CronJob 控制器
> **层级**：第四层——函数级源码解析。
> **范围**：Job 的 Pod 调谐与 tracking finalizer、完成/失败计数、退避；CronJob 的周期调度与并发策略。

## 第一部分：Job

### 1. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/job/` |
| 主文件 | `job_controller.go`（Controller、syncJob、manageJob）、`tracking_utils.go`（finalizer/UID 追踪）、`indexed_job_utils.go`、`success_policy.go` |
| 依赖 | Job/Pod informer、expectations、两个队列（主 queue + orphanQueue）、podBackoffStore、consistencyStore |
| 数据流 | Job/Pod 事件 → queue → syncJob：枚举 Pod → 计数 succeeded/failed → manageJob 增删 Pod → trackJobStatus 写状态/摘 finalizer |

### 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Controller` | `job_controller.go:91` | Job 控制器主体（本分支已去 Job 后缀；不存在 JobController 符号） |
| `syncJobCtx` | :157 | 一次 sync 的聚合上下文：activePods、succeeded/failed、uncounted、terminating、ready、newBackoffRecord |
| `uidTrackingExpectations` | `tracking_utils.go:51` | 记录"已请求摘除但 informer 尚未观测"的 tracking finalizer（UID 集合） |
| `podBackoffStore` | job_controller.go | 保存各 Job 的失败退避时间点 |

### 3. 函数级

#### 函数：NewController / Run
**源码：** `job_controller.go:211 / :333`
**执行流程：** NewController（内部 newControllerWithClock:220）注册 Job/Pod handler；Run 在 WaitForNamedCacheSync 后同时启动 `jm.worker` 与 `jm.orphanWorker`（孤儿 Pod 单独队列）。

#### 函数：worker / processNextWorkItem
**源码：** `job_controller.go:741 / :746`
**流程：** Get key → syncHandler（syncJob）→ 成功 Forget；失败 AddRateLimited。孤儿侧：orphanWorker:765/processNextOrphanPod:770。

#### 函数：syncJob
**源码：** `job_controller.go:922`
**职责：** Job 单次调谐主体。
**执行流程：**
```text
syncJob (+0)
├── 1. consistencyStore.EnsureReady (+18)
├── 2. jobLister.Get；NotFound 清 expectations/backoff 后返回 (+28)
├── 3. managedByExternalController 跳过 (+52)
├── 4. expectations.SatisfiedExpectations (+105)
├── 5. getPodsForJob（:873）claim/adopt Pod（+107）
├── 6. FilterActivePods（+121）；countReadyPods（:2211，+125）
├── 7. getNewFinishedPods（:1739，新 succeeded/failed）(+133)
├── 8. 汇总 succeeded/failed（含 uncounted/ignored 计数）(+134 ～ +136)
├── 9. newBackoffRecord；backoffLimit/activeDeadline 判定 (+142 ～ +177)
├── 10.（Indexed）succeeded/failed indexes、successPolicy (+186 ～ end)
├── 11. finished → deleteActivePods（:1230）；否则 manageJob (+210 ～ +224)
├── 12. completions 成功判定；suspended 条件 (+228 ～ +250)
└── 13. trackJobStatusAndRemoveFinalizers（:1317）(+285)
```
**关键判定：** `exceedsBackoffLimit = failed > *backoffLimit`（:1075）、`pastBackoffLimitOnFailure`（:1674，按 pod 的失败次数与退避时间判定）、`pastActiveDeadline`（:1701）。

#### 函数：getPodsForJob
**源码：** `job_controller.go:873`
**职责：** 用 `NewPodControllerRefManager(..., batch.JobTrackingFinalizer)` 从 podStore 过滤、认领（adopt）属于该 Job 的 Pod；发现缺 tracking finalizer 的已属 Pod 时补 patch（:911 ～ +913）。

#### 函数：manageJob
**源码：** `job_controller.go:1759`
**职责：** 计算本轮活跃 Pod 目标数并增删。
**执行流程：**
1. suspended：activePodsForRemoval + ExpectDeletions + deleteJobPods（+10 ～ +18）
2. 算 wantActive（+20 ～ +41）：
   - Completions=nil：成功过一次则维持现状，否则取 parallelism
   - Completions 指定：`min(completions-succeeded, parallelism)`，下限 0
3. 删除优先：activePodsForRemoval 取 rmAtLeast 个（受 MaxPodCreateDeletePerSync 限速），deleteJobPods（+42 ～ +60；注释 :818 ～ +820 说明一轮只做删或增一种）
4. diff = wantActive - terminating - active > 0 时准备创建（+68）：全局 backoff 未结束则 enqueueSyncJobWithDelay（+70 ～ +82）
5. ExpectCreations（:1862）、appendJobCompletionFinalizerIfNotFound（:2063，模板注入 tracking finalizer）
6. slow-start 分批（+135），`podControl.CreatePodsWithGenerateName`（:1929）实际创建
**返回值：** active 数、sync action 指标标签、error。

#### 函数：trackJobStatusAndRemoveFinalizers
**源码：** `job_controller.go:1317`
**职责：** 写 Job status（Active/Ready/Succeeded/Failed、conditions）；Job 完成时摘除 Pod 上的 tracking finalizer。
**摘 finalizer 链路：** canRemoveFinalizer（:1467）→ flushUncountedAndRemoveFinalizers（:1496）→ removeTrackingFinalizerFromPods（:1573；patch 用 `$deleteFromPrimitiveList/finalizers`，构造于 :2070）；收尾 enactJobFinished（:1617）/recordJobFinished（:1641）。

### 4. tracking finalizer 机制
- 常量 `batch.JobTrackingFinalizer`（`[staging] k8s.io/api/batch`）：Pod 创建前注入 PodTemplate；带 finalizer 的 Pod 在 apiserver 上不会被提前彻底删除，保证 Job 控制器能统计到每一次 Pod 结束。
- `tracking_utils.go`：uidSet（:43）、hasJobTrackingFinalizer:127、recordFinishedPodWithTrackingFinalizer:131、isFinishedPodWithTrackingFinalizer:144；finalizer 摘除的 expect/observed（expectFinalizersRemoved:79/finalizerRemovalObserved:99）防止摘除事件丢失导致 Pod 计数泄漏。

## 第二部分：CronJob

### 5. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/cronjob/` |
| 主文件 | `cronjob_controllerv2.go`（ControllerV2）、`utils.go`；schedule 解析在 `pkg/util/parsers` |
| 依赖 | CronJob informer、Job informer（按 ControllerRef UID 加索引 jobControllerUIDIndex）、jobControl/cronJobControl |
| 数据流 | CronJob/Job 事件 → queue → sync：枚举子 Job → 清理过期历史 → syncCronJob 判定本轮是否创建 Job → UpdateStatus |

### 6. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `ControllerV2` | `cronjob_controllerv2.go:66` | 持有 queue、jobControl、cjControl、cronJobLister、jobIndexer、`now func() time.Time` |
| `jobControlInterface` | 同包 | CreateJob/GetJob/DeleteJob 的抽象 |

### 7. 函数级

#### 函数：NewControllerV2 / Run
**源码：** `cronjob_controllerv2.go:89 / :158`
**注册：** Job 与 CronJob 两类 handler（:135 加 jobControllerUIDIndex）。

#### 函数：worker / processNextWorkItem / sync
**源码：** :188 / :193 / :214
**sync 步骤：** lister 取 CronJob → getCronJobJobsByIndexer（:263，枚举子 Job）→ cleanupFinishedJobs（:705，history limit 超限走 removeOldestJobs:751）→ syncCronJob（:449）→ UpdateStatus。

#### 函数：syncCronJob
**源码：** `cronjob_controllerv2.go:449`
**执行流程：**
```text
syncCronJob (+0)
├── 1. 遍历子 Job：inActiveList（utils.go:59）核对 Active；
│      finished → deleteFromActiveList（:79）并更新 LastSuccessfulTime (+6 ～ +45)
├── 2. Active 中已不存在的 Job（GetJob NotFound，MissingJob）摘除 (+51 ～ +68)
├── 3. 校验 timeZone；spec.Suspend 直接返回 (+79 ～ +90)
├── 4. parsers.ParseCronScheduleWithPanicRecovery（内部 cron.ParseStandard，robfig/cron）(+95)
├── 5. nextScheduleTime/mostRecentScheduleTime（utils.go:210/100；miss>100 报 manyMissed）(+101)
├── 6. startingDeadlineSeconds：tooLate=scheduledTime+deadline<now → MissSchedule (+120 ～ +128)
├── 7. inActiveListByName 防重名 (+137)
├── 8. 并发策略（内联，无独立函数）:
│      ForbidConcurrent 且 Active 非空 → JobAlreadyActive (+147 ～ +152)
│      ReplaceConcurrent → 遍历 Active deleteJob（:771）(+161 ～ +180)
├── 9. getJobFromTemplate2（utils.go:244）构造 Job (+178)
└── 10. jobControl.CreateJob（AlreadyExists 且被本 CJ 拥有则补 Active）；
        追加 Active、设 LastScheduleTime (+182 ～ +227)
```
**关键判定全部内联：** concurrencyPolicy 与 startingDeadlineSeconds 没有独立函数，以 syncCronJob 内的条件分支实现。

### 8. 调用关系
```text
CronJob sync
 ├── getCronJobJobsByIndexer
 ├── cleanupFinishedJobs → removeOldestJobs
 └── syncCronJob
      ├── ParseCronScheduleWithPanicRecovery → cron.ParseStandard
      ├── mostRecentScheduleTime（schedule.Next）
      ├── deleteJob（Replace）
      ├── getJobFromTemplate2
      └── jobControl.CreateJob
```

## 9. 两控制器的错误处理
| 场景 | 行为 |
|---|---|
| Job Pod 失败但未超 backoffLimit | manageJob 按 backoff 延时后补 Pod |
| Pod 超过 backoffLimit/activeDeadline | Job 标记 Failed，deleteActivePods |
| CronJob 超过 startingDeadlineSeconds | 跳过本轮（MissSchedule），等下次事件唤醒 |
| Forbid 时已有 Active Job | 不创建，仅更新 status |
| Job 已被外部控制器管理 | 跳过 sync，不做任何操作 |

[上一篇](12-deployment控制器.md) · [总目录](README.md) · [下一篇](14-daemonset控制器.md)
