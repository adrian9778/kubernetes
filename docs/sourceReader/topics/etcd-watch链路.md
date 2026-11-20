[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# etcd watch 链路

> **层级**：跨篇机制纵深专题——把 **watch** 这条 kube-apiserver 最关键的实时推送链路讲透：etcd3 watcher → watchChan 事件处理 → Cacher（watch cache）→ apiserver watch handler → 客户端。这是 informer、控制器、kubectl `-w` 共同依赖的数据平面。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `w.Watch+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[06-apiserver请求处理与etcd](../06-apiserver请求处理与etcd.md) §watch 是浓缩版；[informer-resync机制](informer-resync机制.md) 讲客户端 resync；本专题纵向展开服务端 watch 推送链路。

---

## 0. 结论速览

### 0.1 watch 解决什么问题

让客户端**实时**感知资源变化，而不用轮询。客户端发一个 watch 请求，apiserver 保持长连接，资源一变就推送事件（ADDED/MODIFIED/DELETED）。

### 0.2 三层 watch 架构

| 层 | 组件 | 作用 |
|---|---|---|
| **etcd 层** | `etcd3 watcher`/`watchChan` | 打开 etcd watch，把 etcd 事件转为 Kubernetes watch.Event |
| **缓存层** | `Cacher`（watch cache） | 内存缓存 + 滑动窗口，多个客户端共享一份 etcd watch |
| **接口层** | apiserver watch handler | 把 watch.Event 序列化后流式推给客户端 |

### 0.3 关键结论

1. **Cacher 是读扩展关键**：多个客户端 watch 同一资源共享一份内存 buffer，**不打 etcd**。
2. **背压而非丢弃**：watchChan 的事件分发是阻塞 select——客户端消费慢时反向阻塞 etcd 事件读取，**不丢事件**（与 PLEG 丢弃策略相反）。
3. **三种事件类型**：ADDED/MODIFIED/DELETED（`watch.Event`）。
4. **sync 先全量后增量**：watchChan 启动时先 sync（List 当前状态），再 startWatching 增量。
5. **支持并发事件处理**：`concurrentProcessEvents`（feature gate）可并发处理事件。

---

## 1. 生产视角：watch 对外行为

### 1.1 客户端可见行为

- `kubectl get pod -w`：保持连接，资源变化实时打印。
- informer：List 一次 + Watch 增量，本地缓存与 apiserver 保持一致。
- 事件流：ADDED/MODIFIED/DELETED 三种。

### 1.2 可观测信号

- watch 长连接数（apiserver 指标）。
- `watch cache` 命中率。
- 客户端过慢 → 背压阻塞 etcd 读取（不丢事件，但可能拖慢）。

---

## 2. 开发视角：etcd 层（etcd3 watcher）

### 2.1 `watcher.Watch`——打开 watch（watcher.go `w.Watch+0`，:140）

```text
Watch(ctx, key, rev, opts)                     w.Watch+0
├── getStartWatchResourceVersion(...)          w.getStartWatchResourceVersion+0 (:206)
│     ↑ 算起始 RV（含 bookmark/initial-events 语义）
├── createWatchChan(...)                       w.createWatchChan+0 (:175)
│     ↑ 构造 watchChan（含 pred、recursive、progressNotify 等选项）
└── go wc.run(...)                             wc.run+0 (:282)
      ↑ 后台 goroutine：先 sync 全量，再 startWatching 增量
└── return wc（watch.Interface）
```

### 2.2 `watchChan.run`——sync + watch（`wc.run+0`，:282）

```text
run(initialEventsEndBookmarkRequired, forceInitialEvents)   wc.run+0
├── sync() / syncPaginated() / syncStreamRecursive()          wc.sync+0 (:322)
│     ↑ 先 List 当前状态（分页/流式），产 ADDED 事件
└── startWatching(watchClosedCh, ...)                          wc.startWatching+0 (:468)
      ↑ 打开 etcd watch，进入事件循环
```
**先全量后增量：** 保证客户端从「当前状态」开始，不错过 watch 建立前的变化。

### 2.3 `startWatching`——etcd 事件循环（`wc.startWatching+0`，:468）

打开 etcd watch stream，循环接收 etcd 事件，转成 Kubernetes watch.Event 推入 channel。

### 2.4 事件处理（`wc.processEvents+0`，:562）

```text
processEvents(wg)                              wc.processEvents+0
├── serialProcessEvents(wg)                    wc.serialProcessEvents+0 (:570)  默认串行
└── concurrentProcessEvents(wg)                wc.concurrentProcessEvents+0 (:593)  feature gate 并发
```

---

## 3. 开发视角：缓存层（`Cacher`）

### 3.1 `Cacher.Watch`——从内存 buffer 服务（cacher.go `c.Watch+0`，:797）

```text
Cacher.Watch(ctx, key, opts)                    c.Watch+0
└── watch(...)                                  c.watch+0 (:848)
     ↑ 从 watchCache 滑动窗口回放历史事件 + 订阅后续事件
```
**意义：** 多个客户端 watch 同一资源共享一份 etcd watch + 内存 buffer，**不打 etcd**——这是 apiserver 读扩展的关键。

### 3.2 `dispatchEvents`——后台分发（cacher.go `c.dispatchEvents+0`，:1252）

后台 goroutine 把 etcd 事件分发给所有订阅的 cacheWatcher。`startDispatching`（:1449）处理单个事件。

---

## 4. 开发视角：接口层（apiserver watch handler）

### 4.1 `ListResource` → `handleWatch`（get.go）

`ListResource+0`（get.go :168）检测 `?watch=true` → `handleWatch+0`（get.go :258）：
```text
handleWatch(ctx, rw, scope, req, w, opts, ...)
├── rw.Watch(...)  调 storage 层（Cacher 或 etcd3）拿 watch.Interface
└── serveWatchHandler(...)                        serveWatchHandler+0 (watch.go:74)
      ↑ 序列化 watch.Event 流式推给客户端（chunked）
```

### 4.2 watch.Event 三类型（watch/watch.go）

`Added`/`Modified`/`Deleted`（watch.go :57-59），`Event{Type, Object}`（:70）。 Deleted 携带删除前状态。

---

## 5. 全链路时序（一次 informer watch）

```text
informer Reflector 发起 watch
└── apiserver ListResource (get.go:168) 检测 watch=true
     └── handleWatch (get.go:258)
          └── storage.Watch → Cacher.Watch (cacher.go:797)
               └── c.watch (:848) 从滑动窗口回放 + 订阅
                    └──（缓存未命中/直连）etcd3 w.Watch (watcher.go:140)
                         ├── wc.run (:282) → sync (:322) 全量 ADDED
                         └── wc.startWatching (:468) → etcd watch 增量
                              └── wc.processEvents (:562) → 事件推 channel
                                   └── serveWatchHandler (watch.go:74) 序列化推客户端
                                        └── informer 收事件 → DeltaFIFO → handler
```

---

## 6. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| 不丢事件（背压） | `wc.processEvents+0`（阻塞 select） | 客户端慢时反压 etcd 读取，informer 不漏变更 |
| 先全量后增量 | `wc.run+0`（sync → startWatching） | 从当前状态开始，不漏 watch 前的变化 |
| 读扩展 | `Cacher.Watch+0`（共享内存 buffer） | 大量客户端 watch 不打爆 etcd |
| bookmark/进度 | `w.getStartWatchResourceVersion+0` | 客户端断连重连不重复/不漏 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- etcd3：`w.Watch+0`（watcher.go:140）、`w.createWatchChan+0`（:175）、`w.getStartWatchResourceVersion+0`（:206）、`wc.run+0`（:282）、`wc.sync+0`（:322）、`wc.startWatching+0`（:468）、`wc.processEvents+0`（:562）、`wc.serialProcessEvents+0`（:570）、`wc.concurrentProcessEvents+0`（:593）
- Cacher：`c.Watch+0`（cacher.go:797）、`c.watch+0`（:848）、`c.dispatchEvents+0`（:1252）
- 接口层：`ListResource+0`（get.go:168）、`handleWatch+0`（:258）、`serveWatchHandler+0`（watch.go:74）
- watch.Event：Added/Modified/Deleted（watch/watch.go:57-59）

### 7.2 生产实测

**未实测** `[待运行验证]`。watch cache 命中率、背压实际表现、bookmark 断连重连语义均为代码推断，未在真实集群抓 watch 流验证。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| bookmark 机制 | 未深挖 | `progressNotify`/bookmark 事件细节未逐行读 |
| watch cache 淘汰 | 未深挖 | 滑动窗口容量与淘汰策略未展开 |
| concurrentProcessEvents | 未深挖 | 并发事件处理的 feature gate 与顺序保证未读 |
| watch 断连重连 | 未深挖 | 客户端重连的 RV 对齐细节未展开 |

### 7.4 修改联动

- 改 etcd watch → `wc.startWatching`/`wc.processEvents`，同步本文 §2。
- 改 watch cache → `Cacher.Watch`/`c.watch`/`dispatchEvents`，同步 §3。
- 改客户端 → informer Reflector，同步 [informer-resync机制](informer-resync机制.md)。

**关联文档：** [06-apiserver请求处理与etcd](../06-apiserver请求处理与etcd.md)（watch handler 浓缩）、[informer-resync机制](informer-resync机制.md)（客户端侧）、[api-server](api-server.md)（watch cache 定位）、[etcd3写入路径](etcd3写入路径.md)（写路径，watch 的对偶）。

[总目录](../README.md)
