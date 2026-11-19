[上一篇](07-scheduler调度与绑定.md) · [总目录](README.md) · [下一篇](09-kubelet同步循环.md)

# controller 副本控制
> **层级**：第四层——函数级源码解析。
> **范围**：以 ReplicaSet 控制器为样本，完整解析 informer 事件入队、worker 调谐、期望计数（expectations）、慢启动批量创建、并发删除与状态更新；末尾给出 Deployment→ReplicaSet 层级定位。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **锚点记法**：`文件 符号+偏移`，函数定义行（含 `func` 的那一行）= +0，逐行递增；裸行号不再使用。

---

## 1. 生产视角：副本控制对外行为与可见结果

ReplicaSet 控制器（`kubectl` 侧对应的资源是 `apps/v1.ReplicaSet`）的**对外契约**只有三类写操作，全部落在 apiserver 上：

| 对外行为 | 落点 | 触发时机 | 用户可见结果 |
|---|---|---|---|
| 创建 Pod | `RealPodControl.CreatePods` → apiserver POST `/api/v1/namespaces/<ns>/pods` | `ReplicaSet.spec.replicas` 大于当前活跃 Pod 数 | 新 Pod 出现，状态从 Pending → Running（由 kubelet 驱动） |
| 删除 Pod | `RealPodControl.DeletePod` → apiserver DELETE `/api/v1/namespaces/<ns>/pods/<name>` | `ReplicaSet.spec.replicas` 小于当前活跃 Pod 数 | 多余 Pod 被删除（带优雅删除） |
| 更新状态 | `updateReplicaSetStatus` → apiserver PUT `/api/v1/namespaces/<ns>/replicasets/<name>/status` | 每次调谐末段 | `ReplicaSet.status` 的 replicas/ready/available/terminating 等计数与 conditions 刷新 |

**数据流（生产可见的调谐循环）**：

```
RS/Pod informer 事件（add/update/delete）
   └─► key 去重入队（workqueue）
        └─► worker 调谐（syncReplicaSet）
             ├─ 比对活跃 Pod 数与 spec.replicas → create / delete
             ├─ 计算 status（Replicas/Ready/Available/Terminating/Conditions）
             └─ 写回 ReplicaSet.status
```

**可观察结论**：
- ReplicaSet 的副本数最终收敛到 `spec.replicas`；收敛速度受 `burstReplicas` 限速与慢启动批大小约束。
- `status.replicas` 反映**当前活跃（非终止）Pod 数**；`status.readyReplicas`/`availableReplicas` 反映就绪/可用数（受 `minReadySeconds` 影响）。
- 状态**每轮无条件更新**（:884 注释「Always updates status as pods come up or die」），即使本次没有副本增减也写 status，保证外部能观察到终态。

---

## 2. 开发视角

### 2.1 模块级

| 项 | 内容 |
|---|---|
| 控制器目录 | `pkg/controller/replicaset`（本样本），其余控制器同模式位于 `pkg/controller/<name>` |
| 组件入口 | `cmd/kube-controller-manager/app`（控制器注册在 core.go/apps.go/batch.go） |
| 依赖 | `[staging] client-go`（informer/lister、workqueue）、**`pkg/controller` 工具包**（expectations、RealPodControl）、`pkg/controller/replicaset` 内部工具 |
| 对外行为 | 对 apiserver：创建/删除 Pod、更新 ReplicaSet status |
| 数据流 | RS/Pod informer 事件 → key 去重入队 → worker 调谐 → 比对 Pod 现状与 Replicas → create/delete → 更新 status |

### 2.2 类型级

| 类型 | 文件 符号+偏移 | 职责 |
|---|---|---|
| `ReplicaSetController` | `pkg/controller/replicaset/replica_set.go ReplicaSetController+0`（:101） | 持有 RS/Pod lister、indexer、`queue workqueue.TypedRateLimitingInterface[string]`、`syncHandler`、`expectations`、`podControl`、`burstReplicas`；另含 workload/podGroup lister 与 `consistencyStore` |
| `ControlleeExpectations`（expectations） | `pkg/controller/controller_utils.go ControlleeExpectations+0`（:271） | 记录「已下发但 informer 尚未观测到」的 create/delete 数量（`add`/`del` 原子计数），抑制过早调谐 |
| `ControllerExpectations` | `pkg/controller/controller_utils.go ControllerExpectations+0`（:169） | TTLStore 缓存各 controller 的期望；`SatisfiedExpectations` 在满足或过期时返回 true |
| `UIDTrackingControllerExpectations` | `pkg/controller/controller_utils.go UIDTrackingControllerExpectations+0`（:350） | 为删除侧额外记录 UID 集，优雅删除（DeletionTimestamp）不被重复计数 |
| `RealPodControl` | `pkg/controller/controller_utils.go RealPodControl+0`（:492） | `CreatePods`/`DeletePod` 的实际 apiserver 调用实现；`OnWrite` 回调 :495（:495，用于 consistency 记录） |

> **归属修正**：`ControllerExpectations`/`ControlleeExpectations`/`UIDTrackingControllerExpectations` 与 `RealPodControl` 的类型定义均在 **`pkg/controller/controller_utils.go`**（包 `k8s.io/kubernetes/pkg/controller`），**不在 replicaset 目录，也不在 client-go**（staging/src/k8s.io/client-go 下 grep 无此三类型）。replicaset 目录只是**装配使用**：`replica_set.go NewReplicaSetController+37`（:205）在 `NewBaseController` 调用里构造 `controller.RealPodControl{KubeClient, Recorder, OnWrite}` 传入。

### 2.3 函数级

#### 函数：NewReplicaSetController
**源码：** `pkg/controller/replicaset/replica_set.go NewReplicaSetController+0`（:168）
**职责：** 构造控制器并绑定 informer 事件：RS informer 的 Add/Update/Delete（`addRS`/`updateRS`/`deleteRS`）与 Pod informer 的 `addPod`/`updatePod`/`deletePod`；事件处理最终汇入 `enqueueRS`/`enqueueRSAfter`。
**关键赋值：** `rsc.syncHandler = rsc.syncReplicaSet`（`replica_set.go NewBaseController+70`，:296，在 `NewBaseController` 内完成）。
**参数：** `ctx`、`rsInformer`、`podInformer`、`workloadInformer`、`podGroupInformer`、`kubeClient`、`burstReplicas`。
**注意：** 当 feature gate `WorkloadWithDeployment` 关闭时直接返回（`replica_set.go NewReplicaSetController+48`，:216）；开启时才 `addSchedulingInformers` 接入 workload/podGroup informer（`replica_set_scheduling_manager.go addSchedulingInformers+0`，:42）。

#### 函数：(*ReplicaSetController).Run
**源码：** `pkg/controller/replicaset/replica_set.go Run+0`（:302）
**执行流程：**
1. 启动事件广播与 sink：`eventBroadcaster.StartStructuredLogging(3)`（+4，:306）、`StartRecordingToSink`（+5，:307），并 `defer Shutdown`（+6，:308）
2. `WaitForNamedCacheSyncWithContext`：等待 RS/Pod（含 scheduling caches）informer 首次 list 完成（+21，:323）
3. 启动 N 个 worker：`for i := 0; i < workers; i++`（+25，:327）内 `wait.UntilWithContext(ctx, rsc.worker, time.Second)`（+27，:329）
4. 阻塞至 `ctx.Done()`（+30，:332）；退出时经 defer 执行 `queue.ShutDown()`（+15，:317）并 `wg.Wait()`（+16，:318）等 worker 收尾
**前置条件：** 缓存必须先同步；未同步（`WaitForNamedCacheSyncWithContext` 返回 false）直接 return（+23，:324），不启动 worker。
**关键修正：** Run 内**仅一处** `queue.ShutDown`（:317）；旧文「+27 第二处 ShutDown」在当前代码基准已不存在。

#### 函数：(*ReplicaSetController).enqueueRS / enqueueRSAfter
**源码：** `pkg/controller/replicaset/replica_set.go enqueueRS+0`（:396）/ `enqueueRSAfter+0`（:406）
**职责：** 计算对象 key（`namespace/name`）并入队；延迟版用 `queue.AddAfter`。
**流程：** `controller.KeyFunc(rs)`（+1，:397/:407）→ 失败 `utilruntime.HandleError`（+3，:399/:409）并 return → `queue.Add(key)`（+7，:403）或 `queue.AddAfter(key, duration)`（+7，:413）。
**错误处理：** key 计算失败仅记录，不 panic。

#### 函数：worker / processNextWorkItem
**源码：** `pkg/controller/replicaset/replica_set.go worker+0`（:651）/ `processNextWorkItem+0`（:656）
**执行流程：**
1. `queue.Get()` 阻塞取 key（+1，:657）；返回 `quit` 为 true 时 return false（+2~+3，:658~:659）
2. `defer queue.Done(key)`（+5，:661）
3. `syncHandler(ctx, key)` 即 syncReplicaSet（+7，:663）
4. 成功：`queue.Forget(key)` 清退避记录（+9，:665）
5. 失败：`utilruntime.HandleError` 记录日志（+13，:669）+ `queue.AddRateLimited(key)` 指数退避重入（+14，:670）
**返回值：** `bool`（queue 关闭时 false，结束 worker）。

#### 函数：(*ReplicaSetController).syncReplicaSet
**源码：** `pkg/controller/replicaset/replica_set.go syncReplicaSet+0`（:808）
**职责：** 单个 RS 的完整调谐。
**参数：** `ctx`、`key string`（namespace/name）。
**返回值：** `error`（非零导致 rate-limited 重入）。
**执行流程：**
```text
syncReplicaSet (+0，:808)
├── 1. SplitMetaNamespaceKey (+7，:815)
├── 2. consistencyStore.EnsureReady（一致性检查）(+12，:820)
├── 3. rsLister.ReplicaSets(ns).Get；NotFound → 清理 expectations 后正常返回 (+23~+28，:831~:836)
├── 4. ensurePodGroupForReplicaSet（workload 调度启用时取 PodGroup）(+34，:842)
├── 5. expectations.SatisfiedExpectations → rsNeedsSync (+36，:844)
├── 6. LabelSelectorAsSelector (+37~+41，:845~:849)
├── 7. FilterPodsByOwner（含孤儿 Pod）(+44~+47，:852~:855)
├── 8. FilterActivePods → claimPods 认领导主 (+51~+55，:859~:863)
├── 9.（feature gate）统计 terminatingPods（FilterTerminatingPods/FilterClaimedPods）(+57~+60，:865~:868)
├── 10. rsNeedsSync 且未删除 → manageReplicas (+67，:875；podGroupErr 非空但 Pod 过多时仍执行)
├── 11. rs = rs.DeepCopy() (+70，:878)
├── 12. calculateStatus（Replicas/Ready/Available/Terminating/Conditions）(+73，:881)
├── 13. updateReplicaSetStatus 写 status (+76，:884)
├── 14. consistencyStore.WroteAt (+82~+87，:890~:895)
├── 15. manageReplicasErr / podGroupErr → errors.Join 返回 (+88~+90，:896~:898)
└── 16. 规划 MinReady 下次检查（AddAfter）(+93~+105，:901~:911)
```
**关键设计：** NotFound 是正常事件（已删除），返回 nil 不重试（+28，:836）；status 每轮无条件更新（+76，:884，注释「Always updates status as pods come up or die」）；`MinReadySeconds > 0` 且 ready≠available 时安排 `FindMinNextPodAvailabilityCheck` 下次唤醒（+101~+107，:906~:911），作为 availability 检查的最后防线。

#### 函数：(*ReplicaSetController).manageReplicas
**源码：** `pkg/controller/replicaset/replica_set.go manageReplicas+0`（:678）
**职责：** 比对活跃 Pod 数与期望 Replicas，执行增删；明确不修改入参 activePods（:676~:677 注释「Does NOT modify <activePods>」）。
**参数：** `ctx`、`activePods []*v1.Pod`、`rs *apps.ReplicaSet`、`podGroup *schedulingv1beta1.PodGroup`（**第 4 参，签名多行化** :678~:683）。
**返回值：** `error`。
**执行流程：**
```text
manageReplicas (+0，:678)
├── diff = len(activePods) - int(*rs.Spec.Replicas) (+6，:684)
├── diff < 0（Pod 不足）:
│   ├── 取绝对值并受 burstReplicas 限速 (+15~+16，:693~:694)
│   ├── expectations.ExpectCreations(rsKey, diff) (+23，:701)
│   ├── 【新增】避免混合 schedulingGroup：若 podGroup 非空且 activePods 存在 SchedulingGroup==nil 的 Pod → 事件 MixedSchedulingGroup + podGroup=nil (+33~+42，:711~:720)
│   ├── 【新增】podTemplate 深拷贝：podGroup 非空时 DeepCopy template 并注入 SchedulingGroup.PodGroupName (+45~+51，:723~:729)
│   ├── slowStartBatch 慢启动创建 (+52，:730)
│   │    初始批量 SlowStartInitialBatchSize，逐轮翻倍
│   │    podControl.CreatePods（带 ControllerRef）(+53，:731)
│   │    namespace terminating → 视为成功 nil (+55~+59，:733~:739)
│   └── 跳过的 Pod 逐个 CreationObserved 回补 expectations (+67~+72，:745~:751)
└── diff > 0（Pod 过多）:
    ├── burstReplicas 限速 (+76~+77，:754~:755)
    ├── getIndirectlyRelatedPods (+81，:759)
    ├── getPodsToDelete 选 Pod（优先未启动完成的）(+85，:763)
    ├── expectations.ExpectDeletions（快照 Pod key）(+93，:771)
    ├── errCh := make(chan error, diff) (+95，:773)；wg.Add(diff) (+97，:775)
    ├── 并发 DeletePod（goroutine + WaitGroup）(+99~+112，:777~:790)
    │    失败且非 NotFound：DeletionObserved + errCh <- err (+101~+108，:779~:787)
    └── select 取 errCh 第一个错误 (+114~+122，:792~:800)
```
**慢启动意图（源码注释 +25~+32，:703~:710）：** 大批量 Pod 若因同一原因（如配额不足）失败，慢启动在早期失败后停止放大，避免 Pod create 与事件洪泛。
**并发：** 删除并发执行；创建经 slowStartBatch 内部串行分批；`errCh` 带缓冲，wg.Wait 后只取首个错误。
**副作用：** expectations 状态、apiserver 写操作。
**新增机制（scheduling 相关）：** ①「避免混合 schedulingGroup」检查（:711~:720）——当 feature gate 重新启用后出现既有 Pod 无 schedulingGroup、而 PodGroup 已建立时，若强行带 PodGroup 创建，gang 调度的 PodGroup 永远达不到 minCount；因此置 `podGroup=nil` 并记 MixedSchedulingGroup 警告事件。② podTemplate 深拷贝（:723~:729）——给新创建 Pod 注入 `SchedulingGroup.PodGroupName` 时**不改写存储中的 `rs.Spec.Template`**（DeepCopy 后写入局部 `podTemplate`）。

#### 慢启动批量创建与 createPods 报文解剖（第二层补充视角）

ManageReplicas 决定「要建 N 个 Pod」后，实际创建不是一次性发 N 个请求，而是经 **slowStartBatch 慢启动**分批并发创建。本节解剖慢启动算法与单个 Pod 的创建报文。

**慢启动算法（`slowStartBatch`，replica_set.go:944）：**
```text
slowStartBatch(count, initialBatchSize, fn)
  remaining = count
  for batchSize := min(remaining, initialBatchSize); batchSize > 0; batchSize = min(2*batchSize, remaining):
      errCh = make(chan error, batchSize); wg.Add(batchSize)
      for i := 0; i < batchSize; i++ { go fn() }   ← 本批并发创建
      wg.Wait()
      curSuccesses = batchSize - len(errCh); successes += curSuccesses
      if len(errCh) > 0 { return successes, <-errCh }   ← 本批有失败即停止放大
      remaining -= batchSize
```
- **初始批量** `SlowStartInitialBatchSize`（controller_utils.go:87，=1）：第一批只建 1 个。
- **倍增**：每批成功则下一批翻倍（1→2→4→8…），直至 `count` 用尽。
- **早期失败即停**：任一批有失败（`len(errCh) > 0`）立即返回，不继续放大——避免「配额不足时一次性打 N 个请求全失败」。

**单个 Pod 创建报文（`RealPodControl.createPods`，controller_utils.go）：**
```text
createPods(ctx, namespace, pod, object, controllerRef)
├── len(labels.Set(pod.Labels)) == 0 → 报错（无 label 不建）
├── KubeClient.CoreV1().Pods(namespace).Create(ctx, pod)   ← POST Pod
│     失败且非 NamespaceTerminatingCause → FailedCreatePodReason 事件
├── OnWrite(newPod, controllerRef)（若设，供 consistencyStore 记账）
└── SuccessfulCreatePodReason 事件 "Created pod: <name>"
```
**关键：** 创建走 `Pods(namespace).Create`（POST Pod），失败时**区分 namespace 是否 terminating**——terminating namespace 不发事件（避免噪声）。

**慢启动 + 创建报文对照（以 count=5、initialBatchSize=1 为例）：**

| 批 | batchSize | 并发创建 | 累计成功 | 失败则 |
|---|---|---|---|---|
| 1 | 1 | 1 个 Pod | 1 | 停止放大 |
| 2 | 2 | 2 个 Pod | 3 | 停止放大 |
| 3 | 4→缩到 2（remaining=2） | 2 个 Pod | 5 | 停止放大 |

**设计要点（源码可证）：**
1. **慢启动防雪崩**：`SlowStartInitialBatchSize=1` 起步、逐批翻倍，使「配额不足」这类系统性失败在第一批就暴露，不会一次性打 N 个请求全失败（slowStartBatch 注释 :703 ～ :710）。
2. **创建走 POST Pod，不预分配 UID**：`CreatePodsWithGenerateName` 设 `pod.ObjectMeta.GenerateName`（:554 起），让 apiserver 生成名字——避免客户端预分配 name 冲突。
3. **失败即停 + 回补 expectations**：任一批失败即停止放大，已成功的通过 `CreationObserved` 回补 expectations（:745 ～ :751），使期望尽快归零、不靠 5min 超时兜底。
4. **混合 schedulingGroup 防护**：PodGroup 已建立但既有 Pod 无 schedulingGroup 时置 `podGroup=nil`（:711 ～ :720），避免 gang 调度 PodGroup 永远达不到 minCount。

#### 函数：calculateStatus / updateReplicaSetStatus
**源码：** `pkg/controller/replicaset/replica_set_utils.go calculateStatus+0`（:96）/ `updateReplicaSetStatus+0`（:41）
**职责：** `calculateStatus` 依据 active/terminating Pod 与 manageReplicas 错误计算 Conditions 与各计数字段（Replicas/FullyLabeled/Ready/Available/Terminating）；`updateReplicaSetStatus` 以 client 做 status update，冲突即返回错误触发重试。
**calculateStatus 流程：** 统计 fullyLabeled/ready/available（+2~+16，:98~:117）→ feature gate 下统计 terminatingReplicas（+23~+26，:119~:122）→ 依 `manageReplicasErr` 设置/移除 `ReplicaSetReplicaFailure` condition（+28~+40，:124~:136）→ 写计数字段（+42~+46，:138~:142）。
**updateReplicaSetStatus 流程：** 稳态短路（各计数与 ObservedGeneration/Conditions 未变 → 直接返回，+4~+13，:45~:53）→ 记 `ObservedGeneration = rs.Generation`（+19，:59）→ 单次 GET/PUT 重试循环（+23~+53，:63~:93），超过 `statusUpdateRetries`（=1，replica_set.go:81）停止，返回错误触发 requeue。

### 2.4 expectations 机制要点

| 操作 | 语义 | 锚点 |
|---|---|---|
| `ExpectCreations(key, adds)` | 置 add 期望 = adds，del=0（SetExpectations 覆盖旧值） | controller_utils.go `ControllerExpectations.ExpectCreations+0`（:234）→ `SetExpectations`（:228） |
| `ExpectDeletions(key, dels)` | 置 del 期望 = dels，add=0 | controller_utils.go `ControllerExpectations.ExpectDeletions+0`（:238） |
| `CreationObserved(key)` | 原子递减 add 1 | controller_utils.go `ControllerExpectations.CreationObserved+0`（:261） |
| `DeletionObserved(key)` | 原子递减 del 1 | controller_utils.go `ControllerExpectations.DeletionObserved+0`（:266） |
| UIDTracking `ExpectDeletions` | 记录 UID 集后委托底层 ExpectDeletions | controller_utils.go `UIDTrackingControllerExpectations.ExpectDeletions+0`（:371） |
| UIDTracking `DeletionObserved` | 仅当 deleteKey 在 UID 集内才递减 del | controller_utils.go `UIDTrackingControllerExpectations.DeletionObserved+0`（:390） |
| `SatisfiedExpectations` | 满足（Fulfilled: add<=0 && del<=0）或过期即 true | controller_utils.go `ControllerExpectations.SatisfiedExpectations+0`（:196）；`ControlleeExpectations.Fulfilled`（:287） |

**要点：**
- 下发 create/delete 前先 Expect；informer 观测到对应 Pod 事件后 Observed 抵消（addPod :513 `CreationObserved`，deletePod :645 `DeletionObserved`）。
- expectations 未满足期间 `SatisfiedExpectations=false`，syncReplicaSet 跳过 manageReplicas（`rsNeedsSync`，:844），只更新 status。
- 兜底：expectations 有超时（`ExpectationsTimeout = 5 * time.Minute`，controller_utils.go:72；`isExpired` :223），Pod 事件丢失不会永久卡住；MinReady 检查队列也作兜底（:899~:911 注释）。
- **归属修正：** 全部 expectations 类型与逻辑在 **`pkg/controller/controller_utils.go`**（包 `k8s.io/kubernetes/pkg/controller`），非 client-go。

#### expectations 判定与并发语义深度拆解

expectations 解决的问题是「控制器刚下发一批 create/delete，但 informer 还没观测到对应 Pod 事件」——若此时再次调谐，会基于旧观测重复下发，造成抖动与空转。

**SatisfiedExpectations 三分支决策（`ControllerExpectations.SatisfiedExpectations+0`，:196）：**

| 分支 | 条件 | 返回 | 含义 |
|---|---|---|---|
| 已满足 | `exists && exp.Fulfilled()` | **true** | add<=0 && del<=0，期望已全部被 informer 观测抵消 |
| 已过期 | `exists && exp.isExpired()` | **true** | 超过 `ExpectationsTimeout`（5min，:72）仍未观测全，强制放行 |
| 等待中 | `exists`（未满足未过期） | **false** | 跳过 manageReplicas，只更新 status |
| 无记录 / 出错 | `!exists` 或 `err != nil` | **true** | 新控制器无期望，或本地 store 读取出错时**强制同步**（宁多调谐也不卡死） |

**期望的设置与抵消（原子计数）：**
- `ControlleeExpectations` 用 `atomic.LoadInt64/Add` 维护 `add`/`del` 两个计数（`Fulfilled` :287、`GetExpectations` :291）；`Fulfilled` 即 `add<=0 && del<=0`（注释 :288 自问「atomic 为何不重要」）。
- `ExpectCreations`（:234）/`ExpectDeletions`（:238）都委托 `SetExpectations`（:228）——**覆盖式**置 `add`/`del` 并刷新 `timestamp`（用于过期判定），而非累加。
- informer 侧抵消走 `LowerExpectations`（`CreationObserved` :261 / `DeletionObserved` :266 内部）：`exp.Add(-1, 0)` 或 `exp.Add(0, -1)` 原子递减。

**过期兜底（`isExpired` 偏移：+223）：** `clk.Since(e.timestamp) > ExpectationsTimeout`。两条触发路径（SatisfiedExpectations 注释 +215 ～ +222）：
- 下发 create/delete 后 >TTL 仍未观测到期望事件 → 期望过期，控制器醒来重新 create/delete 并重设期望。
- 已满足期望且 >TTL 无需增删 → 期望过期，控制器继续运行但**不再设期望**，直到下次需要增删。

**UIDTracking 变体（`UIDTrackingControllerExpectations`，:350）：** 在 `ControlleeExpectations` 之上记录「已请求删除的 Pod UID 集合」——`ExpectDeletions`（:371）存 UID 集；`DeletionObserved`（:390）**仅当 deleteKey 在集合内**才递减 del。这防止「删除请求与 informer 删除事件错配」导致的 del 计数错乱（例如删除的是同名不同 UID 的 Pod）。

**并发语义（源码可证）：**
1. **计数原子、整体非原子**：单个 add/del 的增减是 atomic 的，但「读 add+del」与「减」之间无事务——`Fulfilled` 注释（:288）自问「为何 atomic 在此不重要」，因为控制器是单 worker 串行调谐同一 RS。
2. **覆盖而非累加**：每轮 `manageReplicas` 下发前用 `ExpectCreations/ExpectDeletions` 重置期望为本轮规模，避免上一轮残留计数累积。
3. **失败回补**：慢启动批量创建中个别 Pod 创建失败时逐个 `CreationObserved` 回补（replica_set.go :745 ～ :751），使期望尽快归零、不靠 5min 超时兜底。

### 2.5 Deployment → ReplicaSet 层级

- `kubectl create deployment`（或 `kubectl run` 不带 `--restart=Never`）创建的是 Deployment（`pkg/controller/deployment`，`NewDeploymentController` 于 `deployment_controller.go:115`）。
- Deployment 控制器调谐时按 `pod-template-hash` 维护 ReplicaSet 集合（滚动更新即新旧 RS 之间按比例切 Replicas）。
- ReplicaSet 再按本文件所述维护 Pod。三层同属 informer→queue→sync 的调谐模式；本文档库只对 ReplicaSet 层做函数级展开。

### 2.6 错误处理汇总

| 来源 | 处理 |
|---|---|
| syncHandler 任意错误 | AddRateLimited 退避重试（processNextWorkItem +14，:670） |
| Create Pod 时 namespace terminating | 视为成功（nil），不重试（manageReplicas +55~+59，:733~:739） |
| 慢启动中部分失败 | 回补 expectations（CreationObserved），等下轮 resync（manageReplicas +67~+72，:745~:751） |
| Delete Pod 返回 NotFound | 忽略（已达目的），不写 errCh（manageReplicas +105~+108，:783~:787） |
| RS status 更新失败 | 返回错误触发 requeue（注释说明可避免热循环；syncReplicaSet +76，:884） |
| selector 转换失败 | 记录日志并返回 nil（等待事件再次唤醒；syncReplicaSet +37~+41，:845~:849） |
| RS 已删除（NotFound） | 清理 expectations 后正常返回 nil，不重试（syncReplicaSet +23~+28，:831~:836） |
| consistencyStore 未就绪 | 返回错误（含 ConsistencyError 指标），requeue（syncReplicaSet +12~+28，:820~:829） |
| PodGroup 获取失败 | 不阻断删除路径；仅当 Pod 过多时仍执行 manageReplicas，最后 errors.Join 返回（syncReplicaSet +34/+67/+88，:842/:875/:896） |

---

## 3. 设计意图与验证

**设计意图（为什么这样设计）：**
- **expectations 抑制过早调谐**：控制器下发 create/delete 后不立即重复调谐，等 informer 观测到对应 Pod 事件（Observed）才解除；避免「下发一批 → 立即看到自己还没创建的 Pod → 再次下发」的抖动与空转。
- **慢启动批量创建**：初始批次 `SlowStartInitialBatchSize=1`，逐轮翻倍；若某批因同一原因（如配额不足）失败，后续批次不再放大，防 Pod create 洪泛与事件风暴。
- **删除并发 + 首个错误返回**：删除是独立副作用，并发执行提升吞吐；只返回第一个错误避免重复上报。
- **状态无条件更新 + 稳态短路**：每轮调谐写 status，保证外部可观察终态；稳态（计数与 generation 未变）短路省一次 PUT。
- **scheduling 集成防御**：避免混合 schedulingGroup（防止 gang 调度 PodGroup 永远达不到 minCount）；podTemplate 深拷贝（不污染存储模板）。

**验证（如何确认实现与本文一致）：**
- **锚点核对**：本文所有「符号+偏移」均可按代码基准 HEAD 复现——结构体 :101、NewReplicaSetController :168、syncHandler :296、Run :302、enqueueRS :396、enqueueRSAfter :406、worker :651、processNextWorkItem :656、manageReplicas :678、syncReplicaSet :808、calculateStatus `replica_set_utils.go:96`、updateReplicaSetStatus `replica_set_utils.go:41`、RealPodControl `controller_utils.go:492`、expectations 三类型 `controller_utils.go` :169/:271/:350、常量 :72/:87。
- **行为验证**：`go test ./pkg/controller/replicaset/...`（含 expectations、slowStartBatch、getPodsToDelete 单测）可作为实现级回归；`hack/verify-gofmt.sh` 与 `make verify` 验证代码基准。
- **未确认项**：无。scheduling 相关（PodGroup）行为以 feature gate `WorkloadWithDeployment` 与 `EnableWorkloadScheduling` 门控（`replica_set.go :154~:163`），默认开启；本文对新增参数与两处新增逻辑的偏移均按当前源码逐行核对。

[上一篇](07-scheduler调度与绑定.md) · [总目录](README.md) · [下一篇](09-kubelet同步循环.md)
