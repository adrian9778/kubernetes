# 06. client-go：REST、Informer、Indexer 和 Workqueue

## 6.1 REST Client

`rest.Config` 包含 Host、TLS、认证、impersonation、QPS/Burst、timeout、proxy 和 ContentConfig。transport 通过 RoundTripper wrappers 添加 bearer token、证书、user-agent、debug、metrics 等。

[request.go](../../../staging/src/k8s.io/client-go/rest/request.go) 逐步设置 verb、namespace、resource、name、subresource、params 和 body。`Do(ctx)` 返回 Result；`Watch(ctx)` 建立 streaming decoder。

Typed client 提供编译期对象；dynamic client 使用 GVR 和 unstructured；metadata client 只传 metadata。三者最终都构造 REST 请求。

## 6.2 Discovery 与 RESTMapper

Discovery client读取 `/api`、`/apis` 和资源列表。Cached discovery 减少重复请求，但 stale cache 会导致新 CRD 暂不可见。DeferredDiscoveryRESTMapper 按需发现并在 NoMatch 时允许 reset。

## 6.3 Reflector

[reflector.go](../../../staging/src/k8s.io/client-go/tools/cache/reflector.go) 执行 ListAndWatch：List 建全量基线和 resourceVersion，Store.Replace；随后 Watch 增量并 Add/Update/Delete。连接关闭、timeout、410 Gone 和 transient error 会进入重连或 relist，并使用 backoff 防雪崩。

## 6.4 DeltaFIFO

[delta_fifo.go](../../../staging/src/k8s.io/client-go/tools/cache/delta_fifo.go) 按 key 保存 Deltas。类型包括 Added、Updated、Deleted、Replaced、Sync。FIFO 排 key，同 key 快速变化聚合。Replace 还要为本地存在而新 List 不存在的对象制造删除语义。

DeletedFinalStateUnknown tombstone 表示删除发生但最后对象无法正常取得。Delete handler 必须同时处理对象和 tombstone。

## 6.5 SharedIndexInformer

[shared_informer.go](../../../staging/src/k8s.io/client-go/tools/cache/shared_informer.go) 把 Reflector、DeltaFIFO、Indexer、sharedProcessor 和 listeners 串起来：先更新 Store，再通知 handler。多个消费者共享一条 List-Watch。

Lister 只读缓存，不访问 API Server。`HasSynced` 表示初始 List 已处理，不代表瞬时强一致。worker 启动前通常必须 `WaitForCacheSync`。

Resync 通常把缓存对象重新作为 Sync 通知，并非强制远端 relist。正确性不应仅依赖 resync。

## 6.6 Indexer

默认 namespace index 支持按 namespace 查询。自定义 IndexFunc 可按 owner UID、nodeName、claim name 建索引，避免全表扫描。IndexFunc 必须快速、确定，并对缺失字段安全。

## 6.7 Workqueue 不变量

[queue.go](../../../staging/src/k8s.io/client-go/util/workqueue/queue.go) 使用 queue、dirty、processing：Add 标记 dirty；Get 移出 dirty并加入 processing；处理中再次 Add 只重新标脏；Done 后若仍 dirty 再入队。因此同 key 不会被两个 worker 同时处理，变化也不会丢。

RateLimitingQueue 在失败时 `AddRateLimited`；成功必须 `Forget` 清除失败历史；每次 Get 都必须 Done。DelayingQueue 用 timer/heap 管理未来可见时间。

## 6.8 标准 worker

```go
func (c *Controller) processNext(ctx context.Context) bool {
    key, shutdown := c.queue.Get()
    if shutdown { return false }
    defer c.queue.Done(key)
    if err := c.sync(ctx, key); err != nil {
        if c.queue.NumRequeues(key) < 10 { c.queue.AddRateLimited(key) } else { c.queue.Forget(key) }
        return true
    }
    c.queue.Forget(key)
    return true
}
```

sync 从 Lister 读最新对象；NotFound 进入删除后的外部清理；修改前 DeepCopy；API 冲突通常让下一事件或显式 requeue 重算。

## 6.9 Leader Election

`client-go/tools/leaderelection` 使用 Lease 等 ResourceLock。LeaseDuration、RenewDeadline、RetryPeriod 共同决定故障转移。选主减少正常并发副作用，但不能替代幂等，因为网络分区下仍可能短暂重叠。

## 6.10 Fake Client

`client-go/testing` 的 Fake 用 reactor 和 Action 模拟调用，适合断言 verb/resource/subresource 并注入错误。ObjectTracker 不完整模拟 default、validation、admission、resourceVersion、apply 和真实 watch；需要这些语义时用 integration test server。

## 6.11 从零重建

按 REST client -> ListWatcher -> Reflector -> DeltaFIFO -> Store/Indexer -> Informer/listeners -> typed Lister -> dirty/processing queue -> rate limiter 的顺序实现。验收必须包含 watch 断线、resourceVersion 过期、tombstone、处理中再次 Add、shutdown 和慢 handler。

