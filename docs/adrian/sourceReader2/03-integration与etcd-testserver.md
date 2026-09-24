# Integration 与 Etcd Testserver

## 1. 为什么需要 Integration

当测试要验证以下任一行为时，内存 fake 通常不够：

- REST URL 到 storage 的路由。
- admission、defaulting、conversion、validation 的顺序。
- protobuf/JSON/CBOR 序列化。
- watch、resourceVersion、list/watch 一致性。
- etcd transaction、lease、compaction。
- controller 与 API Server 的闭环。
- scheduler assume/bind 与 API 更新。
- authentication、authorization、audit、encryption at rest。

## 2. 入口和目录

入口：

```bash
make test-integration WHAT=./test/integration/pods
make test-integration WHAT=./test/integration/scheduler GOFLAGS=-v
make test-integration WHAT=./test/integration/pods \
  KUBE_TEST_ARGS='-run ^TestPodUpdateActiveDeadlineSeconds$'
```

关键文件：

- `hack/make-rules/test-integration.sh`
- `test/integration/framework/test_server.go`
- `test/integration/framework/controlplane_utils.go`
- `test/integration/framework/etcd.go`
- 各套件的 `main_test.go`

当前树在 `test/integration` 下约有 353 个 `*_test.go` 文件。

## 3. 套件启动链

典型 `main_test.go` 负责 `TestMain`：解析公共 flag、准备环境，然后调用 `m.Run()`。单个测试通过 framework 启动 server。

```text
go test 启动测试二进制
  -> TestMain 注册/解析 integration flags
  -> test case 创建 context 与临时目录
  -> framework 启动 etcd
  -> 构造 controlplane/apiserver config
  -> 启动 secure serving listener
  -> 等待 /readyz
  -> 创建 REST client/clientset
  -> 启动待测 controller/scheduler
  -> 执行 API 操作并观察结果
  -> cancel context
  -> server shutdown
  -> etcd terminate
  -> 清理临时文件
```

这里最重要的是生命周期所有权。谁创建 goroutine、listener、etcd，就必须注册 cleanup；否则单独用例成功、整包运行却泄漏端口或 goroutine。

## 4. Etcd testserver

关键文件：

- `staging/src/k8s.io/apiserver/pkg/storage/etcd3/testserver/test_server.go`
- `staging/src/k8s.io/apiserver/pkg/storage/etcd3/testing/test_server.go`

关键入口：

- `testserver.NewTestConfig(t)`：为嵌入式 etcd 构造隔离配置。
- `testserver.RunEtcd(t, config)`：启动 etcd，返回可操作 server。
- `testing.NewUnsecuredEtcd3TestClientServer(t)`：包装 etcd server 和 Kubernetes storage backend config。
- `EtcdTestServer.Terminate(t)`：终止并清理。

调用关系：

```text
RunEtcd
  -> 创建临时 data dir
  -> 分配 client/peer URL
  -> embed.StartEtcd
  -> 等待 ReadyNotify 或超时/错误
  -> 建立 clientv3.Client
  -> 返回 TestServer
```

Kubernetes storage 测试再把 endpoint 写入 `storagebackend.Config`，由 generic registry/store 创建 etcd3 storage。

## 5. 并发与状态机

### API Server readiness

```text
Configured -> ListenerStarted -> PostStartHooksRunning -> Ready
                         \-> hook error -> Failed
context canceled -> Draining -> Stopped
```

测试必须在 Ready 后发请求。只因为端口已监听，不代表 informer、bootstrap controller 或 post-start hook 已就绪。

### List/Watch

```text
LIST 获取对象与 resourceVersion
  -> WATCH 从该版本开始
  -> etcd event
  -> storage watch 解码/转换
  -> HTTP watch encoder
  -> client decoder
```

测试若先制造事件再建立 watch，可能遗漏事件；若 resourceVersion 过旧，可能得到 Gone。fake tracker 往往不会暴露这些问题。

### Controller integration

```text
API Create
  -> watch/informer reflector
  -> DeltaFIFO
  -> informer handler
  -> workqueue key
  -> worker Get
  -> reconcile
  -> API write
  -> 再次 watch
```

验收应最终观察 API 对象状态，而不是调用 controller 私有函数。

## 6. 代表性测试区域

- `test/integration/controlplane`：API Server、CRD、审计、加密、graceful shutdown。
- `test/integration/auth`：认证、授权、RBAC、service account token。
- `test/integration/apimachinery`：watch、watchlist、apply。
- `test/integration/scheduler`：调度队列、插件、bind、抢占。
- `test/integration/garbagecollector`：owner reference 和删除传播。
- `test/integration/storageversion`：storage version 迁移和 GC。
- `test/integration/volume`：PV/PVC、attach/detach。
- `test/integration/daemonset|replicaset|statefulset`：控制器闭环。

## 7. 常见失败定位

| 症状 | 首查 |
|---|---|
| connection refused | server 是否 ready、context 是否提前 cancel |
| etcd address in use | cleanup 泄漏、并行用例共享固定端口 |
| watch 超时 | watch 建立顺序、resourceVersion、selector |
| 偶发对象未出现 | informer cache sync、异步 reconcile 等待方式 |
| 整包失败单测成功 | 全局 feature gate、metrics 重复注册、goroutine/端口泄漏 |
| cleanup 卡住 | shutdown 顺序、未消费 channel、worker 没有观察 stop |

## 8. 新手重建最小控制面测试架构

1. 启动嵌入式 KV，提供 revision 和 watch。
2. 实现 REST storage，把 CRUD 映射为 KV 事务。
3. 实现 API Server readiness。
4. 实现 client 的 list/watch。
5. 实现 informer cache 与 queue。
6. 实现一个 controller，例如 ReplicaSet。
7. 测试创建父对象后最终出现子对象。
8. 测试冲突重试、删除、watch 重连和 shutdown。

