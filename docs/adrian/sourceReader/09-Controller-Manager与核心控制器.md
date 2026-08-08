# 09. Controller Manager 与核心控制器

## 9.1 宿主进程

Controller Manager 使用 ControllerDescriptor 注册控制器。获得 leader 后创建 ControllerContext、构造 enabled controllers、启动 shared informers 并运行控制器。当前集合还包含 ResourceClaim、DeviceTaintEviction、ValidatingAdmissionPolicyStatus、StorageVersionMigrator 等新控制器。

## 9.2 标准控制器骨架

```text
Informer Add/Update/Delete
 -> enqueue namespace/name
 -> queue worker
 -> Lister.Get latest object
 -> reconcile desired vs observed
 -> API create/update/patch/delete
 -> success Forget / temporary failure AddRateLimited
```

Event handler 应快速且只做 key 推导。业务动作在 worker，确保可限速、可重试、可停止。

## 9.3 ReplicaSet

[replica_set.go](../../../pkg/controller/replicaset/replica_set.go) 是最佳入门。它观察 RS 和 Pod，使用 ControllerRefManager claim/release Pod，计算 active Pods 与 spec.replicas 的差，批量创建或选择删除，并更新 status。

Expectations 处理 API 写成功但 informer 尚未观察到的窗口。创建前提高 expectation，观察 Pod add/delete 后降低；否则缓存延迟会导致重复创建。Expectation 必须可过期，避免丢事件后永久阻塞。

## 9.4 Deployment

[deployment_controller.go](../../../pkg/controller/deployment/deployment_controller.go) 管理 ReplicaSet，不直接管理 Pod。它根据 template hash 找新 RS，处理 RollingUpdate/Recreate、pause、revision、比例缩放、maxSurge/maxUnavailable 和 progressDeadline。

阅读方法：固定“3 副本、maxSurge=1、maxUnavailable=0”的升级场景，逐轮记录新旧 RS replicas 和 availableReplicas，再进入 helper。

## 9.5 StatefulSet

StatefulSet 保持 ordinal identity、稳定网络名、PVC 和 ControllerRevision。OrderedReady 与 Parallel 决定创建顺序；partition 控制滚动更新边界；PVC retention 影响 scale/delete。Pod 不可互换，因此不能套用 RS 的集合差算法。

## 9.6 Job 与 CronJob

Job 处理 completions、parallelism、backoffLimit、indexed completion、pod failure policy 和 terminal conditions。CronJob 根据 schedule、lastScheduleTime、startingDeadlineSeconds、concurrencyPolicy 创建 Job，并避免同一计划点重复创建。

时间相关控制器必须注入 clock 测试，处理错过多个周期、时钟跳变和控制器重启。

## 9.7 DaemonSet

DaemonSet 为符合条件的每个 Node 维持 Pod。它计算 nodeShouldRunDaemonPod，处理 taint/toleration、node affinity、rolling update、surge 和 misscheduled Pods。它直接设置 Node affinity 而非等待普通 Scheduler 选择任意节点。

## 9.8 Node Lifecycle 与 Taint Eviction

Node Lifecycle 观察 Node 和 Lease，更新 Ready/Unknown，按 zone health 和 grace period控制污点/驱逐速率。大规模节点失联时需要保护机制，不能瞬间驱逐全区 Pod。Taint Eviction 已有独立控制器路径，需结合 NoExecute tolerationSeconds。

## 9.9 Garbage Collector

GC 使用 metadata informer 和 discovery 构建 UID 依赖图。GraphBuilder 处理 owner/dependent 边，attemptToDelete/attemptToOrphan 队列执行 foreground/background/orphan。UID 而非 name 防止同名重建误删。

## 9.10 Namespace Controller

Namespace terminating 后，控制器 discovery 所有 namespaced resources，删除内容、等待清空、移除 finalizer。失效 APIService、不可用 CRD、资源 finalizer 都可卡住 namespace。

## 9.11 EndpointSlice Controller

观察 Service、Pod、Node，按 address type、port 和容量切分 EndpointSlice，维护 owner/labels 和 endpoint conditions。Readiness、terminating/serving、topology hints/traffic distribution影响后续 kube-proxy 选择。

## 9.12 Volume Controllers

AttachDetach Controller 维护 desired/actual state of world，协调 VolumeAttachment 和节点 attach/detach；PV binder 处理 PV/PVC 匹配、预绑定、动态 provisioning；Expand Controller 处理容量扩展。CSI external-provisioner/attacher/resizer 多在仓库外，但 Kubernetes 内置控制器维护 API 协议与迁移边界。

## 9.13 HPA、Quota 与 Disruption

HPA 从 metrics APIs 获取指标，经 ReplicaCalculator 计算期望副本，应用 stabilization 和 behavior policies。ResourceQuota 计算 namespace 使用量并写 status。Disruption Controller 维护 PDB disruptionsAllowed 与 disruptedPods。

## 9.14 控制器错误策略

NotFound 通常表示完成；Conflict 重新读取重算；429/网络错误退避；无效用户配置写 Condition 等待新 generation；外部资源清理用 finalizer 且必须幂等。Status 写前比较，避免无变化更新自激。

## 9.15 重建控制器宿主

先实现一个 Replica Controller，再抽取 descriptor、shared context、client builder、health check、leader election 和多个 workers。每个控制器必须可从 API 状态恢复，不能把关键进度只放内存。

