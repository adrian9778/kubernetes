[上一篇](11-informer与workqueue机制.md) · [总目录](README.md) · [下一篇](13-job与cronjob控制器.md)

# Deployment 控制器

> **层级**：第四层——函数级源码解析。
> **范围**：Deployment 如何管理 ReplicaSet 集合、滚动更新的先扩后缩、回滚、状态计算。
> **基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **记法**：源码锚点用「符号+偏移」，函数定义行 = `+0`；类型/非函数锚点用「文件:行号」。

## 0. 生产视角（Deployment 对外行为、可见结果）

Deployment 是用户面向 `apps/v1` 声明的工作负载抽象：期望副本数 `spec.replicas`、Pod 模板 `spec.template`、更新策略 `spec.strategy`（RollingUpdate / Recreate）、`spec.minReadySeconds`、`spec.progressDeadlineSeconds`、`spec.revisionHistoryLimit`、`spec.paused`。控制器负责把这份声明收敛到实际运行的一组 ReplicaSet（RS）。

- **结构不变式**：每个 revision 对应一个独立 RS；同一时刻存在一个「新 RS」（template 与 Deployment 完全一致）+ 若干旧 RS（历史 revision）。RS 通过 `pod-template-hash` label 区分，该 hash 同时写进 RS 名称（`deploymentName-hash`，`generateReplicaSetName`）与 selector。
- **滚动更新**（默认）：先扩新 RS（受 `maxSurge` 约束），再缩旧 RS（受 `maxUnavailable` 可用性预算约束），最后清理超出 `revisionHistoryLimit` 的旧 RS。整个序列在 `status.conditions` 里表现为 `Progressing` 条件在 `NewReplicaSet`/`ReplicaSetUpdated`/`NewRSAvailable` 原因间演进，最终以 `Available` 条件反映最小可用副本是否满足。
- **Recreate 策略**：先把全部旧 RS 缩到 0 并等待旧 Pod 终止（`oldPodsRunning`），再创建并扩新 RS。期间 Deployment 不保证对外提供服务。
- **回滚**：`kubectl rollout undo` 写入 `rollbackTo` annotation；控制器按 revision 找到目标旧 RS，把其 PodTemplate 拷回 Deployment template（`rollbackToTemplate`），随后走正常 rollout 路径。
- **暂停/恢复**：`spec.paused` 时只做伸缩与状态同步，不推进 rollout；`checkPausedConditions` 会在暂停/恢复时写入 `Progressing=Unknown` 条件，避免误报超时。
- **比例伸缩**：`kubectl scale` 触发 `isScalingEvent`，控制器按各活跃 RS 当前规模比例分配新增/减少的副本（`scale` → `GetReplicaSetProportion`），而非提前推进 rollout。
- **可见结果（`status`）**：`replicas`（实际）、`updatedReplicas`、`readyReplicas`、`availableReplicas`、`unavailableReplicas`、`terminatingReplicas`（`DeploymentReplicaSetTerminatingReplicas` 特性开启时）、`observedGeneration`、`collisionCount`（hash 碰撞计数）、`conditions`（Available / Progressing / ReplicaFailure）。全部由 `calculateStatus` 汇总得出。
- **对外副作用**：控制器经 `RSControlInterface.PatchReplicaSet` 认领/释放 RS 归属（adopt/release），经 `client.AppsV1().ReplicaSets` 直接 `Create`/`Update`/`Delete` RS，经 `client.AppsV1().Deployments` 写 `status` 与 `spec`（回滚清 `rollbackTo`）。

## 1. 模块级

| 项 | 内容 |
|---|---|
| 控制器目录 | `pkg/controller/deployment/` |
| 入口文件 | `deployment_controller.go`（控制器主体，含 `syncDeployment`）、`sync.go`、`rolling.go`、`recreate.go`、`rollback.go`、`progress.go` |
| 调度集成文件 | `deployment_scheduling_manager.go`（`WorkloadWithDeployment` 特性：Workload 认领、模板 minCount 同步） |
| 工具 | `util/deployment_util.go`（RS 查找、边界值计算、状态计数） |
| 依赖 | Deployment / ReplicaSet / Pod 三类 informer；`WorkloadWithDeployment` 特性开启时额外注册 Workload informer；`controller.RealRSControl`（RS 认领/释放）；`queue workqueue.TypedRateLimitingInterface[string]` |
| 数据流 | Deployment 变化 / RS、Pod、Workload 反查 → queue → `syncDeployment` → 选定 rollout 策略 → scale RS → 写 `status` |

## 2. 类型级

| 类型 | 文件 | 职责 |
|---|---|---|
| `DeploymentController` | `DeploymentController+0` | 持有 `rsControl`、`client`、`dLister`/`rsLister`/`podLister`、`podIndexer`、三个 `Synced`（+ 可选的 `workloadLister`/`workloadListerSynced`）、`syncHandler`、`enqueueDeployment`、`queue` |
| `RSControlInterface` | `RSControlInterface+0` | 认领/释放 RS 的抽象：`PatchReplicaSet`（当前仅此一方法） |
| `RealRSControl` | `RealRSControl+0` | `RSControlInterface` 默认实现：`PatchReplicaSet`（:453，经 `StrategicMergePatchType` 打补丁） |
| （RS 侧类型见 08 篇） | — | — |

`DeploymentController` 关键字段：

| 字段 | 锚点 | 说明 |
|---|---|---|
| `rsControl` | `DeploymentController+2` | `controller.RSControlInterface`，认领/释放 RS 的归属 |
| `queue` | `DeploymentController+38` | `workqueue.TypedRateLimitingInterface[string]`，键为 `namespace/name` |
| `syncHandler` | `DeploymentController+9` | 可注入的调谐入口，绑定 `dc.syncDeployment` |
| `enqueueDeployment` | `DeploymentController+11` | 可注入的入队函数，绑定 `dc.enqueue` |
| `workloadLister` / `workloadListerSynced` | `DeploymentController+24~+25` | `WorkloadWithDeployment` 特性开启时非 nil，否则 nil |

## 3. 控制器运行函数级

### 函数：NewDeploymentController

**源码：** `deployment_controller.go:NewDeploymentController+0`
**职责：** 构造队列（名 `"deployment"`，`DefaultTypedControllerRateLimiter[string]`）、`RealRSControl`，注册三类 informer handler，赋值 `syncHandler=dc.syncDeployment`、`enqueueDeployment=dc.enqueue`。

**执行流程：**
1. 构造 `DeploymentController` 与队列（+7 ～ +13）。
2. `dc.rsControl = controller.RealRSControl{KubeClient, Recorder}`（+15）。
3. 注册 Deployment informer：add/update/delete → `addDeployment`/`updateDeployment`/`deleteDeployment`（+20 ～ +31）。
4. 注册 RS informer：add/update/delete → `addReplicaSet`/`updateReplicaSet`/`deleteReplicaSet`（+32 ～ +43）。
5. 注册 Pod informer：仅 DeleteFunc → `deletePod`（+43 ～ +49）。
6. `syncHandler = dc.syncDeployment`、`enqueueDeployment = dc.enqueue`（+49 ～ +50）。
7. 注入 d/rs/pod Lister 与 Synced；`AddPodControllerIndexer` 建 `podIndexer`（+52 ～ +63）。
8. `WorkloadWithDeployment` 特性开启时 `addSchedulingInformers(workloadInformer)`（+64 ～ +68），否则 workload 字段保持 nil。

### 函数：Run / worker / processNextWorkItem

**源码：** `deployment_controller.go:Run+0` / `worker+0` / `processNextWorkItem+0`
**执行流程：**
- `Run`：启动事件管道（`StartStructuredLogging` / `StartRecordingToSink`）→ 组装 `syncs`（d/rs/pod Synced，`workloadListerSynced != nil` 时追加）→ `WaitForNamedCacheSyncWithContext` → 按 `workers` 数 `wg.Go(wait.UntilWithContext(dc.worker, time.Second))`。
- `worker`（:503）：循环调用 `processNextWorkItem`。
- `processNextWorkItem`（:508）：`queue.Get` → `syncHandler` → 成功后 `queue.Forget`；失败走 `handleErr`（:521）。
- `handleErr`：`err == nil` 或命名空间终止 → `Forget`；否则 `NumRequeues < maxRetries`（=15，deployment_controller.go:63）→ `AddRateLimited`；超限记日志后 `Forget` 丢弃。

### 函数：enqueue / enqueueRateLimited / enqueueAfter

**源码：** `deployment_controller.go:enqueue+0` / `enqueueRateLimited+0` / `enqueueAfter+0`
**入键：** `controller.KeyFunc`（= `cache.DeletionHandlingMetaNamespaceKeyFunc`，`pkg/controller/controller_utils.go:110`），得到 `namespace/name`。三者分别调 `queue.Add` / `AddRateLimited` / `AddAfter`。

### 函数：informer handler

**源码：** `deployment_controller.go`
- Deployment：`addDeployment`（:223）/ `updateDeployment`（:229）/ `deleteDeployment`（:236，tombstone 兜底）——直接入队。
- RS：`addReplicaSet`（:255，`DeletionTimestamp` 非空时转 `deleteReplicaSet`；有 ControllerRef 则反查，否则按孤儿匹配）/ `updateReplicaSet`（:311，ResourceVersion 相同直接返回；ControllerRef 变化时反查新旧两侧）/ `deleteReplicaSet`（:358，tombstone 兜底）。
- Pod：只注册 `deletePod`（:392），沿 Pod→RS→Deployment 反查（`getDeploymentForPod`，:453）；仅当所属 Deployment 是 Recreate 策略时入队（:416-418）。
- Workload（特性开启时）：`enqueueDeploymentForWorkload`（`dc.enqueueDeploymentForWorkload+0`），沿 Workload→Deployment 反查入队。

## 4. sync 主流程函数级

### 函数：syncDeployment

**源码：** `deployment_controller.go:syncDeployment+0`
**职责：** 单次调谐的分派器。
**执行流程：**
```text
syncDeployment (+0)
├── 1. lister.Get（NotFound 正常返回 nil）(+14)
├── 2. DeepCopy（避免改缓存）(+24)
├── 3. SelectingAll 检查：空 selector → 发 Warning 事件、补 ObservedGeneration、返回 (+26 ～ +34)
├── 4. getReplicaSetsForDeployment（ControllerRef 认领/释放）(+38)
├── 5. 删除中 → syncStatusOnly（sync.go:syncStatusOnly+0）(+44)
├── 6. workload 检查：shouldManageWorkloadForDeployment → ensureWorkloadForDeployment (+48 ～ +52)
├── 7. checkPausedConditions（sync.go:checkPausedConditions+0）(+57)
├── 8. Paused → sync（sync.go:sync+0：getAllRS→scale→cleanup→status）(+62)
├── 9. getRollbackTo（rollback.go:getRollbackTo+0）非空 → rollback（rollback.go:rollback+0）(+68)
├── 10. isScalingEvent（sync.go:isScalingEvent+0）→ sync (+72)
└── 11. 按 Strategy：
     ├─ Recreate → getPodMapForDeployment → rolloutRecreate（recreate.go:rolloutRecreate+0）(+88)
     └── RollingUpdate → rolloutRolling（rolling.go:rolloutRolling+0）(+90)
```

### 函数：(*DeploymentController).sync

**源码：** `sync.go:sync+0`
**职责：** Paused/纯伸缩场景：`getAllReplicaSetsAndSyncRevision`（+1）→ `scale`（+5）→（`Paused && getRollbackTo==nil` 时 `cleanupDeployment`，+12 ～ +15）→ `syncDeploymentStatus`（+18）。

### 函数：rollback / getRollbackTo

**源码：** `rollback.go:rollback+0` / `getRollbackTo+0`
**职责：** `rollback` 按 annotation 中的 rollback revision（`getRollbackTo`，`apps.DeprecatedRollbackTo`，:124）找到目标旧 RS；revision 为 0 时回退到 `LastRevision`；找到后用 `rollbackToTemplate`（:76）把目标 RS 的 PodTemplate 拷回 Deployment，随后清除 `rollbackTo` 并走正常 rollout 路径。

## 5. RS 管理函数级

### 函数：getAllReplicaSetsAndSyncRevision / getNewReplicaSet

**源码：** `sync.go:getAllReplicaSetsAndSyncRevision+0` / `getNewReplicaSet+0`
**职责：** `getAllReplicaSetsAndSyncRevision`（:126）枚举全部 RS，`FindOldReplicaSets` 分出旧 RS 集合，随后调 `getNewReplicaSet` 同步 revision。`getNewReplicaSet`（:148）先查后建：

1. `FindNewReplicaSet` 找到新 RS；不存在且 `createIfNotExisted=false` 返回 nil。
2. 已存在：同步注解（`SetNewReplicaSetAnnotations`）、`MinReadySeconds`、把 revision 写回 Deployment（+13 ～ +43）。
3. 不存在且 `createIfNotExisted=true`：构造 `apps.ReplicaSet`（template + `pod-template-hash` label + ControllerRef），`NewRSNewReplicas` 算初始副本数，`SetNewReplicaSetAnnotations`，`ReplicaSets().Create`（+51 ～ +116）。
4. `WorkloadWithDeployment` 特性下给新 RS 打 `WorkloadNameAnnotation`/`GroupTemplateNameAnnotation`（+114 ～ +123，`getNewReplicaSet+82~+92`）；delegated PodGroup 场景继承 Deployment 上的同名注解。
5. 已存在冲突：校验 ControllerRef UID 与 template 语义相等，相等则复用，否则 `CollisionCount++` 并重排重试（+129 ～ +165）。

### 函数：FindNewReplicaSet / FindOldReplicaSets

**源码：** `util/deployment_util.go:FindNewReplicaSet+0` / `FindOldReplicaSets+0`
**职责：** 按 `pod-template-hash` 区分新 RS（template 完全一致，`EqualIgnoreHash` 去掉 hash label 后深比较）与旧 RS 集合；`FindOldReplicaSets`（:646）内部先调 `FindNewReplicaSet`，返回两个集合：`requiredRSs`（`Spec.Replicas != 0`）与 `allRSs`（含 0 副本旧 RS）。
**相关：** `FindActiveOrLatest`（:359，无活跃 RS 时取最新一个）、`MaxRevision`（:187，取全部 RS 最大 revision）、`LastRevision`（:201，取第二大 revision）。

### 函数：NewRSNewReplicas

**源码：** `util/deployment_util.go:NewRSNewReplicas+0`
**职责：** 滚动更新中计算新 RS 本轮应达到的副本数：`maxTotalPods = replicas + maxSurge`；`currentPodCount >= maxTotalPods` 时不可再扩；否则 `scaleUpCount = min(maxTotalPods - currentPodCount, replicas - newRS.replicas)`。Recreate 策略直接返回期望副本数。

### 函数：scaleReplicaSet

**源码：** `sync.go:scaleReplicaSet+0`
**职责：** 实际 Update RS，同时写 `DesiredReplicas`/`MaxReplicas` 注解（供 RS 控制器和观测使用）。`forceUpdate=false` 且副本未变时直接返回；副本或注解变化才真正 Update。返回 `(scaled bool, updatedRS, err)`。

### 边界函数：MaxUnavailable / MaxSurge / MinAvailable

**源码：** `util/deployment_util.go:MaxUnavailable+0` / `MaxSurge+0` / `MinAvailable+0`
**职责：** `MaxUnavailable`（:449）与 `MaxSurge`（:470）经 `ResolveFenceposts` 解析 IntOrString（百分比按期望副本数取整，边界值最小保证 1）；`MinAvailable`（:462）= `replicas - MaxUnavailable`。非 RollingUpdate 或 `replicas == 0` 时返回 0。

## 6. 滚动更新函数级

### 函数：rolloutRolling

**源码：** `rolling.go:rolloutRolling+0`
**职责：** 滚动更新主循环，严格先扩后缩。
**执行流程：**
```text
rolloutRolling (+0)
├── 1. getAllReplicaSetsAndSyncRevision(ctx, d, rsList, createIfNotExisted=true) (+1)
├── 2. reconcileRollingReplicaSets（+7）：内部先扩后缩，完成后清理超限旧 RS
│    ├── reconcileNewReplicaSet（rolling.go:reconcileRollingReplicaSets+2）——扩了立即返回
│    ├── reconcileOldReplicaSets（rolling.go:reconcileRollingReplicaSets+8）——缩了立即返回
│    └── DeploymentComplete 后 cleanupDeployment（rolling.go:reconcileRollingReplicaSets+14）
└── 3. syncRolloutStatusAfter（+8，progress.go:syncRolloutStatusAfter+0）——包装 reconcile 错误与状态错误
```

### 函数：reconcileRollingReplicaSets

**源码：** `rolling.go:reconcileRollingReplicaSets+0`
**职责：** 新增的滚动协调骨架，把「先扩、后缩、清理」三步收拢进一个函数；`rolloutRolling` 不再直接调用三个子步骤，而是调本函数并在返回后统一走 `syncRolloutStatusAfter`。内部：`reconcileNewReplicaSet`（+2）→ 若有错或已扩则返回 → `reconcileOldReplicaSets`（+8）→ 若有错或已缩则返回 → `DeploymentComplete` 时 `cleanupDeployment`（+14）。

### 函数：reconcileNewReplicaSet

**源码：** `rolling.go:reconcileNewReplicaSet+0`
**执行流程：** 已达期望 → false（+1 ～ +3）；超期望 → `scaleReplicaSet` 缩回（+5 ～ +8）；不足 → `NewRSNewReplicas` 算目标后 `scaleReplicaSet`（+10 ～ +16）。

### 函数：reconcileOldReplicaSets

**源码：** `rolling.go:reconcileOldReplicaSets+0`
**职责：** 决定旧 RS 的缩容量并执行。
**缩容量公式（+41 ～ +43）：**
```text
minAvailable = replicas - maxUnavailable
newRSUnavailablePodCount = newRS.replicas - newRS.available
maxScaledDown = allPodsCount - minAvailable - newRSUnavailablePodCount
```
该公式保证新 RS 未就绪的 Pod 也占用「不可用预算」，旧 RS 不会被过早缩掉（注释 +11 ～ +40 给出配额场景推演）。`maxScaledDown <= 0` 时直接返回（+44 ～ +46）。
**内部步骤：** `cleanupUnhealthyReplicas`（+69，`cleanupUnhealthyReplicas+0`，先缩旧 RS 的不健康副本）→ `scaleDownOldReplicaSetsForRollingUpdate`（+107，`scaleDownOldReplicaSetsForRollingUpdate+0`，按 `maxScaledDown` 在旧 RS 间分配）。返回 `totalScaledDown > 0`（+64 ～ +65）。

### 函数：cleanupUnhealthyReplicas / scaleDownOldReplicaSetsForRollingUpdate

**源码：** `rolling.go:cleanupUnhealthyReplicas+0` / `scaleDownOldReplicaSetsForRollingUpdate+0`
**职责：** `cleanupUnhealthyReplicas`（:149）按创建时间序，把不健康副本（`Spec.Replicas > AvailableReplicas`）优先缩掉，直至 `maxCleanupCount` 用尽。`scaleDownOldReplicaSetsForRollingUpdate`（:187）按 `availablePodCount > minAvailable` 的剩余预算，在旧 RS 间按创建时间序逐个缩小。

#### maxScaledDown 公式与两个官方推演

**源码：** `rolling.go:reconcileOldReplicaSets+0`（:80），缩容判定 +41 ～ +43；注释 +12 ～ +40 给出两个完整推演。

**公式：**
```text
minAvailable             = *spec.Replicas - maxUnavailable
newRSUnavailablePodCount = newRS.spec.Replicas - newRS.status.AvailableReplicas
maxScaledDown            = allPodsCount - minAvailable - newRSUnavailablePodCount
```
`maxScaledDown <= 0` 时直接返回（+44 ～ +46），本轮不缩。

**公式要解决的问题：** 缩旧 RS 不能只看 `maxUnavailable`，还要把**新 RS 自己的不健康 Pod 也算进不可用预算**——否则新 RS crashloop 时会一边缩旧 RS、一边新 RS 又补不上可用 Pod，可用性被双向挤压。

**官方推演一：新 RS crashloop + 回滚（注释 +21 ～ +34）**
- 10 副本、maxUnavailable=2、maxSurge=3。
- 更新后 newRS 建 3 副本、oldRS 缩到 8、newRS 扩到 5；newRS Pod 全部 crashloop。
- allPodsCount=13、minAvailable=8、newRSUnavailablePodCount=5 → `maxScaledDown = 13-8-5 = 0`，**oldRS 一律不缩**。
- 节点故障又让一个 oldRS Pod 不可用，仍 `13-8-5=0`，oldRS 不缩。
- 用户 `kubectl rollout undo` 回滚到好的 RS → newRSUnavailablePodCount 降为 1 → `maxScaledDown = 13-8-1 = 4`，4 个 crashloop Pod 被缩掉；总 Pod 数降到 9，newRS 再扩到 10。

**官方推演二：roll over（注释 +35 ～ +40）**
- 同样 allPodsCount=13，但这次是推新模板（roll over）而非回滚。
- 新 RS 必须从 0 起步（因为 allPodsCount 已到 13）。
- 此时 newRSUnavailablePodCount=0 → 旧 RS 可缩 `13-8-0=5` 个，从而给新 RS 腾出扩到 5 的空间。

**两个内部步骤的分工：**
- `cleanupUnhealthyReplicas`（:149）：按创建时间序，优先缩掉 `Spec.Replicas > AvailableReplicas` 的不健康副本，至 `maxCleanupCount` 用尽——**先清理已经不健康的，不增加额外不可用**。
- `scaleDownOldReplicaSetsForRollingUpdate`（:187）：在 `availablePodCount > minAvailable` 的剩余预算内，按创建时间序在旧 RS 间逐个缩小——**只缩仍健康的、且缩完不会跌破 minAvailable 的**。

**设计要点（源码可证）：**
1. **不可用预算统一核算**：maxScaledDown 把新旧 RS 的不可用 Pod 一并计入，使「先扩后缩」在任何失败场景下都不会同时增加不可用。
2. **回滚/roll over 是公式自然结果**：无需特判——回滚时 newRSUnavailablePodCount 变小 → 预算变大 → 自动加速缩旧；roll over 时预算已被占满 → 新 RS 从 0 起步。
3. **先清不健康、再按预算缩健康**：两个内部步骤顺序保证「清理无价值 Pod」优先于「缩减有价值 Pod」。

## 7. Recreate 与状态函数级

### 函数：rolloutRecreate

**源码：** `recreate.go:rolloutRecreate+0`
**职责：** 先把旧 RS 全部缩到 0 并等待 Pod 删除完成（`oldPodsRunning`，:88），再把新 RS 扩到期望数；期间 Deployment 不对外提供服务。
**执行流程：**
1. `getAllReplicaSetsAndSyncRevision(ctx, d, rsList, createIfNotExisted=false)`（+2）——避免先扩后缩。
2. `scaleDownOldReplicaSetsForRecreate`（:67）缩旧；有错、已缩、或旧 Pod 仍在运行 → `syncRolloutStatusAfter` 返回（+10 ～ +14）。
3. `newRS == nil` 时 `getAllReplicaSetsAndSyncRevision(ctx, d, rsList, true)` 创建新 RS（+16 ～ +22）。
4. `scaleUpNewReplicaSetForRecreate`（:118）扩新；完成后 `cleanupDeployment`（+26 ～ +30）。
5. `syncRolloutStatusAfter`（+34）。

### 函数：calculateStatus / syncDeploymentStatus

**源码：** `sync.go:calculateStatus+0` / `syncDeploymentStatus+0`
**职责：** `calculateStatus`（:506）汇总 `Replicas`/`UpdatedReplicas`/`ReadyReplicas`/`AvailableReplicas`/`UnavailableReplicas`（`GetActualReplicaCountForReplicaSets` 系列，负数归零）；`DeploymentReplicaSetTerminatingReplicas` 特性开启时补 `TerminatingReplicas`（+20 ～ +22，`calculateStatus+20~+22`）；据 `availableReplicas >= replicas - MaxUnavailable` 判定 `Available` condition（+30 ～ +36）。`syncDeploymentStatus`（:492）deep-equal 后 `UpdateStatus`。

### 函数：syncRolloutStatusAfter / syncRolloutStatus / requeueStuckDeployment

**源码：** `progress.go:syncRolloutStatusAfter+0` / `syncRolloutStatus+0` / `requeueStuckDeployment+0`
**职责：** `syncRolloutStatusAfter`（:34）是新增包装：先调 `syncRolloutStatus`（:49），再合并 reconcile 错误与状态错误（`utilerrors.NewAggregate`）。`syncRolloutStatus` 维护滚动场景的 `Progressing` condition（无 `progressDeadlineSeconds` 时移除；完成 → `NewRSAvailable`；有进展 → `ReplicaSetUpdated`；超时 → `TimedOut`）；`getReplicaFailures` 把 RS 的 `ReplicaFailure` 条件提升到 Deployment。`requeueStuckDeployment`（:173）按 `progressDeadlineSeconds` 对停滞的 rollout 报 `TimedOut`，并安排下次检查时间（`LastUpdateTime + progressDeadline - now`，留 1s 防时钟偏移）。

## 8. 调用关系总览

```text
syncDeployment
 ├── getReplicaSetsForDeployment → RSControlInterface.PatchReplicaSet（认领/释放）
 ├── sync（paused/scale）→ scale → scaleReplicaSet → ReplicaSets().Update
 ├── rollback → rollbackToTemplate
 ├── rolloutRecreate
 │    ├── scaleDownOldReplicaSetsForRecreate → scaleReplicaSet
 │    ├── scaleUpNewReplicaSetForRecreate → scaleReplicaSet
 │    └── syncRolloutStatusAfter
 └── rolloutRolling
      ├── getAllReplicaSetsAndSyncRevision → getNewReplicaSet（Create RS）
      ├── reconcileRollingReplicaSets
      │    ├── reconcileNewReplicaSet → NewRSNewReplicas → scaleReplicaSet
      │    ├── reconcileOldReplicaSets
      │    │    ├── MaxUnavailable
      │    │    ├── cleanupUnhealthyReplicas
      │    │    └── scaleDownOldReplicaSetsForRollingUpdate → scaleReplicaSet
      │    └── cleanupDeployment
      └── syncRolloutStatusAfter → syncRolloutStatus → calculateStatus → UpdateStatus
```

## 8b. 滚动更新进度与 condition 迁移时间线（第二层补充视角）

滚动更新不是瞬时完成，而是跨多个调度周期逐步推进。本节把「一轮 reconcile 后 Deployment 的 status 与 condition 如何变化」展开成时间线——这是用户 `kubectl rollout status` / `kubectl describe deployment` 看到的可见状态机。

**每轮 reconcile 的状态计算：`calculateStatus`（`calculateStatus+0`）**
```text
availableReplicas   = Σ newRS + oldRSs 的 AvailableReplicas
totalReplicas       = Σ 所有 RS 的 Spec.Replicas
unavailableReplicas = totalReplicas - availableReplicas（负数归零，缩容时会出现）
UpdatedReplicas     = 仅 newRS 的 ActualReplicaCount
ReadyReplicas       = Σ 所有 RS 的 ReadyReplicaCount
```
- **Available condition**：`availableReplicas >= spec.Replicas - MaxUnavailable` → `Available=True`（`MinimumReplicasAvailable`）；否则 `Available=False`（`MinimumReplicasUnavailable`）。

**Progressing condition 三分支（`syncRolloutStatus+0`）：**
| 分支 | 判定（`util` 函数） | Progressing condition | reason |
|---|---|---|---|
| 完成 | `DeploymentComplete`（util:744，`Replicas==UpdatedReplicas` 且已 Available） | True | `NewReplicaSetAvailable`（:83） |
| 进行中 | `DeploymentProgressing`（util:755，新 RS 副本数有变化） | True | `ReplicaSetUpdated`（:73） |
| 超时 | `DeploymentTimedOut`（util:774，超过 `progressDeadlineSeconds`） | **False** | `ProgressDeadlineExceeded`（:86） |

**condition 迁移的精妙处（`syncRolloutStatus+27` ～ 起的注释）：**
- **仅 Progressing 是特例**：其它 condition 若新 reason 与旧 reason 相同则不更新；但 Progressing 要在「有进展」时**更新 lastUpdateTime、保留 lastTransitionTime**——即"仍在推进，别重置计时"。这正是 `progressDeadlineSeconds` 超时判定的计时基准。
- **无 progressDeadlineSeconds** → 直接移除 Progressing condition（:52 ～ :54），不做进度估算。
- **已完成（isCompleteDeployment）** → 不再估算进度（:58 ～ :62）。

**一次滚动更新的 condition 时间线：**

| 时刻 | Replicas/Updated | Progressing | Available |
|---|---|---|---|
| t0 提交新模板 | newRS 建 0 副本 | True/`ReplicaSetUpdated` | False/`MinimumReplicasUnavailable` |
| t1 先扩 | UpdatedReplicas 上升 | True/`ReplicaSetUpdated`（lastUpdateTime 刷新） | False |
| t2 新 Pod Ready | Updated==Available 上升 | True/`ReplicaSetUpdated` | True（达 minAvailable） |
| t3 全部就绪 | Replicas==UpdatedReplicas | True/`NewReplicaSetAvailable` | True |
| 超时（若卡住） | 无进展 | **False**/`ProgressDeadlineExceeded` | 视情况 |

**设计要点（源码可证）：**
1. **Progressing 的 lastUpdateTime 与 lastTransitionTime 分离**：有进展时只刷 lastUpdateTime（保留 lastTransitionTime），使 `progressDeadlineSeconds` 度量「自上次真实进展以来的时长」而非「自 condition 创建以来的时长」。
2. **超时置 False 而非删除**：`ProgressDeadlineExceeded` 把 Progressing 置 False，用户 `kubectl rollout status` 能看到明确的失败态，而非一直 Pending。
3. **`syncRolloutStatusAfter` 包装**（`syncRolloutStatusAfter+0`）：即使 reconcile 出错也照样跑进度检查，防止「reconcile 失败 → 跳过超时判定 → 卡死在 Progressing」。
4. **Available 与 Progressing 独立**：Available 看 minAvailable（是否够用），Progressing 看是否还在向新版本收敛——一个 Pod 滚动更新期间 Available 可为 True 而 Progressing 仍为 True。

## 9. 设计意图（源码可证）

- 每个 Deployment revision 对应一个独立 RS，回滚即把旧 revision 的 RS 重新扩起，无需重建 Pod template 历史（`rollbackToTemplate` 拷贝 template + `SetDeploymentRevision` 递增 revision）。
- 先扩后缩 + `maxScaledDown` 公式把「可用性预算」集中计算，新旧 RS 的规模在每轮被同时约束；新 RS 未就绪 Pod 也占用不可用预算，防止旧 RS 被过早缩掉。
- `reconcileRollingReplicaSets` 把「先扩、后缩、清理」三步收拢，`rolloutRolling` 与 `rolloutRecreate` 统一经 `syncRolloutStatusAfter` 落状态，使 reconcile 错误不再跳过进度检查。
- Pod delete 事件也反查入队：Pod 被意外删除时 Deployment 能立即唤醒，而不必等 RS 的 resync（仅 Recreate 策略）。
- `WorkloadWithDeployment` 特性下，控制器在调谐前 `ensureWorkloadForDeployment` 同步 Gang 调度所需的 Workload 及其模板 `minCount`，并把 `WorkloadNameAnnotation`/`GroupTemplateNameAnnotation` 打到新 RS 上。

## 10. 验证与基准

- **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`，工作区 `.go` 文件干净（仅 docs 有未提交文档修改）。Go 1.27.4。
- **证据索引**：`docs/sourceReader/.progress/evidence/12.md`，生成于 2026-11-18 21:10+08:00，与本文档锚点逐项一致。
- **本次修正**：① 弃用裸行号，全部改「符号+偏移」（函数定义行 = +0）；② 补写 `reconcileRollingReplicaSets`（`reconcileRollingReplicaSets+0`）与 `syncRolloutStatusAfter`（`syncRolloutStatusAfter+0`）两处重构；③ `syncDeployment` 补 `SelectingAll` 检查（+26 ～ +34）与 workload 检查（+48 ～ +52）；④ `reconcileOldReplicaSets` 的 `maxScaledDown` 公式改为相对偏移 +41 ～ +43；⑤ `RealRSControl` 位置改为 `RealRSControl+0`，接口方法更新为 `PatchReplicaSet`（旧文误记为 Create/Update）。
- **未确认项**：无。所有锚点均已按当前源码逐一核对。

[上一篇](11-informer与workqueue机制.md) · [总目录](README.md) · [下一篇](13-job与cronjob控制器.md)
