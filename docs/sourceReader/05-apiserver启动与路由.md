[上一篇](04-kubectl客户端.md) · [总目录](README.md) · [下一篇](06-apiserver请求处理与etcd.md)

# apiserver 启动与路由

> **层级**：第四层——函数级源码解析。
> **范围**：进程启动、三层委托 server 构造、handler 中间件链、HTTP 分流（director）、RESTStorage 到 go-restful 路由的生成。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **锚点记法**：源码引用用「符号 + 偏移」；函数定义行 = `+0`，其余为相对定义行的偏移。

## 1. 生产视角

### 1.1 对外角色

| 项 | 内容 |
|---|---|
| 进程 | `kube-apiserver`，`cmd/kube-apiserver` 构建的可执行文件 |
| 对外接口 | HTTPS REST API；默认 6443 端口（`--secure-port`） |
| 可见结果 | 客户端（kubectl、controller-manager、scheduler 等）经它读写集群对象；对外暴露 discovery（`/api`、`/apis`）、openapi、healthz/readyz/livez、metrics 等端点 |
| 内部语义 | 以 `APIGroupInfo`（版本 → `map[string]rest.Storage`）描述资源；`rest.Storage` 是每个资源的能力抽象 |
| 委托链 | 请求先到最外层 aggregator → kube-apiserver → apiextensions（CRD）→ NotFound |

### 1.2 请求入口

```text
客户端 HTTPS 请求
  ↓
SecureServing（TLS 监听）
  ↓
APIServerHandler.ServeHTTP → FullHandlerChain（DefaultBuildHandlerChain 的中间件链）
  ↓
director.ServeHTTP（按已注册 webservice 分流）
  ├── go-restful 认领（/apis、/apis/ 精确路径，或前缀 + 边界匹配）
  └── 其余 → NonGoRestfulMux（非 API 端点、委托 fallthrough）
  ↓
（go-restful 分支）GoRestfulContainer → APIGroupVersion.InstallREST 注册的 Route → handlers.CreateResource 等
```

### 1.3 可见结果（本次范围）

本层负责「把进程拉起来、把 RESTStorage 变成可响应的 HTTP 路由」；具体的对象编解码、etcd 读写、admission、watch 推送不在本层，见 06。

---

## 2. 模块级

| 项 | 内容 |
|---|---|
| 入口模块 | `cmd/kube-apiserver`（main）、`cmd/kube-apiserver/app`（命令与装配） |
| K8s 控制面装配 | `pkg/controlplane/apiserver`（KubeAPIServer 的 Config/New/Server）、`pkg/controlplane`（CompletedConfig.New 调 InstallAPIs）、`pkg/registry/core/rest`（核心组 RESTStorage 构造） |
| 通用 API 框架 | `[staging] k8s.io/apiserver/pkg/server`（GenericAPIServer、Config、handler 链）、`.../endpoints`（路由安装）、`.../registry/generic` |
| 模块对外接口 | HTTPS REST API；内部以 `APIGroupInfo`（版本 → `map[string]rest.Storage`）描述资源 |
| 主数据流 | options → Config → 委托 server 链 → 安装 APIGroup → 生成路由 → 启动 SecureServing |

---

## 3. 类型级

| 类型 | 文件 | 职责 |
|---|---|---|
| `Config`（controlplane） | `pkg/controlplane/apiserver/config.go:70` | KubeAPIServer 的构造配置，内嵌 `genericapiserver.Config` 与 `Extra` |
| `Server`（controlplane） | `pkg/controlplane/apiserver/server.go:80` | KubeAPIServer 实例：持有 `GenericAPIServer`、资源配置源、RESTOptionsGetter；`InstallAPIs` 方法挂在它上面 |
| `GenericAPIServer` | `[staging] apiserver/pkg/server/genericapiserver.go:111` | 通用 API server：持有 Handler、APIGroup 安装、委托目标、discovery |
| `APIGroupInfo` | `[staging] apiserver/pkg/server/genericapiserver.go:73` | 一个 group 的版本 → RESTStorage map 与序列化协商 |
| `APIGroupVersion` | `[staging] endpoints/groupversion.go:50` | 单个 group/version 的安装输入（Storage map、Creater、converter、根前缀） |
| `APIInstaller` | `[staging] endpoints/installer.go:63` | 把每个 rest.Storage 的能力接口翻译成一组 go-restful 路由 |
| `APIServerHandler` | `[staging] apiserver/pkg/server/handler.go:42` | FullHandlerChain + GoRestfulContainer + NonGoRestfulMux + Director 的聚合体 |
| `director` | `handler.go:115` | 按已注册 webservice 判断，把请求分给 go-restful 或 NonGoRestfulMux |
| `PodStorage` | `pkg/registry/core/pod/storage/storage.go:58` | Pod 主资源与全部子资源的 RESTStorage 集合 |

---

## 4. 函数级

### 函数：main

**源码：** `cmd/kube-apiserver/apiserver.go` `main` +0
**职责：** 进程入口。
**执行流程：**
1. `app.NewAPIServerCommand()`（+1）
2. `cli.Run(command)`（+2），返回退出码
3. `os.Exit(code)`（+3）

### 函数：NewAPIServerCommand

**源码：** `cmd/kube-apiserver/app/server.go` `NewAPIServerCommand` +0（:74）
**职责：** 从默认 options 构造 cobra 命令，注册 flags。
**执行流程：**
1. `options.NewServerRunOptions()`（+2）：默认选项
2. `signals.SetupSignalContext()`（+3）：signal 上下文
3. 取该组件的 FeatureGate（+4）
4. 构造 `cobra.Command`（+6）：`Use`、`Long`、`SilenceUsage`、`PersistentPreRunE`（+15，注册 ComponentGlobalsRegistry 并静默 client-go warning）、`RunE`（+24，打印版本→校验 flags→`s.Complete(ctx)`→`completedOptions.Validate()`→`Run(ctx, completedOptions)`）、`Args`（+50，禁止位置参数）
5. `cmd.SetContext(ctx)`（+59）
6. 注册 flags（+61 起）：`verflag`、`globalflag`、各 namedFlagSets；设置 usage/help（+72）
**返回值：** `*cobra.Command`（+75 返回）。

### 函数：Run

**源码：** `cmd/kube-apiserver/app/server.go` `Run` +0（:152）
**职责：** 端到端启动序列。
**参数：** `ctx context.Context`、`opts options.CompletedOptions`。
**返回值：** `error`。
**执行流程：**
1. 打印版本与 Go 运行参数（+3 ～ +5）
2. 建 informer name（+7 ～ +13）
3. `NewConfig(opts)`：解析选项为各 server 的 Config（+15）
4. `config.Complete()`：补全默认值与派生配置（+19）
5. `CreateServerChain(completed)`：构造委托 server 链（+23）
6. `server.PrepareRun()`：安装非资源 handler、准备探针与生命周期信号（+28）
7. `prepared.Run(ctx)`：启动 SecureServing 并阻塞（+33）
**状态变化：** 从纯配置过渡到监听端口的服务进程；Run 阻塞直到 shutdown。
**错误处理：** 任一步失败立即返回，无部分启动回滚（各 server 尚未起 goroutine）。

### 函数：CreateServerChain

**源码：** `cmd/kube-apiserver/app/server.go` `CreateServerChain` +0（:189）
**职责：** 按委托顺序构造三个 server。
**参数：** `config CompletedConfig`。
**返回值：** `(*aggregatorapiserver.APIAggregator, error)`。
**执行流程：**
```text
CreateServerChain (+0)
├── 1. notFoundHandler（+1）
├── 2. APIExtensionsServer.New(空委托 + NotFound)（+2）
├── 3. kubeAPIServer.New(apiExtensions 作为 delegate)（+8）
└── 4. CreateAggregatorServer(kubeAPIServer 作为 delegate)（+14）
```
**委托语义：** 请求先到最外层 aggregator；未命中（非聚合 APIService）则委托给 kubeAPIServer；再未命中委托给 apiextensions（CRD）；最终未命中走 NotFound。
**被调用函数：** `config.ApiExtensions.New`、`config.KubeAPIs.New`、`controlplaneapiserver.CreateAggregatorServer`。

### 函数：Config.New（controlplane 构造）

**源码：** `pkg/controlplane/apiserver/server.go` `completedConfig.New` +0（:93）
**职责：** 用 genericConfig 构造 KubeAPIServer 的 `*Server`；**本身不安装 API**，安装由 `(*Server).InstallAPIs` 完成。
**参数：** `name string`、`delegationTarget genericapiserver.DelegationTarget`。
**返回值：** `(*Server, error)`。
**执行流程：**
1. `c.Generic.New(name, delegationTarget)`（+3）：构造通用 GenericAPIServer
2. 安装 `routes.Logs`（+10）或 `routes.NewOpenIDMetadataServer`（+35）
3. 构造 `&Server{...}`（+38 起），挂 GenericAPIServer、APIResourceConfigSource、RESTOptionsGetter 等
4. 注册一组 PostStartHook（system-namespaces、coordinated-leader-election、peer-endpoint、cluster-authentication-info、apiserver-identity-lease、storage-readiness、legacy-token-tracking 等，+51 ～ +209）
**调用点（安装 API 的真实入口）：** `pkg/controlplane/instance.go:345` 处 `controlplane.CompletedConfig.New`（:321）调用 `s.ControlPlane.InstallAPIs(restStorageProviders...)`，即 `Server.InstallAPIs`。

### 函数：(*Server).InstallAPIs

**源码：** `pkg/controlplane/apiserver/apis.go` `(*Server).InstallAPIs` +0（:88）
**语义修正：** 由包级函数改为**方法** `(s *Server) InstallAPIs(restStorageProviders ...RESTStorageProvider) error`。
**参数：** 变长 `RESTStorageProvider`（含 core legacy、internal、authentication、authorization、certificates、coordination、rbac、svm、flowcontrol、admissionregistration、events）。
**返回值：** `error`。
**执行流程：**
1. 初始化 `nonLegacy []*APIGroupInfo`（+1）与 resourceExpirationEvaluator（+5 ～ +12）
2. 循环每个 provider（+14）：`groupName := provider.GroupName()`（+15）→ `provider.NewRESTStorage(...)` 产出 `APIGroupInfo`（+17）
3. 空存储的组视为禁用并跳过（+20 ～ +23）；`RemoveUnavailableKinds` 剔除未到生命周期的资源（+27 起）
4. 若 provider 实现 `PostStartHookProvider`，注册其 hook（+38 起）
5. **组名空 = legacy（core，前缀 `/api`）**：走 `GenericAPIServer.InstallLegacyAPIGroup("/api", &apiGroupInfo)`（+52）；否则（有 group 名，走 `/apis`）append 到 `nonLegacy`（+60）
6. 最后 `GenericAPIServer.InstallAPIGroups(nonLegacy...)`（+70）
**关键点：** legacy 与 `/apis` 组在此分叉；`InstallLegacyAPIGroup` 只装 `/api` 前缀，`InstallAPIGroups` 装 `/apis` 下全部组。

### 函数：(*legacyProvider).NewRESTStorage

**源码：** `pkg/registry/core/rest/storage_core.go` `(*legacyProvider).NewRESTStorage` +0（:140）
**职责：** 构造核心 v1 全部资源的 RESTStorage。
**参数：** `apiResourceConfigSource serverstorage.APIResourceConfigSource`、`restOptionsGetter generic.RESTOptionsGetter`。
**返回值：** `(genericapiserver.APIGroupInfo, error)`。
**执行流程：**
1. 先调 `p.GenericConfig.NewRESTStorage(...)` 产出基础 `apiGroupInfo`（+6 ～ +10），再在其上叠加核心组专属资源
2. 逐资源构造 storage：podTemplate（+19）、limitRange（+24）、persistentVolume（+30）、node（+42）、**pod（+47 `podstore.NewStorage`）**、service（+64）
3. 取 `apiGroupInfo.VersionedResourcesStorageMap["v1"]`（+76，无则建空 map）
4. serviceAccount 特殊处理（+87 ～ +95）
5. **pods 分支**（+97）：`storage["pods"] = podStorage.Pod`；子资源 map（+100 ～ +109）：attach/status/log/exec/portforward/proxy/binding/ephemeralcontainers/resize，条件加 eviction/dynamic；循环写入 `storage[resource+"/"+subresource]`（+117 ～ +121）
6. 其余核心资源（bindings、podtemplates、replicationcontrollers、serviceaccounts 等）各自写入 map（+123 起）
**关键动作：** PodStorage 的 `Pod` 放入 `VersionedResourcesStorageMap["v1"]["pods"]`；binding/status/eviction/log/exec 等子资源各自占 map 一项（`pods/<sub>`）。

### 函数：DefaultBuildHandlerChain

**源码：** `[staging] apiserver/pkg/server/config.go` `DefaultBuildHandlerChain` +0（:1078）
**职责：** 返回包裹在 director 之外的完整中间件链。
**参数：** `apiHandler http.Handler`（director）、`c *Config`。
**返回值：** `http.Handler`。
**实际经过顺序（请求先进入最后包裹的 filter，即请求执行顺序，外 → 内）：**
```text
WithAuditInit（最外）
 ↓
WithPanicRecovery → WithMuxAndDiscoveryComplete → [WithEndpointLoadMetrics]
 ↓
WithRequestReceivedTimestamp → WithRequestInfo → [WithRoutine]
 ↓
WithLatencyTrackers → WithHTTPLogging → [WithRetryAfter]
 ↓
WithHSTS → WithCacheControl → [WithProbabilisticGoaway] → [WithWatchTerminationDuringShutdown]
 ↓
WithWaitGroup → WithRequestDeadline → WithTimeoutForNonLongRunningRequests
 ↓
WithWarningRecorder → WithCORS
 ↓
WithAuthentication（失败走 failedHandler = Unauthorized + WithFailedAuthenticationAudit，+46 ～ +52）
 ↓
WithTracing → WithAudit
 ↓
[WithConstrainedImpersonation 默认]（ConstrainedImpersonation Beta 默认开；否则 WithImpersonation，+34 ～ +40）
 ↓
[WithPriorityAndFairness]（c.FlowControl != nil；否则 WithMaxInFlightLimit，+22 ～ +31）
 ↓
WithAuthorization（或 ConditionalAuthorization 开启时 WithConditionsAwareAuthorization，+4 ～ +15）
 ↓
TrackCompleted（最内） → director
```
**关键分支（证据必用）：**
- **授权分支（:1082-1092）：** `ConditionalAuthorization`（Alpha 1.38 Default:false）开启 **且** `ConditionsEnforcerPluginEnabled` 时用 `WithConditionsAwareAuthorization`；否则默认走 `WithAuthorization`。ConditionalAuthorization 开启但 plugin 未启用时打 Warning（:1094-1096）。
- **P&F / MaxInFlight（:1100-1109）：** `c.FlowControl != nil` 时用 `WithPriorityAndFairness`（P&F，位于授权**之后**、冒充**之前**）；否则 `WithMaxInFlightLimit`。
- **冒充分支（:1112-1118）：** `ConstrainedImpersonation`（Beta 1.36 Default:true）默认**开启**，走 `WithConstrainedImpersonation`；关闭时才走旧的 `WithImpersonation`。**默认路径已从 WithImpersonation 切换为 WithConstrainedImpersonation。**
- **WithRoutine（:1164-1166）：** `APIServingWithRoutine`（Alpha 1.30 Default:false）默认**不包**。
- **条件包装：** `WithEndpointLoadMetrics`（LoadReporter != nil，:1169-1171）、`WithWatchTerminationDuringShutdown`（GracePeriod > 0，:1148-1150）、`WithProbabilisticGoaway`（SecureServing != nil && !DisableHTTP2 && GoawayChance > 0，:1151-1153）、`WithRetryAfter`（ShutdownSendRetryAfter，:1156-1158）。
**设计意图（源码可证）：** `filterlatency.TrackStarted/TrackCompleted` 成对包裹每个 filter 以分别记录排队与执行延迟（:1081、:1098、:1104-1106、:1111、:1120、:1130-1131 等）；WithTimeout 把后续处理放进 goroutine，使长请求超时后仍能继续收尾（:1141-1142 注释）。

### 4.3b 认证 / 鉴权 filter 深度拆解

DefaultBuildHandlerChain 只搭好 filter 顺序；本节下钻到「一次请求如何被认证成 `user.Info`」和「鉴权器如何做出 Allow/Deny 决策」的函数级细节。两个 filter 的实现都在 `[staging] apiserver/pkg/endpoints/filters/`。

#### 认证：WithAuthentication

**源码：** `[staging] apiserver/pkg/endpoints/filters/authentication.go`
- `WithAuthentication`（偏移：+46）：外层，`auth == nil`（认证禁用）时**直接透传**不认证（withAuthentication +4 ～ +6 打 Warning）。
- `withAuthentication`（偏移：+51）为实际逻辑，`http.HandlerFunc` 闭包内（以下偏移均相对 `withAuthentication` 定义行 +0）：
  1. `apiAuds` 非空则 `req.WithContext(authenticator.WithAudiences(...))`（偏移：+15）注入期望 audience。
  2. **`auth.AuthenticateRequest(req)`**（偏移：+17）：调用认证器链（client-go authenticator）——按认证方式解析出 `resp`（含 `resp.User`、`resp.Audiences`）、`ok`、`err`；计时 `TrackAuthenticationLatency`。
  3. `err != nil || !ok`（偏移：+23）：认证失败走 `failed.ServeHTTP`（偏移：+27；= `Unauthorized` +128，401，带 `WithFailedAuthenticationAudit`），**短路返回**。
  4. `audiencesAreAcceptable`（偏移：+31 / 定义 +153）：校验响应 audience 是否被 apiserver 接受；不匹配则 401（failed.ServeHTTP 偏移：+34）。
  5. **认证成功后清理敏感头**：`req.Header.Del("Authorization")`（偏移：+39，凭证用后即弃）；`headerrequest.ClearAuthenticationHeaders`（偏移：+42）删除标准 front-proxy 头（`X-Remote-User/Uid/Group/Extra-`）及自定义 `requestHeaderConfig` 头（偏移：+52），防止伪造冒充。
  6. **HTTP/2 DoS 缓解**（偏移：+66 ～ +72，CVE-2023-44487 / CVE-2023-39325）：feature gate `UnauthenticatedHTTP2DOSMitigation` 开启且 HTTP/2 且匿名用户时，置 `Connection: close` 降级连接。
  7. **`req = req.WithContext(genericapirequest.WithUser(...))`**（偏移：+72）：把 `resp.User`（`user.Info`）写入 request context，供下游鉴权/审计使用 → 放行 `handler.ServeHTTP`。
- 匿名判定 `isAnonymousUser`（+161）；`Unauthorized`（+128）用 `NegotiatedSerializer` 产出 401 响应体。

#### 鉴权：WithAuthorization

**源码：** `[staging] apiserver/pkg/endpoints/filters/authorization.go`
- `WithAuthorization`（偏移：+61）：外层；`a == nil`（鉴权禁用）时直接透传（+4 ～ +6 打 Warning）。注释强调**不应被同一请求执行两次**。
- `withAuthorization`（偏移：+66）为实际逻辑：
  1. `liftedAuthorizer := authorizer.AuthorizerFunc(a.Authorize)`（偏移：+5）把无条件授权器提升为标准 `Authorizer`。
  2. **`GetAuthorizerAttributes(ctx)`**（偏移：+10 / 定义 +225）：从 request context 取出鉴权属性 `authorizer.Attributes`（verb、resource、subresource、namespace、name、user 等，由上游 RequestInfo/认证阶段填充）。
  3. **`a.Authorize(ctx, attributes)`**（偏移：+15）：授权器做出决策，返回 `authorized`（`DecisionAllow`/`DecisionDeny`/`DecisionNoOpinion`）、`reason`、`err`；`metricsResultLabel`（+17）；计时 `TrackAuthorizationLatency`（+22）。
  4. **决策先于错误判定**（偏移：+26 注释，关键）：RBAC 之类授权器可能在评估出错的同时仍给出 Allow，故**先看 `authorized == DecisionAllow`**（偏移：+26）——允许则 `audit.AddAuditAnnotations`（偏移：+27，写 `DecisionAllow` 与 reason）并放行；同时在 context 里登记 Allow 决策（偏移：+38，`WithConditionallyAuthorizedDecision`，兼容条件授权场景）。
  5. 非 Allow 走 forbidden 分支（偏移：+59，`responsewriters.Forbidden`），返回 403 及 `reason`。
  5. 非 Allow 则走 forbidden 分支（`responsewriters.Forbidden`），返回 403 及 `reason`。

#### 条件感知鉴权：WithConditionsAwareAuthorization

**源码：** `[staging] apiserver/pkg/endpoints/filters/authorization.go`
- `WithConditionsAwareAuthorization`（偏移：+135）/`withConditionsAwareAuthorization`（+143）：`ConditionalAuthorization`（Alpha 1.38 默认关）+ `ConditionsEnforcerPluginEnabled` 同时满足时启用，配合 `AuthorizationConditionsEnforcer` admission 插件防护条件场景下的 context detach（见 `withAuthorization` +100 注释）。

#### 两个 filter 的协作与放行

| 维度 | 认证 WithAuthentication | 鉴权 WithAuthorization |
|---|---|---|
| 产出 | `resp.User`（`user.Info`）写入 context | `authorizer.Attributes` → Allow/Deny 决策 |
| 禁用时 | 直接透传（不认证） | 直接透传（不鉴权） |
| 失败表现 | 401（`Unauthorized`），短路 | 403（`Forbidden`），带 reason |
| 放行下游 | `handler.ServeHTTP` → tracing/audit/impersonation | → director → REST handler |
| 审计 | `WithFailedAuthenticationAudit` | `audit.AddAuditAnnotations`(DecisionAllow/reason) |

**设计要点（源码可证）：**
1. **认证先于鉴权、两者都禁用即透传**：`auth == nil` / `a == nil` 分别让认证/鉴权 filter 变成 no-op，便于纯代理或测试部署。
2. **凭证用后即弃 + front-proxy 头清理**：认证成功即删 `Authorization` 头并清所有 `X-Remote-*` 头，防止下游伪造身份（认证 filter 内偏移 +39 ～ +52）。
3. **决策先于错误**（鉴权 +93 注释）：允许 RBAC「评估出错但仍 Allow」，决策优先于 `err`，避免把可放行请求误判为 500。
4. **认证产物通过 context 传递**：`genericapirequest.WithUser`（认证偏移 +72）与 `genericapirequest.GetAuthorizerAttributes`（鉴权偏移 +10 / 定义 +225）是两者间唯一的耦合点——认证把身份塞进 context，鉴权从 context 取属性，二者解耦。

### 函数：NewAPIServerHandler

**源码：** `[staging] apiserver/pkg/server/handler.go` `NewAPIServerHandler` +0（:73）
**职责：** 组装 HTTP 入口四件套。
**参数：** `name string`、`s runtime.NegotiatedSerializer`、`handlerChainBuilder HandlerChainBuilderFn`、`notFoundHandler http.Handler`。
**返回值：** `*APIServerHandler`。
**执行流程：**
1. `mux.NewPathRecorderMux(name)` 建非 go-restful 路由（+1），可选挂 NotFound（+2 ～ +4）
2. `restful.NewContainer()`（+6）+ `CurlyRouter`（+7，支持 proxy 类通配路径），设置 panic 恢复（+8）与错误处理（+11）
3. 构造 director（+15 ～ +19）
4. `FullHandlerChain = handlerChainBuilder(director)`（+22，默认即 DefaultBuildHandlerChain）；返回 `&APIServerHandler{...}`（+21 ～ +26）

### 函数：director.ServeHTTP

**源码：** `[staging] apiserver/pkg/server/handler.go` `director.ServeHTTP` +0（:121）
**职责：** HTTP 分流。
**接收者：** `d director`（值接收者）。
**内部逻辑：**
1. `path := req.URL.Path`（+1）
2. 遍历 `RegisteredWebServices()`（+4）：`ws.RootPath() == "/apis"` 时（+6），`path == "/apis" || path == "/apis/"` 才交给 go-restful（+10 ～ +15）；否则若 `strings.HasPrefix(path, ws.RootPath())` 且满足边界匹配（`len==len(root)` 或下一字符为 `/`，+19 ～ +26），交给 go-restful（+24 ～ +25）
3. 未匹配 → `nonGoRestfulMux.ServeHTTP`（+31 ～ +32）
**存在原因（注释可证，:53-64）：** go-restful 的 `/apis` webservice 会强制对非 discovery 请求 404，破坏委托链；director 先自省路径再决定路由，解决 fallthrough。

### 4.3c 三层委托链请求流走读（第二层补充视角）

`CreateServerChain` 构造的三个 server 是**层层委托**关系：每层的 `NonGoRestfulMux` 未命中时回落到下一层。本节走一个请求，看它如何穿过这三层。

**委托链的构造顺序（`CreateServerChain`，:189）：**
```text
1. apiExtensionsServer = ApiExtensions.New(空 delegate + notFoundHandler)
2. kubeAPIServer       = KubeAPIs.New(apiExtensionsServer.GenericAPIServer)  ← delegate = 扩展层
3. aggregatorServer    = CreateAggregatorServer(..., kubeAPIServer.GenericAPIServer, ...)  ← delegate = 核心层
```
**关键：** 每层的 `NewHandler` 把 `nonGoRestfulMux.NotFoundHandler` 设为下一层的入口（handler.go:75-76）——即「本层 go-restful + NonGoRestfulMux 都不认的路径，交给 delegate」。对外唯一入口是**最外层的 aggregatorServer**。

**一个请求的三种落点：**

| 请求路径 | 第一跳（aggregator） | 第二跳（kubeAPIServer） | 第三跳（apiExtensions） |
|---|---|---|---|
| `/api/v1/namespaces/default/pods` | NonGoRestfulMux 未注册 `/api/v1/...`（聚合 API 才注册）→ 回落 delegate | go-restful 命中 `pods` → `Store.Create` → etcd | 不会到达 |
| `/apis/apps/v1/deployments` | 非 aggregated APIService → 回落 delegate | go-restful 命中 `deployments` | 不会到达 |
| `/apis/customresourcedefinitions...` | 未注册 → 回落 delegate | NonGoRestfulMux 未注册 → 回落 delegate | crdHandler 命中 `/apis` → 懒分发（见篇 17） |
| `/apis/<aggregated-group>/v1/...` | APIService 已注册 → proxyHandler 反向代理到后端 Service | 不会到达 | 不会到达 |

**走读一次 `/api/v1/pods` 请求穿过三层：**
```text
客户端 → aggregatorServer（最外层入口）
  ↓ FullHandlerChain（认证/鉴权/APF，见 §DefaultBuildHandlerChain）
  ↓ director.ServeHTTP：遍历 go-restful webservices，aggregator 只注册了 APIService 相关路径
  ↓ 非 aggregated 路径 → nonGoRestfulMux → NotFoundHandler = kubeAPIServer 入口
kubeAPIServer（第二层）
  ↓ FullHandlerChain（第二次认证/鉴权/APF）
  ↓ director.ServeHTTP：go-restful 命中 pods webservice → Store.Create → etcd
  ↓ （若是 CRD 路径）nonGoRestfulMux → NotFoundHandler = apiExtensionsServer 入口
apiExtensionsServer（第三层）
  ↓ crdHandler.ServeHTTP → 懒分发 → customresource NewStorage
```

**设计要点（源码可证）：**
1. **构造顺序与委托方向相反**：`CreateServerChain` 先建最内层（apiExtensions，delegate 为空+notFoundHandler），再建核心层（delegate=扩展层），最后建聚合层（delegate=核心层）——使聚合层成为对外唯一入口，未匹配时逐层回落。
2. **每层独立 FullHandlerChain**：请求穿过聚合层、核心层时会**各过一次**认证/鉴权/APF（每层有自己的 handler chain）——这是「聚合 API 请求被认证两次」的根因，也是 APF 并发席位可能被计两次的原因。
3. **director 解决 go-restful `/apis` 404 破坏委托链**：go-restful 的 `/apis` webservice 会对非 discovery 请求强制 404；director 先自省路径，只在确认 go-restful 能处理时才 `Dispatch`，否则交给 NonGoRestfulMux 回落 delegate（director.ServeHTTP 注释 :53-64）。
4. **CRD 走 NonGoRestfulMux 而非 go-restful**：CRD 不经 go-restful 路由，crdHandler 注册在 NonGoRestfulMux 的 `/apis` 前缀——这是「CR 实例与内置资源走不同路由机制」的根因（见篇 17）。

### 函数：(*GenericAPIServer).InstallLegacyAPIGroup / installAPIResources

**源码：** `[staging] apiserver/pkg/server/genericapiserver.go` `InstallLegacyAPIGroup` +0（:885）／`installAPIResources` +0（:820）
**职责：** 安装核心组（前缀 `/api`，无 group 名）；对每个 groupVersion 调 `APIGroupVersion.InstallREST`。
**执行流程（InstallLegacyAPIGroup，:885）：**
1. 前置校验 `legacyAPIGroupPrefixes.Has(apiPrefix)`（+1 ～ +3）
2. `getOpenAPIModels` 取 openapi 模型（+5 ～ +8）
3. `installAPIResources(apiPrefix, apiGroupInfo, openAPIModels)`（+10）
4. 安装版本 handler：`discovery.NewLegacyRootAPIHandler` + `WrapAggregatedDiscoveryToHandler`，`GoRestfulContainer.Add(...GenerateWebService("/api", ...))`（+16 ～ +19）
5. `registerStorageReadinessCheck`（+20）
**执行流程（installAPIResources，:820）：**
1. 循环 `PrioritizedVersions`（+2）：空版本跳过（+3 ～ +6）
2. `getAPIGroupVersion(...)`（+8）；设置 `TypeConverter`、`MaxRequestBodyBytes`（+15 ～ +16）
3. `apiGroupVersion.InstallREST(s.Handler.GoRestfulContainer)`（+18）——核心路由落地
4. 聚合 discovery：`apiPrefix == APIGroupPrefix`（/apis）走 `AggregatedDiscoveryGroupManager.AddGroupVersion`（+26 ～ +33），否则（legacy /api）走 `AggregatedLegacyDiscoveryGroupManager`（+35 ～ +43）
5. `RegisterDestroyFunc`（+49）；StorageVersionAPI 开启时注册 ResourceInfos（+51 ～ +57）
**被调用函数：** `APIGroupVersion.InstallREST`；`InstallAPIGroups`（:913）走同一 `installAPIResources`。

### 函数：(*APIGroupVersion).InstallREST

**源码：** `[staging] endpoints/groupversion.go` `(*APIGroupVersion).InstallREST` +0（:106）
**职责：** 单个 group/version 的路由落地。
**参数：** `container *restful.Container`。
**返回值：** `([]apidiscoveryv2.APIResourceDiscovery, []*storageversion.ResourceInfo, error)`。
**执行流程：**
1. 拼前缀：`path.Join(g.Root, g.GroupVersion.Group, g.GroupVersion.Version)`（+1，核心 v1 即 `/api/v1`）
2. 构造 `APIInstaller`（+3 ～ +7）
3. `installer.Install()`：遍历 Storage map 产出 resources、resourceInfos、go-restful WebService（+8）
4. 挂版本级 discovery handler：`NewAPIVersionHandler` + `AddToWebService`（+9 ～ +10）
5. `container.Add(ws)` 注册到 GoRestfulContainer（+11）
6. `ConvertGroupVersionIntoToDiscovery` 产出聚合 discovery（+12 ～ +15）
**错误处理：** 注册错误聚合返回（`utilerrors.NewAggregate`，+16）；单个资源失败不影响已注册部分。

### 函数：(*APIInstaller).registerResourceHandlers

**源码：** `[staging] endpoints/installer.go` `(*APIInstaller).registerResourceHandlers` +0（:288）
**职责：** 为单个资源生成全部动作（GET/POST/PUT/PATCH/DELETE 及子资源）的路由。
**参数：** `path string`、`storage rest.Storage`、`ws *restful.WebService`。
**返回值：** `(*metav1.APIResource, *storageversion.ResourceInfo, error)`。
**关键机制：**
- 函数开头对 storage 做能力接口断言（+50 起，:338 `creater, isCreater := storage.(rest.Creater)`；同时断言 namedCreater/getter/lister/updater/patcher/gracefulDeleter/collectionDeleter/watcher/connecter 等，:338-351）。
- `isNamedCreater` 时把 `isCreater` 置 true（+68 ～ +70）。
- **POST 分支（:923，case request.MethodPost）：** named creater 用 `restfulCreateNamedResource(namedCreater, reqScope, admit)`（:926），否则 `restfulCreateResource(creater, reqScope, admit)`（:928，**包级函数调用**）。
- handler 先被 `metrics.InstrumentRouteFunc` 包指标（:930），再 `AddWarningsHandler`（:931）。
- `ws.POST(action.Path).To(handler)...`（:937 ～ :948）注册路由、文档、出入参类型。
**数据流：** rest.Storage 能力 → handler 闭包 → go-restful Route。

### 函数：restfulCreateResource

**源码：** `[staging] endpoints/installer.go` `restfulCreateResource` +0（:1307）
**语义修正：** 由**方法**改为**包级函数** `func restfulCreateResource(r rest.Creater, scope handlers.RequestScope, admit admission.Interface) restful.RouteFunction`。
**职责：** 适配函数：返回的 RouteFunction 直接调用 `handlers.CreateResource(r, &scope, admit)`（+1 ～ +3，请求处理细节在下一篇 06）。
**调用点：** `installer.go:928`（POST 分支），非 namedCreater 路径；namedCreater 走 `restfulCreateNamedResource`（:1301）。

---

## 5. 路由生成总览

```text
NewConfig → Complete
 ↓
CreateServerChain
 └── KubeAPIServer.New（controlplane/apiserver server.go:93）
      └── controlplane.CompletedConfig.New（instance.go:321）→ (*Server).InstallAPIs（apis.go:88）
           ├── legacyProvider.NewRESTStorage（storage_core.go:140，PodStorage：pod + 子资源）
           │    ↓ APIGroupInfo
           ├── legacy 组（group 名空）→ InstallLegacyAPIGroup（/api）→ installAPIResources
           └── 其余组 → InstallAPIGroups（/apis）→ installAPIResources
                └── APIGroupVersion.InstallREST（groupversion.go:106）
                     └── APIInstaller.Install → registerResourceHandlers（installer.go:288）
                          ├── 断言 rest.Creater 等能力接口（installer.go:338 起）
                          └── ws.POST(...).To(restfulCreateResource → handlers.CreateResource，installer.go:1307/928）
```
请求运行时方向：SecureServing → FullHandlerChain（filters）→ director → GoRestfulContainer → CreateResource。

---

## 6. 设计意图与验证

### 6.1 设计意图（源码可证）

| 设计点 | 证据 |
|---|---|
| 委托链解耦 | 三个 server 以 delegate 链串接，每层只负责自己认领的路径，未命中向下委托；director 解决 go-restful `/apis` webservice 强制 404 破坏 fallthrough 的问题（handler.go:53-64 注释） |
| 能力接口驱动路由 | `registerResourceHandlers` 对 `rest.Storage` 做能力断言，能力决定动词，storage 与路由生成解耦 |
| 中间件顺序固定 | 认证 → 授权 → 限流/冒充 → audit 等顺序经 `filterlatency` 成对包裹，保证排队与执行延迟可分别度量 |
| legacy /api 与 /apis 分流 | `InstallAPIs` 按组名是否为空分叉：空走 `InstallLegacyAPIGroup`，非空走 `InstallAPIGroups`，二者最终都进 `installAPIResources` |
| 条件 feature 包装 | WithRoutine、WithConditionsAwareAuthorization、WithConstrainedImpersonation 等均为 feature gate 控制，默认路径已收敛（见下） |

### 6.2 中间件默认路径变化（相对旧版）

| 变化 | 说明 | 锚点 |
|---|---|---|
| **冒充默认路径切换** | `ConstrainedImpersonation`（Beta 1.36 Default:true）默认开 → 默认走 `WithConstrainedImpersonation`，**不再走 `WithImpersonation`** | config.go:1112-1118；kube_features.go:428-431 |
| **ConditionsAwareAuthorization 新增** | `ConditionalAuthorization`（Alpha 1.38 Default:false）默认关；需 feature + `ConditionsEnforcerPluginEnabled` 才包 `WithConditionsAwareAuthorization`，否则默认 `WithAuthorization` | config.go:1082-1092；kube_features.go:416-418 |
| **P&F 位置** | `WithPriorityAndFairness`（c.FlowControl != nil）在授权**之后**、冒充**之前**；否则 `WithMaxInFlightLimit` | config.go:1100-1109 |
| **WithRoutine** | `APIServingWithRoutine`（Alpha 1.30 Default:false）默认不包 | config.go:1164-1166；kube_features.go:390-392 |

### 6.3 语义修正（相对旧版）

| 项 | 旧版 | 当前 |
|---|---|---|
| `InstallAPIs` | 包级函数 | **方法** `(s *Server).InstallAPIs`（apis.go:88） |
| `restfulCreateResource` | 方法 | **包级函数**（installer.go:1307），调用点 :928 |
| `Config.New` | `config.go` | `completedConfig.New`（server.go:93）；API 安装入口在 instance.go:345 |

### 6.4 行号漂移（相对旧文）

| 符号 | 旧文 | 当前 | 漂移 |
|---|---|---|---|
| main | :32 | :32 | 0 |
| NewAPIServerCommand | :71 | :74 | +3 |
| Run | :149 | :152 | +3 |
| CreateServerChain | :185 | :189 | +4 |
| Config struct | — | :70 | — |
| InstallAPIs | — | :88 | — |
| NewRESTStorage | :140 | :140 | 0 |
| DefaultBuildHandlerChain | :1036 | :1078 | +42 |
| NewAPIServerHandler | :73 | :73 | 0 |
| director 类型 | :115 | :115 | 0 |
| director.ServeHTTP | :120 | :121 | +1 |
| installAPIResources | :794 | :820 | +26 |
| InstallLegacyAPIGroup | :859 | :885 | +26 |
| InstallREST | :106 | :106 | 0 |
| registerResourceHandlers | :288 | :288 | 0 |
| creater 断言 | :338 | :338 | 0 |
| POST 分支 | :923 | :923 | 0 |
| InstrumentRouteFunc | :930 | :930 | 0 |
| ws.POST.To | :936~:948 | :937-948 | +1 |
| restfulCreateResource | :1307 | :1307 | 0 |

### 6.5 验证与未确认

- **已逐点回源码确认**（HEAD 8893923938107170c86eb4b8cc2b7225b4e44e9b）：全部锚点与语义修正均与证据一致，无符号消失。
- **未确认项：** 无。所有标注均为源码可证或证据已判定；未标注 `[源码未确认]` 项。

---

[上一篇](04-kubectl客户端.md) · [总目录](README.md) · [下一篇](06-apiserver请求处理与etcd.md)
