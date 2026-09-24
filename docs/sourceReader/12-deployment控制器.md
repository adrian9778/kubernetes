[上一篇](11-informer与workqueue机制.md) · [总目录](README.md) · [下一篇](13-job与cronjob控制器.md)

# Deployment 控制器
> **层级**：第四层——函数级源码解析。
> **范围**：Deployment 如何管理 ReplicaSet 集合、滚动更新的先扩后缩、回滚、状态计算。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 控制器目录 | `pkg/controller/deployment/` |
| 入口文件 | `deployment_controller.go`（控制器主体，含 syncDeployment）、`sync.go`、`rolling.go`、`recreate.go`、`rollback.go`、`progress.go` |
| 工具 | `util/deployment_util.go`（RS 查找、边界值计算、状态计数） |
| 依赖 | RS/Pod/Deployment 三类 informer、`RealRSControl`（RS 增删改）、string 泛型队列 |
| 数据流 | Deployment 变化/RS、Pod 反查 → queue → syncDeployment → 选定 rollout 策略 → scale RS → 写 status |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `DeploymentController` | `deployment_controller.go:67` | 持有 rsControl、d/rs/pod Lister、podIndexer、三个 Synced、syncHandler、`queue TypedRateLimitingInterface[string]` |
| `RealRSControl` | 同包 | RSControl 接口实现：CreateReplicaSet/UpdateReplicaSet |
| （RS 侧类型见 08 篇） | — | — |

## 3. 控制器运行函数级

### 函数：NewDeploymentController
**源码：** `deployment_controller.go:104`
**职责：** 构造队列（名 "deployment"）、RealRSControl，注册三类 informer handler，赋值 `syncHandler=dc.syncDeployment`（绑定点）、`enqueueDeployment=dc.enqueue`（:156）。

### 函数：Run / worker / processNextWorkItem
**源码：** `deployment_controller.go:172 / :482 / :487`
**执行流程：** 启动事件管道 → `WaitForNamedCacheSync`（三个 informer 全同步）→ 按 workers 数 `wait.UntilWithContext(dc.worker)`。processNextWorkItem：Get → syncHandler → 成功 Forget；失败走 handleErr（:500），重试上限 maxRetries=15，超限 AddRateLimited。

### 函数：enqueue / enqueueRateLimited / enqueueAfter
**源码：** `deployment_controller.go:400 / :410 / :421`
**入键：** `cache.MetaNamespaceKeyFunc` 得到的 `namespace/name`。

### 函数：informer handler
**源码：** `deployment_controller.go`
- Deployment：addDeployment（:202）/updateDeployment（:208）/deleteDeployment（:215，tombstone）
- RS：addReplicaSet:235/updateReplicaSet:290/deleteReplicaSet:337——经 ControllerRef 或孤儿匹配反查所属 Deployment
- Pod：只注册 DeleteFunc（deletePod:371），沿 Pod→RS→Deployment 反查

## 4. sync 主流程函数级

### 函数：syncDeployment
**源码：** `deployment_controller.go:575`
**职责：** 单次调谐的分派器。
**执行流程：**
```text
syncDeployment (+0)
├── 1. lister.Get（NotFound 正常返回）(+14)；DeepCopy (+25)
├── 2. getReplicaSetsForDeployment（:525，ControllerRef 认领/释放）(+38)
├── 3. 删除中 → syncStatusOnly（sync.go:45）(+44)
├── 4. checkPausedConditions（sync.go:82）(+51)
├── 5. Paused → sync（sync.go:57：getAllRS→scale:307→cleanup→status）(+54)
├── 6. getRollbackTo（rollback.go:124）非空 → rollback（rollback.go:32）(+63 ～ +64)
├── 7. isScalingEvent（sync.go:538）→ sync (+65)
└── 8. 按 Strategy：
     ├─ Recreate → rolloutRecreate（recreate.go:29）
     └── RollingUpdate → rolloutRolling（rolling.go:31）(+74 ～ +76)
```

### 函数：(*DeploymentController).sync
**源码：** `sync.go:57`
**职责：** Paused/纯伸缩场景：`getAllReplicaSetsAndSyncRevision`（:124）→ `scale`（:307，把全部 RS 的 Replicas 对齐）→ cleanupDeployment → syncDeploymentStatus。

### 函数：rollback
**源码：** `rollback.go:32`
**职责：** 按 annotation 中的 rollback revision 找到目标旧 RS，用其 template 覆盖当前 Deployment template，随后走正常 rollout 路径。

## 5. RS 管理函数级

### 函数：getAllReplicaSetsAndSyncRevision / getNewReplicaSet
**源码：** `sync.go:124 / :146`
**职责：** 枚举全部 RS 并同步 Deployment 的 revision annotation；getNewReplicaSet 先查后建。
**新建 RS：** `dc.getNewReplicaSet` 内部（sync.go:195 ～ +300）构造 `apps.ReplicaSet`（template + pod-template-hash label + ControllerRef），`ReplicaSets().Create`（:232），hash 碰撞处理（:234 ～ +268）。

### 函数：FindNewReplicaSet / FindOldReplicaSets
**源码：** `util/deployment_util.go:626 / :643`
**职责：** 按 pod-template-hash 区分新 RS（template 完全一致）与旧 RS 集合；FindOldReplicaSets 内部先调 FindNewReplicaSet。
**相关：** FindActiveOrLatest（:356，无活跃 RS 时取最新一个）、MaxRevision（revision 比较）。

### 函数：NewRSNewReplicas
**源码：** `util/deployment_util.go:817`
**职责：** 滚动更新中计算新 RS 本轮应达到的副本数（受 maxSurge 与全部 RS 当前规模约束）。

## 6. 滚动更新函数级

### 函数：rolloutRolling
**源码：** `rolling.go:31`
**职责：** 滚动更新主循环，严格先扩后缩。
**执行流程：**
1. `getAllReplicaSetsAndSyncRevision(..., createIfNotExisted=true)`（+1）
2. 先扩：`reconcileNewReplicaSet`（+8）；扩了立即 syncRolloutStatus 返回（+11 ～ +14）
3. 后缩：`reconcileOldReplicaSets`（+17）；缩了立即返回（+20 ～ +23）
4. DeploymentComplete 后 cleanupDeployment 清理超限旧 RS（+26 ～ +30）
5. syncRolloutStatus（+34）

### 函数：reconcileNewReplicaSet
**源码：** `rolling.go:68`
**执行流程：** 已达期望 → false（+1 ～ +3）；超期望 → scaleReplicaSet 缩回（+4 ～ +7）；不足 → NewRSNewReplicas 算目标后 scaleReplicaSet（+9 ～ +15）。

### 函数：reconcileOldReplicaSets
**源码：** `rolling.go:86`
**职责：** 决定旧 RS 的缩容量并执行。
**缩容量公式（:124 ～ +129）：**
```text
minAvailable = replicas - maxUnavailable
newRSUnavailablePodCount = newRS.replicas - newRS.available
maxScaledDown = allPodsCount - minAvailable - newRSUnavailablePodCount
```
该公式保证新 RS 未就绪的 Pod 也占用"不可用预算"，旧 RS 不会被过早缩掉（注释 :101 ～ +128 给出配额场景推演）。
**内部步骤：** `cleanupUnhealthyReplicas`（:155，先缩旧 RS 的不健康副本）→ `scaleDownOldReplicaSetsForRollingUpdate`（:193，按 maxScaledDown 在旧 RS 间分配）。

### 函数：scaleReplicaSet
**源码：** `sync.go:407`
**职责：** 实际 Update RS，同时写 DesiredReplicas/MaxReplicas 注解（供 RS 控制器和观测使用）。

### 边界函数：MaxUnavailable / MaxSurge
**源码：** `util/deployment_util.go:446 / :467`
**职责：** 经 ResolveFenceposts 解析 IntOrString（百分比按期望副本数取整，边界值最小保证 1）。

## 7. Recreate 与状态函数级

### 函数：rolloutRecreate
**源码：** `recreate.go:29`
**职责：** 先把旧 RS 全部缩到 0 并等待 Pod 删除完成，再把新 RS 扩到期望数；期间 Deployment 不对外提供服务。

### 函数：calculateStatus / syncDeploymentStatus
**源码：** `sync.go:493 / :479`
**职责：** 汇总 Replicas/Updated/Ready/Available 计数（util 的 GetAvailableReplicaCount 系列），判定 Available condition；deep-equal 后 UpdateStatus。

### 函数：syncRolloutStatus
**源码：** `progress.go:36`
**职责：** 滚动场景维护 Progressing condition；`requeueStuckDeployment`（:160）按 progressDeadlineSeconds 对停滞的 rollout 报 TimedOut，并安排下次检查时间。

## 8. 调用关系总览
```text
syncDeployment
 ├── getReplicaSetsForDeployment
 ├── sync（paused/scale）→ scale → scaleReplicaSet
 ├── rollback
 ├── rolloutRecreate
 └── rolloutRolling
      ├── getAllReplicaSetsAndSyncRevision → getNewReplicaSet（Create RS）
      ├── reconcileNewReplicaSet → NewRSNewReplicas → scaleReplicaSet
      ├── reconcileOldReplicaSets
      │    ├── MaxUnavailable
      │    ├── cleanupUnhealthyReplicas
      │    └── scaleDownOldReplicaSetsForRollingUpdate → scaleReplicaSet
      ├── cleanupDeployment
      └── syncRolloutStatus → calculateStatus → UpdateStatus
```

## 9. 设计意图（源码可证）
- 每个 Deployment revision 对应一个独立 RS，回滚即把旧 revision 的 RS 重新扩起，无需重建 Pod template 历史。
- 先扩后缩 + maxScaledDown 公式把"可用性预算"集中计算，新旧 RS 的规模在每轮被同时约束。
- Pod delete 事件也反查入队：Pod 被意外删除时 Deployment 能立即唤醒，而不必等 RS 的 resync。

[上一篇](11-informer与workqueue机制.md) · [总目录](README.md) · [下一篇](13-job与cronjob控制器.md)
