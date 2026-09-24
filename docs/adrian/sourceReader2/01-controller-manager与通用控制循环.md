# controller-manager 与通用控制循环

## 1. 进程入口和控制器注册

入口为 [`cmd/kube-controller-manager/controller-manager.go`](../../../cmd/kube-controller-manager/controller-manager.go)，命令构造在 [`NewControllerManagerCommand`](../../../cmd/kube-controller-manager/app/controllermanager.go#L109)，主运行函数是 [`Run`](../../../cmd/kube-controller-manager/app/controllermanager.go#L199)。

控制器注册中心是 [`ControllerDescriptor`](../../../cmd/kube-controller-manager/app/controller_descriptor.go#L50)。描述符保存稳定名称、构造函数、feature gate、别名、默认禁用和特殊启动标记。全部内置控制器在 [`NewControllerDescriptors`](../../../cmd/kube-controller-manager/app/controller_descriptor.go#L138) 注册。

新增控制器的真实接线顺序是：

```text
定义稳定名称
  -> 编写 newXControllerDescriptor()
  -> 在 NewControllerDescriptors() register
  -> 构造函数从 ControllerContext 取得 informer/client/config
  -> 返回实现 Run(context.Context) 的 Controller
```

Deployment、ReplicaSet、StatefulSet 构造分别位于 [`apps.go:128`](../../../cmd/kube-controller-manager/app/apps.go#L128)、[`apps.go:102`](../../../cmd/kube-controller-manager/app/apps.go#L102)、[`apps.go:75`](../../../cmd/kube-controller-manager/app/apps.go#L75)。Job 位于 [`batch.go:43`](../../../cmd/kube-controller-manager/app/batch.go#L43)。

## 2. 启动时序

[`Run`](../../../cmd/kube-controller-manager/app/controllermanager.go#L199) 的关键顺序如下：

```text
启动 EventBroadcaster
  -> 注册 configz/healthz/metrics/secure server
  -> 创建普通 clientBuilder 与 rootClientBuilder
  -> 选主，或直接运行
  -> CreateControllerContext
  -> BuildControllers：先构造全部控制器
  -> 启动 SharedInformerFactory
  -> close(InformersStarted)
  -> RunControllers
```

“先构造控制器、再启动 informer”很重要。构造阶段给共享 informer 注册 handler/indexer；若 informer 已运行，晚注册的 handler 可能看到不完整的初始事件窗口。

[`ControllerContext`](../../../cmd/kube-controller-manager/app/controllermanager.go#L463) 提供：

| 字段 | 作用 |
|---|---|
| `ClientBuilder` | 为每个控制器创建独立限流 client |
| `InformerFactory` | typed shared informer |
| `ObjectOrMetadataInformerFactory` | GC 等动态资源使用对象或 metadata informer |
| `RESTMapper` | GVR/GVK 映射，定时 reset discovery cache |
| `InformersStarted` | 控制器可安全启动动态 informer 的屏障 |
| `ResyncPeriod` | 加随机因子，避免控制器同时 relist |
| `GraphBuilder` | GC 与其他控制器共享的依赖图构建器 |

共享 informer 在 [`CreateControllerContext`](../../../cmd/kube-controller-manager/app/controllermanager.go#L530) 中配置 transform，删除 `ManagedFields` 以降低控制面内存占用。

[`BuildControllers`](../../../cmd/kube-controller-manager/app/controllermanager.go#L626) 先启动 ServiceAccount token controller，再构造普通控制器，并为实现 `HealthCheckable` 或 `Debuggable` 的控制器挂载真实检查和调试端点。

## 3. Leader election

不开启选主时直接执行控制器集合。开启后，以 `hostname_UUID` 为 identity 获取主锁。`OnStartedLeading` 的 context 是所有控制器的生命周期根；失去锁会取消 context，默认配置下进程退出，避免旧 leader 继续写。

Leader migration 使用主锁和迁移锁两组控制器集合。代码先等待 ServiceAccount token controller 发出 `MigrationReady`，再竞争迁移锁，防止两个实例分别持有不同锁而同时控制同一资源域。

## 4. WorkQueue 为什么只保存 key

典型 worker 见 [`ReplicaSetController.processNextWorkItem`](../../../pkg/controller/replicaset/replica_set.go#L627)：

```go
key, quit := queue.Get()
defer queue.Done(key)
err := sync(key)
if err == nil {
    queue.Forget(key)
} else {
    queue.AddRateLimited(key)
}
```

队列保存 `namespace/name`，不保存对象快照。合并同 key 的连续事件后，worker 总是从 lister 读取较新的缓存状态。`Get/Done` 还保证同一个 key 在一次处理期间不会被另一个 worker 并发执行；期间再次 `Add` 会在 `Done` 后重新入队。

`Forget` 清除 rate limiter 历史，`AddRateLimited` 指数退避。业务上的定时状态转换使用 `AddAfter`，例如 `minReadySeconds`；它不是错误重试。

## 5. Expectations：写后观察屏障

核心类型是 [`ControllerExpectations`](../../../pkg/controller/controller_utils.go#L169) 和 [`UIDTrackingControllerExpectations`](../../../pkg/controller/controller_utils.go#L340)。

ReplicaSet 创建 Pod 前调用 [`ExpectCreations`](../../../pkg/controller/controller_utils.go#L236)，删除前记录准确的 Pod key。Pod informer 的 add/delete handler 分别调用 `CreationObserved` 和 `DeletionObserved`。只有 [`SatisfiedExpectations`](../../../pkg/controller/controller_utils.go#L195) 为真时，下一次 sync 才允许再次增删。

它解决下面的重复创建竞态：

```text
sync 看到 0/3 -> 请求创建 3 个 Pod -> API 返回成功
                                     informer 还没看到新 Pod
另一个事件触发 sync -> 若无 expectations，仍看到 0/3 -> 又创建 3 个
```

expectations 不是事务。它有超时，防止 informer 永远没收到事件时控制器永久停摆。API 写失败时必须主动降低 expectation，因为不会有对应 informer 事件。

## 6. OwnerReference、adopt 和 release

[`PodControllerRefManager.ClaimPods`](../../../pkg/controller/controller_ref_manager.go#L183) 对每个 Pod 执行三态判断：

| Pod 状态 | selector 匹配 | 动作 |
|---|---:|---|
| owner UID 是当前控制器 | 是 | claim |
| owner UID 是当前控制器 | 否 | release，除非控制器/Pod 正在删除 |
| 无 controller owner | 是 | adopt |
| 被其他 controller 控制 | 任意 | 忽略 |

adopt 前通过 [`RecheckDeletionTimestamp`](../../../pkg/controller/controller_ref_manager.go#L391) 做一次 uncached API GET，并校验 UID。这避免旧控制器对象已删除、同名新对象已创建时，旧缓存实体错误领养孤儿。

OwnerReference 的 UID 才是身份；名称只是定位线索。`blockOwnerDeletion` 配合 GC 的 foreground deletion。Finalizer 则反向表达“对象删除前，本控制器还必须完成工作”。

## 7. v1.37 ConsistencyStore

expectations 只追踪子对象数量事件，不能覆盖 status 写、PVC 写等任意资源。当前 ReplicaSet 和 StatefulSet 可建立 `ConsistencyStore`，记录控制器写入的 resourceVersion；下一轮 sync 开头 `EnsureReady` 检查 informer store 是否至少追上该 RV。未追上时返回可重试错误，不基于旧缓存决策。

ReplicaSet 检查位于 [`syncReplicaSet`](../../../pkg/controller/replicaset/replica_set.go#L755)，StatefulSet 检查位于 [`sync`](../../../pkg/controller/statefulset/stateful_set.go#L524)。这是 v1.37 阅读时不能忽略的新并发边界。

