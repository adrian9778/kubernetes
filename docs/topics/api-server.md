[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# API Server 全景专题

> **层级**：跨篇机制纵深专题——把 API Server（kube-apiserver + generic apiserver + kube-aggregator + apiextensions-apiserver）从**进程启动**到**一次请求落 etcd** 的全链路讲透，覆盖启动装配、路由注册、请求处理中间件链、CRUD handlers、storage 层、watch、聚合层、discovery。
>
> **代码基准**：adrian @ `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。所有「符号+偏移」均在该基准上用 MCP `codegraph_explore` 回核。
>
> **锚点记法**：`<完整文件路径>` · `<符号名>` `+<偏移>`。`+0` = 声明行；符号名不带 `func`/`type` 前缀、不带 receiver（如 `(s *store) Create` 写作 `store.Create`）。一律用符号+偏移定位，不使用绝对行号。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。

---

## 0. 结论速览

### 0.1 API Server 是什么、由几个进程组成

API Server 不是单个二进制逻辑块，而是**三层委托的服务器链**（delegation chain），外层请求先到最外层，逐层向内委托：

| 层 | 代码位置 | 职责 |
|---|---|---|
| **apiextensions-apiserver** | `staging/src/k8s.io/apiextensions-apiserver/` | 最内层 delegate。serve CRD（自定义资源） |
| **kube-apiserver（controlplane）** | `cmd/kube-apiserver/` + `staging/src/k8s.io/apiserver/` | 核心层。serve 内置资源（Pod/Service/...），中间件链在此装配 |
| **kube-aggregator** | `staging/src/k8s.io/kube-aggregator/` | 最外层。serve 聚合 API（APIService），把 `/apis/<group>/<version>` 转发给后端 |

三者由 `CreateServerChain` 用 **delegate** 串起来：aggregator（外）→ kubeAPIServer（中）→ apiExtensionsServer（内）。

### 0.2 一次请求的关键结论

1. **请求入口**：所有请求先经 `DefaultBuildHandlerChain` 包裹的**中间件链**（认证→审计→impersonation→流控→鉴权→admission 在 handler 内），再进 `APIServerHandler.Director` 分流到 gorestful 或 `NonGoRestfulMux`。
2. **CRUD 入口**：gorestful 路由由 `APIInstaller.registerResourceHandlers` 按 `rest.Storage` 实现的接口（Creater/Updater/Lister/Watcher/Deleter/Getter）逐资源注册。
3. **写路径**：`createHandler` → admission → validation → `Store.Create` → `etcd3 store.Create` → `OptimisticPut`（乐观事务，`expectedRev=0`）。
4. **读路径**：`Store.Get/List` → etcd3 `Get/GetList`（或 watch cache）。**watch** 走 `Cacher`（内存 watch cache），不打 etcd。
5. **聚合 API**：aggregator 的 `proxyHandler.ServeHTTP` 把匹配 APIService 的请求反向代理到后端 service。

### 0.3 关键数字（本专题覆盖）

- 中间件链：**40+ 层 filter** 包裹（见 §3）
- 三层服务器：apiextensions → kubeAPIServer → aggregator
- storage 写路径：PrepareObjectForStorage → Encode → GetLease → **TransformToStorage（加密）** → OptimisticPut

---

## 1. 生产视角：部署形态与真实入口

### 1.1 部署形态

生产环境 kube-apiserver 通常是：
- **静态 Pod**（kubeadm 部署）或 **systemd 服务**（二进制部署），多副本 + 前置 LB。
- 每个 apiserver 直连 **etcd 集群**（通常 3/5 节点）。
- 前置负载均衡（云 NLB / keepalived VIP / haproxy）。

### 1.2 客户端如何访问 apiserver（以三主三副 6 节点集群为例）

apiserver 起来后监听 HTTPS（默认 `--secure-port 6443`），客户端要发请求需要**三样东西**：**服务器地址、证明「我是谁」的凭据、校验 apiserver 身份的 CA**。下面用一个 6 节点集群把这三样说清楚。

#### 集群拓扑（3 控制面 + 3 工作）

```text
                    客户端（kubectl / curl）
                            │  https://<LB-VIP>:6443
                    ┌───────▼────────┐
                    │  前置 LB / VIP  │   ← 对外唯一入口，转发到某个 apiserver
                    └───┬────┬────┬───┘
        ┌───────────────┘    │    └───────────────┐
   master-1             master-2            master-3      ← 控制面节点
   apiserver            apiserver           apiserver
   scheduler/CM         scheduler/CM        scheduler/CM   (leader election)
   etcd                 etcd                etcd
        └───────────────┬────┴────┬───────────────┘
   node-1               node-2              node-3        ← 工作节点
   kubelet/kube-proxy   kubelet/kube-proxy  kubelet/kube-proxy
   容器运行时            容器运行时           容器运行时
```

6 个节点各自要装什么见 [01-系统骨架 §2.1 三主三副](../sourceReader/01-系统骨架.md#21-进程与组件)。**对外只有一个地址**：LB 的 VIP:6443，它背后轮询三个 apiserver。所以 kubeconfig 里 `server:` 写的是 LB 地址，不是某一台 master。

#### 三种访问方式与所需内容

| 方式 | 用在哪 | 地址 | 凭据（证明我是谁） | CA（校验服务器） | 源码锚点 |
|---|---|---|---|---|---|
| **客户端证书** | 管理员从集群外操作 | `https://<LB-VIP>:6443` | kubeconfig 的 `client-certificate` + `client-key`（x509，CN=用户名、O=组） | `certificate-authority-data` | `pkg/kubeapiserver/authenticator/config.go` · `Config.New` `+21`（x509 分支，`ClientCAContentProvider` → `x509.NewDynamic(..., CommonNameUserConversion)`） |
| **ServiceAccount token** | 集群内 Pod 访问 | `https://kubernetes.default.svc`（ClusterIP） | Pod 挂载的 JWT：`/var/run/secrets/kubernetes.io/serviceaccount/token` | 同目录 `ca.crt` | 同上，SA 分支 `pkg/kubeapiserver/authenticator/config.go` · `Config.New` `+34`（legacy）/`+41`（projected，`ServiceAccountIssuers`）；token 文件路径见 `staging/src/k8s.io/client-go/rest/config.go` · `InClusterConfig` `+2`~`+3` |
| **Bearer token（外部）** | 脚本 / CDN 用长期 token | `https://<LB-VIP>:6443` | 请求头 `Authorization: Bearer <token>` | `--certificate-authority` 或 `insecure-skip-tls-verify` | token 类 authenticator 经 `token/union` · `New`（`tokenunion` 别名，`staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go`）汇入 `request/union` · `New`（`union` 别名） |

认证链如何组织：`Config.New` 把 requestheader / x509 / token 类（legacy-SA、projected-SA、bootstrap、OIDC、webhook）逐个 append，token 类先经 `tokenunion.New(tokenAuthenticators...)`（`Config.New+99`）合成一个，最后所有 authenticator 用 `union.New(authenticators...)`（`Config.New+131`）串成一条链；`Anonymous.Enabled` 时再追一个匿名 authenticator（`Config.New+135`~`+138`）。任何一条命中即解析出 `user.Info`，全不命中 → 401。

#### 例 A：从集群外用客户端证书创建一个 Pod + Service

前提：已有一份管理员 kubeconfig（`kubeadm` 在控制面节点生成于 `/etc/kubernetes/admin.conf`）。它长这样：

```yaml
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://10.0.0.100:6443          # ← LB VIP，不是单台 master
    certificate-authority-data: <base64 CA>  # 校验 apiserver 证书
  name: kubernetes
users:
- user:
    client-certificate-data: <base64 证书>   # 证明「我是 admin」，CN=admin、O=kubeadm:cluster-admins
    client-key-data: <base64 私钥>
  name: admin
contexts:
- context: {cluster: kubernetes, user: admin, namespace: default}
  name: default
current-context: default
```

三样齐了（地址 + 客户端证书 + CA），`kubectl` 就能连。等价的裸 `curl`（把 kubeconfig 里的 base64 解出来用）：

```bash
# 1) 建 namespace（后续对象放进去）
curl -s --cacert ca.crt --cert admin.crt --key admin.key \
  -X POST https://10.0.0.100:6443/api/v1/namespaces \
  -H 'Content-Type: application/json' \
  -d '{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"demo"}}'

# 2) 建一个 Pod
curl -s --cacert ca.crt --cert admin.crt --key admin.key \
  -X POST https://10.0.0.100:6443/api/v1/namespaces/demo/pods \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"v1","kind":"Pod","metadata":{"name":"nginx"},
 "spec":{"containers":[{"name":"c","image":"nginx:1.27"}]}}
JSON

# 3) 建一个 Service 暴露它
curl -s --cacert ca.crt --cert admin.crt --key admin.key \
  -X POST https://10.0.0.100:6443/api/v1/namespaces/demo/services \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"v1","kind":"Service","metadata":{"name":"nginx-svc"},
 "spec":{"selector":{"app":"nginx"},"ports":[{"port":80,"targetPort":80}]}}
JSON
```

这三个 POST 命中的路由正是 §2.2 讲的接口驱动注册：`pods`、`services` 都是 namespaced 资源，在 `pkg/registry/core/rest/storage_core.go` 注册（`pods` 条件块 `+130`、`services` 条件块 `+189`），路径 `/api/v1/namespaces/<ns>/<resource>`。请求进来后走 §3 的中间件链：`endpoints/filters/authentication.go` · `WithAuthentication`（此处 x509 分支解析出 `admin`）→ `endpoints/filters/authorization.go` · `WithAuthorization`（admin 的证书 O=组 `kubeadm:cluster-admins`，该组被绑到 `cluster-admin` ClusterRole，放行）→ `CreateResource → createHandler → Store.Create → store.Create`（§7 的全链路时序）。

> **为什么 Pod 建出来还没 IP、Service 也还没 ClusterIP**：POST 返回的 201 只是 apiserver 把期望状态落进了 etcd。Pod 的 `status.podIP` 要等 scheduler 绑节点、kubelet 起容器后才回写；Service 的 `spec.clusterIP` 由 apiserver 内置的 service registry 在 create 时分配，`Endpoints`/EndpointSlice 则由 EndpointSlice 控制器根据 Pod 就绪状态生成。声明 ≠ 完成，收敛需要时间。

#### 例 B：集群内 Pod 里用 SA token 读自己的信息

一个 Pod 里的进程没有 kubeconfig，靠挂载的 SA 凭据访问：

```bash
# Pod 内自动挂了三样（namespace / token / ca.crt）
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)

# 用 token 访问 —— 服务器地址用内置 Service 的 DNS 名
curl -s --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  -H "Authorization: Bearer $TOKEN" \
  https://kubernetes.default.svc/api/v1/namespaces/$NS/pods
```

这走的是认证链里 SA token 分支（`Config.New+34`/`+41`）：apiserver 用 `ServiceAccountPublicKeysGetter` 校验 JWT 签名，解析出 `system:serviceaccount:<ns>:<sa-name>` 这个用户。**默认这个用户几乎没权限**——要读 Pod 还得先建 RBAC 授权：

```bash
# 授权 demo namespace 里的 Pod 能 list/get pods（集群外执行，admin 身份）
curl -s --cacert ca.crt --cert admin.crt --key admin.key \
  -X POST https://10.0.0.100:6443/apis/rbac.authorization.k8s.io/v1/namespaces/demo/roles \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"Role","metadata":{"name":"pod-reader"},
 "rules":[{"apiGroups":[""],"resources":["pods"],"verbs":["get","list","watch"]}]}
JSON
```

再建一个 `RoleBinding` 把它绑到 Pod 用的 ServiceAccount 上。这一段对应 §8.3 里「RBAC authorizer 决策细节」标注为未深挖的部分——本专题只说明「SA token 解决『我是谁』，RBAC 解决『我能干啥』」，role 聚合与通配符匹配不在本篇展开。

### 1.3 进程启动的真实入口

```text
kube-apiserver 二进制
  └─ NewCommand (cmd/kube-apiserver/app/server.go)
       └─ cobra 命令回调 → Run(ctx, completedOptions)        Run+0
            ├─ NewConfig(opts) → Config
            ├─ Config.Complete() → CompletedConfig
            ├─ CreateServerChain(completed)                    CreateServerChain+0
            ├─ server.PrepareRun()
            └─ prepared.Run(ctx)   ← 阻塞监听
```

**`Run`（server.go `Run+0`）核心动作：**
```text
Run(ctx, opts)
├── logger.Info("Build information", version)   ← 立即打版本号，便于排障
├── informerName = cache.NewInformerName("kube-apiserver")
├── config = NewConfig(opts)
├── completed = config.Complete()
├── server = CreateServerChain(completed)       ← 装配三层服务器
├── prepared = server.PrepareRun()
└── return prepared.Run(ctx)                     ← 启动 HTTP 监听
```

### 1.4 三层委托链装配（`CreateServerChain`，`CreateServerChain+0`）

```text
CreateServerChain(config)
├── notFoundHandler = notfoundhandler.New(serializer, NoMuxAndDiscoveryIncompleteKey)
├── apiExtensionsServer = config.ApiExtensions.New(NewEmptyDelegateWithCustomHandler(notFoundHandler))
│     ↑ 最内层：CRD 服务器，delegate = notFoundHandler
├── crdAPIEnabled = config.ApiExtensions.GenericConfig.MergedResourceConfig.ResourceEnabled(CRD)
├── kubeAPIServer = config.KubeAPIs.New(apiExtensionsServer.GenericAPIServer)
│     ↑ 中层：核心 apiserver，delegate = apiExtensionsServer
└── aggregatorServer = CreateAggregatorServer(config.Aggregator, kubeAPIServer.GenericAPIServer, ...)
      ↑ 最外层：聚合器，delegate = kubeAPIServer
return aggregatorServer
```

**委托语义：** 外层 handler 处理不了（资源不属于自己）就 `delegate.ServeHTTP` 交给内层。请求 `/api/v1/pods` → aggregator 不匹配 APIService → 委托 kubeAPIServer → 路由到 Pod storage。请求 `/apis/custom.metrics.k8s.io/v1beta1/...` → aggregator 匹配 APIService → **代理到后端**（不委托）。

---

## 2. 路由注册与 API 装配

### 2.1 `GenericAPIServer` 与容器

`GenericAPIServer`（`staging/src/k8s.io/apiserver/pkg/server/genericapiserver.go`）持有两个 HTTP 容器：

| 容器 | 类型 | 用途 |
|---|---|---|
| `NonGoRestfulMux` | `*mux.PathRecorderMux` | 非 gorestful 路由（/healthz、/metrics、/version、discovery） |
| `GoRestfulContainer` | `*restful.Container` | gorestful 路由（所有资源 CRUD） |

`NewAPIServerHandler`（`staging/src/k8s.io/apiserver/pkg/server/handler.go` · `NewAPIServerHandler` `+0`）把两者包进 `APIServerHandler`，由 **`director`**（同文件 · `director` `+0`）按请求路径分流：
```text
APIServerHandler.ServeHTTP
└── director.ServeHTTP
     ├── 路径匹配 gorestful 已注册 WebService → GoRestfulContainer
     └── 否则 → NonGoRestfulMux（未实现则 notFoundHandler）
```

### 2.2 资源路由注册（`APIInstaller`）

每个 API group/version 由 `APIInstaller.Install`（`staging/src/k8s.io/apiserver/pkg/endpoints/installer.go` · `APIInstaller.Install` `+0`）装配：

```text
APIInstaller.Install()
├── newWebService()                            创建 restful.WebService
├── for each (path, storage) in group.Storage:
│    └── registerResourceHandlers(path, storage, ws)   `APIInstaller.registerResourceHandlers` `+0`
│         ├── 探测 storage 实现哪些接口：
│         │     Creater → POST、Updater → PUT/PATCH、Getter → GET single、
│         │     Lister → GET collection、Watcher → GET ?watch=true、Deleter → DELETE、
│         │     CollectionDeleter → DELETE collection
│         └── 为每个 action 注册 restful.Route（绑定 handler + 参数 + 响应类型）
└── 返回 discovery 信息 + storageversion 信息 + WebService
```

**`rest.Storage` 接口驱动注册（关键设计）：** 一个资源暴露哪些 HTTP 动词，取决于它实现哪个接口：

| 接口 | HTTP | handler |
|---|---|---|
| `rest.Creater` | POST | `CreateResource` |
| `rest.Updater` / `rest.Patcher` | PUT / PATCH | `UpdateResource` / `PatchResource` |
| `rest.Getter` | GET (single) | `GetResource` |
| `rest.Lister` | GET (collection) | `ListResource` |
| `rest.Watcher` | GET (?watch) | `WatchServer` |
| `rest.Deleter` | DELETE (single) | `DeleteResource` |
| `rest.GracefulDeleter` | DELETE (graceful) | 带 gracePeriod |

`registerResourceHandlers` 用类型断言逐个探测，命中就 `ws.Route(...)` 注册对应路由。这是「**接口驱动的能力发现**」——加一个资源只需实现对应接口，路由自动生成。

### 2.3 Discovery 与 OpenAPI

- **`/api`、`/apis`**：`NewRootAPIsHandler`（`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/root.go` · `NewRootAPIsHandler` `+0`）返回所有 group/version 列表。
- **`/apis/<group>/<version>`**：group/version handler 返回该版本的资源列表（名称、动词、kind）。
- **`/version`**：版本信息。
- **`/openapi/v2`、`/openapi/v3`**：OpenAPI 文档，由各 handler 注册时附带的响应 schema 聚合生成。

---

## 3. 请求处理中间件链（`DefaultBuildHandlerChain`）

这是 API Server 最核心的一段。`DefaultBuildHandlerChain`（`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultBuildHandlerChain` `+0`）把业务 handler 用 **40+ 层 filter** 逐层包裹。**外层先执行、内层后执行**（近业务 handler），而源码里 `handler = WithX(handler)` 的**书写顺序与包裹后执行顺序相反**——最先写的 `WithAuthorization` 在最内（最后执行），最后写的 `WithRequestInfo` 在最外（最先执行）。

### 3.1 中间件链完整顺序（外→内，即执行顺序）

```text
请求进来，依次经过（外层 → 内层）：
1.  WithPanicRecovery / WithPanicRecover        捕获 panic
2.  WithMuxAndDiscoveryComplete                  等 discovery ready
3.  WithEndpointLoadMetrics                      端点负载指标
4.  WithRequestReceivedTimestamp                 记录请求接收时间
5.  WithRequestInfo                              解析 RequestInfo（group/version/resource/verb）
6.  WithRoutine                                  goroutine 管理
7.  WithLatencyTrackers / WithHTTPLogging        延迟追踪 + HTTP 日志
8.  WithRetryAfter / WithHSTS / WithCacheControl 安全响应头
9.  WithProbabilisticGoaway                      概率性 goaway（优雅缩容）
10. WithWatchTerminationDuringShutdown           关闭时终止长连接 watch
11. WithWaitGroup                                等待组（优雅关闭）
12. WithRequestDeadline / WithTimeoutForNonLongRunningRequests   请求超时
13. WithWarningRecorder                          记录 warning header
14. WithCORS                                     跨域
15. WithAuthentication                           ★ 认证（解析用户身份）
16. WithTracing                                  分布式追踪
17. WithFailedAuthenticationAudit                认证失败审计
18. WithAudit                                    ★ 审计（记录审计事件）
19. WithImpersonation / WithConstrainedImpersonation   ★ 模拟用户
20. WithMaxInFlightLimit                         （旧）全局并发上限
21. WithPriorityAndFairness                      ★ APF 流控（排队/拒绝）
22. TrackStarted/TrackCompleted                  延迟度量包裹
23. WithConditionsAwareAuthorization             （feature gate，默认关）
24. WithAuthorization                            ★ 鉴权（RBAC/Node/Webhook）
    └── 业务 handler（gorestful / NonGoRestfulMux）
```

**三个最关键的横切关注点：**
- **认证（`endpoints/filters/authentication.go` · `WithAuthentication` `+0`）**：用 authenticator 链（client cert / bearer token / service account / OIDC / requestheader）解析出 `user.Info`，注入 request context。**失败走 `failedHandler`**（=`Unauthorized` 包 `WithFailedAuthenticationAudit`，见 `DefaultBuildHandlerChain+46`~`+47`），返回 401。注意 `WithFailedAuthenticationAudit` **不在主链**，只在认证失败路径触发——认证失败也留审计。
- **鉴权（`endpoints/filters/authorization.go` · `WithAuthorization` `+0`）**：用 authorizer（RBAC / Node / Webhook / ABAC）判断 `user` 对 `RequestInfo` 标识的资源是否有权执行 `verb`。失败 403。
- **流控（`WithPriorityAndFairness`）**：APF 按 FlowSchema 分类、PriorityLevel 排队，超载返回 429（详见 [优先级与公平性流控专题](优先级与公平性流控.md)）。

> 说明：`WithAuthentication`/`WithAuthorization` 均定义在 **`endpoints/filters/`** 包（`genericapifilters` 别名），**不在** `admission/chain.go`——admission 包里只有 `NewChainHandler`（`staging/src/k8s.io/apiserver/pkg/admission/chain.go` · `NewChainHandler` `+0`）这条 admission 链。两者在 `DefaultBuildHandlerChain` 里的装配点分别是 `+54`（authn）与 `+13`（authz，feature gate 关闭时走 `WithAuthorization` 分支）。

**认证/鉴权链的装配位置：** authenticator 链在 `pkg/kubeapiserver/authenticator/config.go`（`Config.New`）装配：requestheader → x509 → token-file/legacy-SA/JWT-SA/bootstrap/OIDC/webhook → union。authorizer 链在 `pkg/kubeapiserver/authorizer/config.go` + `reload.go`（Node/ABAC/RBAC/Webhook/AlwaysAllow 各 mode builder）。

**审计与模拟（精确锚点）：**
- `WithAudit`（`endpoints/filters/audit.go` · `WithAudit` `+0`）：记录审计事件（含 defer/panic/long-running stage）；策略评估在 `evaluatePolicyAndCreateAuditEvent+0`（同文件）。
- `WithImpersonation`（`filters/impersonation/impersonation.go` · `WithImpersonation` `+0`）：每请求做 `Authorize(verb=impersonate)` 检查 + `LogImpersonatedUser`（`audit/request.go` · `LogImpersonatedUser` `+0` → `audit/context.go` · `AuditContext.LogImpersonatedUser` `+0`）。
- `WithConstrainedImpersonation`（`filters/impersonation/constrained_impersonation.go` · `WithConstrainedImpersonation` `+0`，独立文件，feature gate `ConstrainedImpersonation` 开启时替换 `WithImpersonation`）。

### 3.2 admission 在哪一层

**admission 不在中间件链里**，而在 **CRUD handler 内部**（`createHandler`/`updateHandler` 显式调用）。原因是 admission 需要解码后的对象 + 操作语义（CREATE/UPDATE/DELETE），与 HTTP 层解耦。顺序：**mutating admission → 对象默认化/validation → validating admission**。

admission 链由 `NewChainHandler`（`staging/src/k8s.io/apiserver/pkg/admission/chain.go` · `NewChainHandler` `+0`）构造，`admission.Interface`（`admission/interfaces.go` · `Interface` `+0`）定义 `Admit`/`Validate`。

**两种 admission 的触发时机不同（精确）：**
- **mutating admission 内联触发**：`createHandler` 直接调 `mutatingAdmission.Admit(ctx, admissionAttributes, scope)`（`endpoints/handlers/create.go` · `createHandler` `+150`）。
- **validating admission 经 storage 写触发**：handler 把 `rest.AdmissionToValidateObjectFunc(admit, attrs, scope)`（调用点 `createHandler+135`，函数本体在 `registry/rest/create.go` · `AdmissionToValidateObjectFunc` `+0`）作为 validate 回调传给 `Store.Create`，validating 在 storage 写路径内才执行（update 用 `AdmissionToValidateObjectUpdateFunc` `+0`，见 `registry/rest/update.go`）。这样保证 validating 看到的是最终写入对象。

---

## 4. CRUD Handlers（gorestful → storage）

### 4.1 Create 全流程（`createHandler`）

`CreateResource`（`endpoints/handlers/create.go` · `CreateResource` `+0`）是注册到路由的入口，它返回 `createHandler(...)`（同文件 · `createHandler` `+0`）这个 http.HandlerFunc：

```text
CreateResource(creater, scope, admission) → createHandler
createHandler(req, w)                               createHandler+0
├── namespace = scope.NamespaceFrom(req)            取 ns（cluster-scoped 为空）
├── 走 gorestful 参数注入 body（decoder）
├── admit = admission（已传入）
├── rest.BeforeCreate / 对象默认化
├── obj, err = create(r, namespace, obj)            实际创建
└── 写响应（201 + Location header）
```

**create 内部（写路径核心）：**
```text
create(creater, req, scope, admit, namespace, ...)
├── 解码 body → obj（按 Content-Type 协商）
├── admission.Admit(ctx, attrs, obj)               ★ mutating admission
├── validation（对象级校验，registry/rest/validate）
├── result, err = creater.Create(ctx, obj, validateFunc, options)
│     ↑ 进 generic registry Store.Create
└── 序列化 result 写响应
```

### 4.2 Update / Patch / Delete

- **`UpdateResource`**（`endpoints/handlers/update.go` · `UpdateResource` `+0`）：PUT 全量更新，走乐观并发（ResourceVersion 前置条件）。
- **`PatchResource`**（`endpoints/handlers/patch.go` · `PatchResource` `+0`）：支持 JSON Patch / Merge Patch / Strategic Merge Patch / Server-Side Apply。先取当前对象 → 应用 patch → 走 update 路径。
- **`DeleteResource`**：DELETE，支持 graceful deletion（`GracefulDeleter`）。

### 4.3 generic registry `Store`（连接 handler 与 etcd）

`Store`（`registry/generic/registry/store.go`）实现 `rest.Storage` 全套接口：

| 方法 | 锚点 | 作用 |
|---|---|---|
| `Store.Create` | `Store.Create` `+0` | 校验 → `Storage.Create`（etcd） |
| `Store.Update` | `Store.Update` `+0` | 乐观更新（RV 前置条件） |
| `Store.Get` | `Store.Get` `+0` | 单对象读 |
| `Store.List` | `Store.List` `+0` | 集合读（支持分页/label selector） |
| `Store.Delete` | `Store.Delete` `+0` | 删除（含 graceful） |
| `Store.Watch` | `Store.Watch` `+0` | 建立 watch |

**Store 不直接操作 etcd**，而是持有 `Storage storage.Interface`（`storage/interfaces.go` · `Interface` `+0`，由 storage factory 注入，可能是 etcd3 直连或 `Cacher` 包装）。它负责对象级语义：`CreateStrategy`/`UpdateStrategy`/`DeleteStrategy`（默认化、校验、canonicalize）、namespace 校验、generation 维护。

#### Store.Create 内部链（`Store.create`，registry store.go）

`Store.Create` 委托给私有 `Store.create`（`+0`），其内部顺序：

```text
Store.create(ctx, obj, createValidation, options)     Store.create+0
├── rest.BeforeCreate(e.CreateStrategy, ctx, obj)      Store.create+24
│     ↑ BeforeCreate 在 registry/rest/create.go BeforeCreate+0
├── createValidation(ctx, obj)                         Store.create+29  ← validating admission 回调
└── e.Storage.Create(ctx, key, obj, out, ttl, dryRun)  Store.create+49
      ↑ 真正落 etcd（经 dryrun 包装）
```

#### Get / List / Watch handlers（同一文件 get.go）

读路径 handler 都在 `endpoints/handlers/get.go`：

| handler | 锚点 | 作用 |
|---|---|---|
| `GetResource` | `GetResource` `+0` | GET 单对象 |
| `ListResource` | `ListResource` `+0` | GET 集合（含 `?watch=true` 分流） |
| `handleWatch` | `handleWatch` `+0` | watch 长连接 |
| `handleList` | `handleList` `+0` | list 主体 |

DELETE 路由的注册辅助 `restfulDeleteResource` 在 `endpoints/installer.go` · `restfulDeleteResource` `+0`。

---

## 5. Storage 层（etcd3）

### 5.1 `store.Create` 写路径（etcd3）

`etcd3 store.Create`（`staging/src/k8s.io/apiserver/pkg/storage/etcd3/store.go` · `store.Create` `+0`，实现 `storage.Interface.Create`）是真正落 etcd 的地方：

```text
store.Create(ctx, key, obj, out, ttl)                 store.Create+0
├── s.prepareKey(key, false)                          store.Create+1    key 防逃逸（store.prepareKey+0）
├── s.versioner.PrepareObjectForStorage(obj)          store.Create+16   版本化预处理（非加密）
├── data = runtime.Encode(s.codec, obj)               store.Create+20   序列化
├── if ttl != 0: lease = s.leaseManager.GetLease(...) store.Create+28   短 TTL 对象用 lease
├── newData = s.transformer.TransformToStorage(...)   store.Create+35   ★ 加密变换（见 §5.3）
└── txnResp = s.client.Kubernetes.OptimisticPut(ctx, preparedKey, newData, 0, {LeaseID: lease})
      ↑ ★ 乐观事务，expectedRev=0（store.Create+43，Create 幂等：已存在即失败）
```

**注意：** `OptimisticPut` 来自外部包 `go.etcd.io/etcd/client/v3/kubernetes`（经 `s.client.Kubernetes` 调用），**不在仓库内**——仓库内仅有测试记录器 `storage/testing/recorder.go` · `KubernetesRecorder.OptimisticPut+0`。乐观锁重试逻辑在 `storage/txn.go` · `GuaranteedUpdate`。

**关键点：**
1. **加密在 storage 层**：`TransformToStorage` 加密后才落 etcd，密文对 etcd 透明。
2. **Create 用 `OptimisticPut` + `expectedRev=0`**：保证「已存在即失败」的 Create 幂等语义。
3. **lease 复用**：短 TTL 对象（如 Event、lease）用 lease，`leaseManager.GetLease` 复用 lease 减少开销。

### 5.2 乐观并发（`GuaranteedUpdate`）

`etcd3 store.GuaranteedUpdate`（`store.go` · `store.GuaranteedUpdate` `+0`）与 `storage/txn.go` · `GuaranteedUpdate` `+0` 实现乐观事务：
```text
GuaranteedUpdate(key, dest, preconditions, tryUpdate, origState)
└── for:                                          乐观重试循环
     ├── 取当前状态 origState
     ├── newObj = tryUpdate(origState)            用户更新函数
     ├── d.UpdateOnce(...)                        条件于 Rev 写入
     ├── RV 冲突 / 前置条件失败 → refreshOnError 重读
     │     └── refreshOnError（txn.go · refreshOnError+0）：
     │           st.Current → 返回原错误
     │           cachedRev == currentState.Rev → 数据没变，返回原错误
     │           否则 → 重读后重试
     └── 成功 → 返回
```
**语义：** 只有当缓存 rev 与 etcd 当前 rev **不同**（数据真变了）才重试，避免「错误是别的原因却无限重试」。

### 5.3 加密变换（`Transformer`）

`value.Transformer`（`storage/value/transformer.go` · `Transformer` `+0`）= `Read` + `Write`（`TransformFromStorage`/`TransformToStorage`）。`prefixTransformers.TransformToStorage`（同文件 · `prefixTransformers.TransformToStorage` `+0`）按前缀匹配多个 transformer，**每个用自己 prefix 作 `authenticatedDataString`**（AAD，绑定 key 与密文）。

实现：
- `identityTransformer`：恒等（不加密）。
- `envelopeTransformer`（KMSv2）：DEK 加密数据，DEK 用 KEK（KMS provider）加密后存 etcd annotation（`storage/value/encrypt/envelope/kmsv2/envelope.go` · `kmsv2EnvelopeTransformer.TransformToStorage`）。
- 压缩 transformer：先压缩再加密（`storage/value/compression/compression.go` · `compressedValue.TransformToStorage`）。

### 5.4 Watch 与 Watch Cache

- **etcd3 `store.Watch`**（`store.go` · `store.Watch` `+0`）：打开 etcd watch，把 etcd 事件转为 Kubernetes watch.Event。
- **`Cacher.Watch`**（`storage/cacher/cacher.go` · `Cacher.Watch` `+0`）：**内存 watch cache**。`Cacher`（同文件 · `Cacher` `+0`）由 `NewCacherFromConfig` 构造，List 一次全量到内存，后续 watch 从内存 buffer 服务，**不打 etcd**。这是读扩展的关键——大量 watch 客户端共享一份内存 buffer。

**读路径分流：** `Store.Get/List/Watch` 经 storage factory 注入的可能是 `Cacher` 包装的 etcd3 store。命中 cache 的读不打 etcd。

---

## 6. 聚合层（Aggregation）

### 6.1 kube-aggregator 与 APIService

aggregator 在最外层，持有 `APIService` 资源表。每个 `APIService` 声明「`/apis/<group>/<version>` 由哪个后端 service 服务」。

### 6.2 `proxyHandler.ServeHTTP`

`proxyHandler`（`staging/src/k8s.io/kube-aggregator/pkg/apiserver/handler_proxy.go` · `proxyHandler` `+0`）是聚合代理核心。`proxyHandler.ServeHTTP`（同文件 · `proxyHandler.ServeHTTP` `+0`）：

```text
proxyHandler.ServeHTTP(w, req)
├── 解析请求的 group/version → 查 APIService
├── APIService 不存在/未就绪 → 404/503
├── 解析后端 service（APIService.spec.service）→ Endpoints
├── 负载均衡选一个后端 pod IP
├── 改写请求 Host/URL 指向后端
├── 转发请求（带原始认证头 / 或重新签名）
└── 回写后端响应
```

**语义：** 聚合 API 请求**不落本 apiserver 的 etcd**，而是反向代理到后端（metrics-server、prometheus-adapter 等自定义 apiserver）。

### 6.3 CRD 服务（apiextensions）

- `crdHandler.getOrCreateServingInfoFor`（`staging/src/k8s.io/apiextensions-apiserver/pkg/apiserver/customresource_handler.go` · `crdHandler.getOrCreateServingInfoFor` `+0`）：为每个 CRD 按需构建动态 REST handler（`serveResource`），缓存 serving info。
- CRD 请求 → apiextensions-apiserver → 动态 handler → CRD storage（etcd）。
- CRD 变更（新增/更新）触发 **establish controller** 把 CRD 标记为 established（可服务）。

---

## 7. 全链路时序（一次 POST）

```text
客户端 POST /api/v1/namespaces/default/pods
  └─ [中间件链 DefaultBuildHandlerChain+0]
       WithRequestInfo（解析 pods/create）
       WithAuthentication（解析 user）
       WithAudit（记录审计）
       WithImpersonation（模拟用户）
       WithPriorityAndFairness（APF 排队/放行）
       WithAuthorization（RBAC 判断 create pods）
       └─ Director → GoRestfulContainer
            └─ APIInstaller 注册的路由 → CreateResource → createHandler
                 ├─ admission.Admit（mutating）
                 ├─ validation
                 └─ Store.Create（registry Store · `Store.Create`）
                      └─ store.Create（etcd3 · `store.Create`，实现 `storage.Interface.Create`）
                           ├─ PrepareObjectForStorage
                           ├─ Encode
                           ├─ prepareKey
                           ├─ TransformToStorage（加密）
                           └─ OptimisticPut（expectedRev=0）→ etcd
                                └─ 201 Created + Location
```

---

## 8. 验证与边界

### 8.1 代码回核

本专题所有「符号+偏移」均在基准 `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 上用 MCP `codegraph_explore` 回核（下列均确认为当前定义行；行号请用文档库自带校验脚本实时获取，本文不写绝对行号）：

| 锚点 | 文件 |
|---|---|
| `Run` / `CreateServerChain` / `CreateKubeAPIServerConfig` | `cmd/kube-apiserver/app/server.go` |
| `DefaultBuildHandlerChain` | `staging/src/k8s.io/apiserver/pkg/server/config.go` |
| `NewAPIServerHandler` / `director` | `staging/src/k8s.io/apiserver/pkg/server/handler.go` |
| `APIInstaller.Install` / `APIInstaller.registerResourceHandlers` / `restfulDeleteResource` | `staging/src/k8s.io/apiserver/pkg/endpoints/installer.go` |
| `createHandler` / `CreateResource` | `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go` |
| `UpdateResource` | `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go` |
| `PatchResource` | `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/patch.go` |
| `GetResource` / `ListResource` / `handleWatch` / `handleList` | `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/get.go` |
| `Store.Create` / `Store.create` / `Store.Update` / `Store.Get` / `Store.List` / `Store.Delete` / `Store.Watch` | `staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go` |
| `BeforeCreate` / `AdmissionToValidateObjectFunc` | `staging/src/k8s.io/apiserver/pkg/registry/rest/create.go` |
| `BeforeUpdate` / `AdmissionToValidateObjectUpdateFunc` | `staging/src/k8s.io/apiserver/pkg/registry/rest/update.go` |
| `store.Create` / `store.GuaranteedUpdate` / `store.Watch` / `store.prepareKey` | `staging/src/k8s.io/apiserver/pkg/storage/etcd3/store.go` |
| `GuaranteedUpdate` / `refreshOnError` / `TxnDelegator` | `staging/src/k8s.io/apiserver/pkg/storage/txn.go` |
| `Transformer` / `prefixTransformers.TransformToStorage` | `staging/src/k8s.io/apiserver/pkg/storage/value/transformer.go` |
| `Cacher.Watch` / `Cacher` | `staging/src/k8s.io/apiserver/pkg/storage/cacher/cacher.go` |
| `proxyHandler.ServeHTTP` / `proxyHandler` | `staging/src/k8s.io/kube-aggregator/pkg/apiserver/handler_proxy.go` |
| `crdHandler.getOrCreateServingInfoFor` | `staging/src/k8s.io/apiextensions-apiserver/pkg/apiserver/customresource_handler.go` |
| `NewChainHandler` | `staging/src/k8s.io/apiserver/pkg/admission/chain.go` |
| `WithAuthentication` | `staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go` |
| `WithAuthorization` | `staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go` |
| `WithAudit` / `evaluatePolicyAndCreateAuditEvent` | `staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go` |
| `WithImpersonation` | `staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go` |
| `WithConstrainedImpersonation` | `staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/constrained_impersonation.go` |
| `Config.New`（authenticator 装配） | `pkg/kubeapiserver/authenticator/config.go` |
| `Interface`（storage）/ `Interface`（admission） | `storage/interfaces.go` / `admission/interfaces.go` |

**全部偏移为 `+0`（声明行）**，除正文标注的体内偏移（`store.Create` `+35` 加密、`+43` OptimisticPut；`Config.New` `+21` x509、`+34` legacy-SA、`+41` projected-SA、`+99` tokenunion、`+131` union、`+135` anonymous；`createHandler` `+135` validate 回调、`+150` mutating Admit；`Store.create` `+24`/`+29`/`+49`）。上述体内偏移以 `codegraph_explore` 返回的定义行为 `+0` 推算。

### 8.2 生产实测

**未实测** `[待运行验证]`。中间件链实际执行顺序、加密 provider 行为、聚合代理转发、watch cache 命中率均为代码推断，未在真实集群抓包/查 etcd 验证。

### 8.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| 认证 authenticator 各 provider 细节 | 未深挖 | client-cert/OIDC/service-account token 各自实现未逐行读 |
| RBAC authorizer 决策细节 | 未深挖 | role/clusterrole 聚合、通配符匹配未展开 |
| OpenAPI 生成 | 未深挖 | schema 聚合与 v2/v3 差异未读 |
| watch cache 一致性 | 未深挖 | cache 与 etcd 的 RV 对齐、bookmark 机制未展开 |
| CRD establish controller | 未深挖 | CRD 变为 established 的控制循环未读 |

### 8.4 修改联动

改动这些前必查：
- 改中间件顺序 → `DefaultBuildHandlerChain`（config.go），同步本文 §3。
- 改 storage 写路径 → `etcd3 store.Create`/`OptimisticPut`，同步 [etcd3写入路径专题](etcd3写入路径.md)。
- 改流控 → `WithPriorityAndFairness`，同步 [优先级与公平性流控专题](优先级与公平性流控.md)。
- 改路由注册 → `registerResourceHandlers`，同步 §2.2。

**关联文档：** [05-apiserver启动与路由](../sourceReader/05-apiserver启动与路由.md)、[06-apiserver请求处理与etcd](../sourceReader/06-apiserver请求处理与etcd.md)、[07-scheduler调度与绑定](../sourceReader/07-scheduler调度与绑定.md)、[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md)；专题 [etcd3写入路径](etcd3写入路径.md)、[优先级与公平性流控](优先级与公平性流控.md)。

[总目录](../sourceReader/README.md)
