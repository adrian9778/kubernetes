[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# Job 与 CronJob 周期调度

> **层级**：跨篇机制纵深专题——把 **Job** 的 Pod 调谐与 tracking finalizer、完成/失败计数、退避；**CronJob** 的周期调度与并发策略讲透。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `jm.syncJob+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[13-job与cronjob控制器](../sourceReader/13-job与cronjob控制器.md) 是浓缩版；本专题纵向展开 tracking finalizer 精确计数与周期调度语义。

---

## 0. 结论速览

### 0.1 Job 与 CronJob 各解决什么

- **Job**：跑「一次性」批处理到完成——按 `parallelism` 维持活跃 Pod、失败按 `backoffLimit` 退避重试、`activeDeadlineSeconds` 超时终止。
- **CronJob**：按 cron 表达式周期性**创建 Job**，受 `Suspend`/`TimeZone`/`startingDeadlineSeconds` 控制，并发策略决定重叠处理。

### 0.2 关键结论

1. **tracking finalizer 精确计数**：Job 给 Pod 注入 `JobTrackingFinalizer`，保证「一次 Pod 结束」被精确统计一次（Pod 不会提前彻底删除）。
2. **完成/失败计数**：`syncJob` 枚举 Pod，数 succeeded/failed，写回 `status`。
3. **CronJob 到点创建 Job**：`syncCronJob` 算下一个调度时间，到点用 `getJobFromTemplate2` 构造 Job 并 Create。
4. **并发策略三选一**：`Allow`（并发）/`Forbid`（有活跃则跳过）/`Replace`（删活跃再建）。
5. **CronJob 不直接管 Pod**：CronJob 只管 Job，Pod 由 Job 控制器管——两层解耦。

---

## 1. 生产视角：对外行为

### 1.1 Job

| 行为 | 控制 |
|---|---|
| 维持活跃 Pod | `parallelism`（并发上限）、`completions`（完成数目标） |
| 失败重试 | `backoffLimit`（失败超此数 Job 标 Failed） |
| 超时终止 | `activeDeadlineSeconds` |
| 精确计数 | tracking finalizer |

### 1.2 CronJob

| 行为 | 控制 |
|---|---|
| 周期创建 Job | cron 表达式 + `TimeZone` |
| 错过调度 | `startingDeadlineSeconds`（超时太久则跳过并计 missed） |
| 重叠处理 | `concurrencyPolicy`：Allow/Forbid/Replace |
| 暂停 | `Suspend` |
| 历史保留 | `successfulJobsHistoryLimit`/`failedJobsHistoryLimit` |

---

## 2. 开发视角：Job 控制器

### 2.1 骨架（job_controller.go）

`NewController+0`（:210）→ `Run+0`（:338）→ `worker+0`（:748）→ `processNextWorkItem+0`（:753）→ `syncJob+0`（:929）。孤儿 Pod 侧有独立 worker。

### 2.2 `syncJob`——Job 调谐核心（job_controller.go `jm.syncJob+0`，:929）

```text
syncJob(ctx, key)                                   jm.syncJob+0
├── 取 Job；枚举本 Job 的 Pod（按 ControllerRef）
├── 聚合上下文 syncJobCtx（job_controller.go:152）：
│     activePods / succeeded / failed / finishedCondition / indexes / uncounted ...
├── 统计 succeeded/failed（含 tracking finalizer 精确计数）
├── 判完成/失败条件（completions 达标 → Complete；失败超 backoffLimit → Failed）
├── manageJob(ctx, job, jobCtx)                       jm.manageJob+0 (:1771)
│     ↑ 按 parallelism 增删活跃 Pod
└── trackJobStatusAndRemoveFinalizers(...)            jm.trackJobStatusAndRemoveFinalizers+0 (:1328)
      ↑ 写 status + 摘已结束 Pod 的 tracking finalizer
```

### 2.3 tracking finalizer 精确计数

**问题：** Pod 结束后若被立刻 GC 删掉，控制器可能数不到这次退出（漏计）。
**解法：** Pod 创建前注入 `JobTrackingFinalizer`（`hasJobTrackingFinalizer+0`，tracking_utils.go :127）。带 finalizer 的 Pod 删除时会卡在 Terminating，控制器**必然能观测到**这次退出、数完后再摘 finalizer。
- `finalizerExpectations`（job_controller.go :110）：`uidTrackingExpectations`（tracking_utils.go :51）记录「已请求摘除但 informer 还没观测到」的 UID。
- `trackJobStatusAndRemoveFinalizers+0`（:1328）：数完退出后摘 finalizer，Pod 才真正消失。

### 2.4 `manageJob`——增删活跃 Pod（job_controller.go `jm.manageJob+0`，:1771）

```text
manageJob(ctx, job, jobCtx)                         jm.manageJob+0
├── active = len(activePods)
├── diff = min(completions 剩余, parallelism) - active
├── diff > 0 → 创建 Pod（受 parallelism 上限）
├── diff < 0 → activePodsForRemoval 选 Pod 删除
└── 返回 (新活跃数, reason, err)
```

### 2.5 失败退避（backoffStore）

`podBackoffStore`（job_controller.go :142 字段；类型 `backoff_utils.go:39`）保存各 Job 的失败退避时间点——失败后延迟再重试，避免快速连续失败打爆 apiserver。

---

## 3. 开发视角：CronJob 控制器

### 3.1 骨架（cronjob_controllerv2.go）

`NewControllerV2+0`（:89）→ `Run+0`（:158）→ `worker+0`（:188）→ `processNextWorkItem+0`（:193）→ `sync+0`（:214）→ `syncCronJob+0`（:449）。

### 3.2 `syncCronJob`——周期调度核心（cronjob_controllerv2.go `jm.syncCronJob+0`，:449）

```text
syncCronJob(ctx, cj)                                jm.syncCronJob+0
├── Suspend → 不创建（返回 nextScheduleTime）
├── 算最近调度时间：
│     mostRecentScheduleTime (utils.go mostRecentScheduleTime+0，:100)
│     nextScheduleTime / nextScheduleTimeDuration (utils.go :210/:188)
├── 错过太久（超 startingDeadlineSeconds）→ 计 missed、跳过
├── 并发策略判定：
│   ├── ForbidConcurrent 且有活跃 Job → 跳过本轮            jm.syncCronJob+147 (:596)
│   └── ReplaceConcurrent → 删活跃 Job 再建                  jm.syncCronJob+162 (:611)
├── 到点 → getJobFromTemplate2 (utils.go getJobFromTemplate2+0，:244) 构造 Job
│     └── CreateJob                                          jm.syncCronJob+183 (:632)
└── cleanupFinishedJobs（:705）按 history limit 清旧 Job
```

### 3.3 周期调度时间计算（utils.go）

| 函数 | 锚点 | 作用 |
|---|---|---|
| `mostRecentScheduleTime` | `mostRecentScheduleTime+0`（:100） | 算最近一次该跑但可能错过的时间 |
| `nextScheduleTimeDuration` | `nextScheduleTimeDuration+0`（:188） | 距下次调度还有多久 |
| `nextScheduleTime` | `nextScheduleTime+0`（:210） | 下次调度时间点 |
| `getJobFromTemplate2` | `getJobFromTemplate2+0`（:244） | 从 template 构造 Job（带 scheduledTime 标注） |

### 3.4 并发策略语义

| 策略 | 行为 | 锚点 |
|---|---|---|
| `Allow` | 到点就建，允许并发 | 默认 |
| `Forbid` | 有活跃 Job → 本轮跳过（不建） | `jm.syncCronJob+147`（:596） |
| `Replace` | 删活跃 Job → 建新 Job | `jm.syncCronJob+162`（:611） |

---

## 4. 全链路时序

### 4.1 Job 跑完一次批处理

```text
Job 创建 → 入队 → syncJob (job_controller.go:929)
├── 枚举 Pod、数 succeeded/failed（tracking finalizer 精确计数）
├── manageJob (:1771) 按 parallelism 增删 Pod
├── completions 达标 → finishedCondition=Complete
├── 失败超 backoffLimit → finishedCondition=Failed
└── trackJobStatusAndRemoveFinalizers (:1328) 写 status + 摘 finalizer
```

### 4.2 CronJob 到点创建 Job

```text
CronJob 入队 → sync (:214) → syncCronJob (:449)
├── Suspend? → 跳过
├── mostRecentScheduleTime (utils.go:100) 算该跑的时间
├── 错过太久 → 跳过 + 计 missed
├── Forbid 有活跃 → 跳过 (:596) / Replace 删活跃 (:611)
├── 到点 → getJobFromTemplate2 (utils.go:244) + CreateJob (:632)
└── cleanupFinishedJobs (:705) 清旧 Job
     └── 新 Job → Job 控制器接管（创建 Pod）
```

---

## 5. 验证与边界

### 5.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- Job：`NewController+0`（job_controller.go:210）、`jm.Run+0`（:338）、`jm.worker+0`（:748）、`jm.processNextWorkItem+0`（:753）、`jm.syncJob+0`（:929）、`jm.trackJobStatusAndRemoveFinalizers+0`（:1328）、`jm.manageJob+0`（:1771）
- `syncJobCtx`（:152）、`finalizerExpectations`（:110）、`podBackoffStore`（:142）
- `hasJobTrackingFinalizer+0`（tracking_utils.go:127）、`uidTrackingExpectations`（:51）
- CronJob：`NewControllerV2+0`（cronjob_controllerv2.go:89）、`jm.Run+0`（:158）、`jm.worker+0`（:188）、`jm.processNextWorkItem+0`（:193）、`jm.sync+0`（:214）、`jm.syncCronJob+0`（:449）、并发判定（+147/:596 Forbid、+162/:611 Replace、+183/:632 CreateJob）、`jm.cleanupFinishedJobs+0`（:705）
- 调度时间：`mostRecentScheduleTime+0`（utils.go:100）、`nextScheduleTimeDuration+0`（:188）、`nextScheduleTime+0`（:210）、`getJobFromTemplate2+0`（:244）

### 5.2 生产实测

**未实测** `[待运行验证]`。tracking finalizer 精确计数窗口、CronJob 错过调度判定（missed 计数）、并发策略实际行为均为代码推断，未在真实集群观测。

### 5.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| Indexed Job | 未深挖 | `indexed_job_utils.go` 的 completion index 分配未读 |
| Success Policy | 未深挖 | `success_policy.go` 的 success 判定（如按 index）未展开 |
| backoff 细节 | 未深挖 | `backoff_utils.go` 的退避时间点计算未逐行读 |
| CronJob 时区/夏令时 | 未深挖 | `TimeZone` 字段的 cron 解析细节未读 |
| Job Pod 删除选择 | 未深挖 | `activePodsForRemoval` 的选择优先级未展开 |

### 5.4 修改联动

- 改精确计数 → tracking finalizer 逻辑（tracking_utils.go / `trackJobStatusAndRemoveFinalizers`），同步本文 §2.3。
- 改周期调度 → `syncCronJob`/`mostRecentScheduleTime`，同步 §3.2。
- 改并发策略 → `syncCronJob` 并发判定，同步 §3.4。

**关联文档：** [13-job与cronjob控制器](../sourceReader/13-job与cronjob控制器.md)（浓缩版）、[replicaset副本控制](replicaset副本控制.md)（控制器范式）、[workqueue机制](workqueue机制.md)（退避队列）、[garbage-collector依赖图与级联删除](garbage-collector依赖图与级联删除.md)（Job→Pod owner）。

[总目录](../sourceReader/README.md)
