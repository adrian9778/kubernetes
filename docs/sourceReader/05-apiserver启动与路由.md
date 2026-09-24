[上一篇](04-kubectl客户端.md) · [总目录](README.md) · [下一篇](06-apiserver请求处理与etcd.md)

# apiserver 启动与路由
> **层级**：第四层——函数级源码解析。
> **范围**：进程启动、三层委托 server 构造、handler 中间件链、HTTP 分流（director）、RESTStorage 到 go-restful 路由的生成。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口模块 | `cmd/kube-apiserver`（main）、`cmd/kube-apiserver/app`（命令与装配） |
| K8s 控制面装配 | `pkg/controlplane/apiserver`（KubeAPIServer 的 Config/New）、`pkg/registry/core/rest`（核心组 RESTStorage 构造）、`pkg/kubeapiserver` |
| 通用 API 框架 | `[staging] k8s.io/apiserver/pkg/server`（GenericAPIServer、Config、handler 链）、`.../endpoints`（路由安装）、`.../registry/generic` |
| 模块对外接口 | HTTPS REST API；内部以 `APIGroupInfo`（版本→`map[string]rest.Storage`）描述资源 |
| 主数据流 | options → Config → 三个 GenericAPIServer（委托链）→ 安装 APIGroup → 生成路由 → 启动 SecureServing |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Config`（controlplane） | `pkg/controlplane/apiserver/config.go` | KubeAPIServer 的构造配置，内嵌 genericConfig |
| `GenericAPIServer` | `[staging] apiserver/pkg/server/genericapiserver.go` | 通用 API server：持有 Handler、APIGroup 安装、委托目标、discovery |
| `APIGroupInfo` | `[staging] apiserver/pkg/server/genericapiserver.go` | 一个 group 的版本→RESTStorage map 与序列化协商 |
| `APIGroupVersion` | `[staging] endpoints/groupversion.go` | 单个 group/version 的安装输入（Storage map、Creater、converter、根前缀） |
| `APIInstaller` | `[staging] endpoints/installer.go` | 把每个 rest.Storage 的能力接口翻译成一组 go-restful 路由 |
| `APIServerHandler` | `[staging] apiserver/pkg/server/handler.go` | FullHandlerChain + GoRestfulContainer + NonGoRestfulMux + Director 的聚合体 |
| `director` | handler.go:115 | 按已注册 webservice 判断，把请求分给 go-restful 或 NonGoRestfulMux |
| `PodStorage` | `pkg/registry/core/pod/storage/storage.go` | Pod 主资源与全部子资源的 RESTStorage 集合 |

## 3. 函数级

### 函数：main
**源码：** `cmd/kube-apiserver/apiserver.go`
**执行流程：** `app.NewAPIServerCommand()` → `cli.Run(command)`。

### 函数：NewAPIServerCommand
**源码：** `cmd/kube-apiserver/app/server.go:71`
**职责：** 从默认 options 构造 cobra 命令，注册 flags，RunE 调 `Run(ctx, opts)`。

### 函数：Run
**源码：** `cmd/kube-apiserver/app/server.go:149`
**职责：** 端到端启动序列。
**参数：** `ctx context.Context`、`opts options.CompletedOptions`。
**返回值：** `error`。
**执行流程：**
1. 打印版本与 Go 运行参数（+1 ～ +3）
2. 建 informer name（+6 ～ +11）
3. `NewConfig(opts)`：解析选项为各 server 的 Config（+13 ～ +15）
4. `config.Complete()`：补全默认值与派生配置（+16 ～ +18）
5. `CreateServerChain(completed)`：构造委托 server 链（+19 ～ +21）
6. `server.PrepareRun()`：安装非资源 handler、准备探针与生命周期信号（+23 ～ +25）
7. `prepared.Run(ctx)`：启动 SecureServing 并阻塞（+29）
**状态变化：** 从纯配置过渡到监听端口的服务进程；Run 阻塞直到 shutdown。
**错误处理：** 任一步失败立即返回，无部分启动回滚（各 server 尚未起 goroutine）。

### 函数：CreateServerChain
**源码：** `cmd/kube-apiserver/app/server.go:185`
**职责：** 按委托顺序构造三个 server。
**执行流程：**
```text
CreateServerChain (+0)
├── 1. notFoundHandler（+2）
├── 2. APIExtensionsServer.New(空委托 + NotFound)（+3 ～ +5）
├── 3. kubeAPIServer.New(apiExtensions 作为 delegate)（+10 ～ +12）
└── 4. CreateAggregatorServer(kubeAPIServer 作为 delegate)（+14 ～ +15）
```
**委托语义：** 请求先到最外层 aggregator；未命中（非聚合 APIService）则委托给 kubeAPIServer；再未命中委托给 apiextensions（CRD）；最终未命中走 NotFound。
**被调用函数：** `config.ApiExtensions.New`、`config.KubeAPIs.New`、`controlplaneapiserver.CreateAggregatorServer`。

### 函数：Config.New（controlplane）
**源码：** `pkg/controlplane/apiserver/config.go`
**职责：** 用 genericConfig 构造 KubeAPIServer，安装核心 API（InstallAPIs，见 `pkg/controlplane/apiserver/apis.go`）。
**关键动作：** `restStorageBuilder.NewRESTStorage(...)` 产出核心组 `APIGroupInfo`，随后 `GenericAPIServer.InstallLegacyAPIGroup("/api", &apiGroupInfo)`。

### 函数：(*legacyProvider).NewRESTStorage
**源码：** `pkg/registry/core/rest/storage_core.go`
**职责：** 构造核心 v1 全部资源的 RESTStorage。
**关键动作：** 调 `podstore.NewStorage(...)`，将 `podStorage.Pod` 放入 `VersionedResourcesStorageMap["pods"]`；binding/status/eviction/log/exec 等子资源各自占 map 一项。

### 函数：DefaultBuildHandlerChain
**源码：** `[staging] apiserver/pkg/server/config.go:1036`
**职责：** 返回包裹在 director 之外的完整中间件链。
**参数：** `apiHandler http.Handler`（director）、`c *Config`。
**返回值：** `http.Handler`。
**实际经过顺序（请求先进入最后包裹的 filter）：**
```text
AuditInit（最外）
 ↓
PanicRecovery → MuxAndDiscoveryComplete → RequestReceivedTimestamp → RequestInfo
 ↓
(可选)WithRoutine
 ↓
LatencyTrackers → HTTPLogging → (可选)RetryAfter → HSTS → CacheControl
 ↓
(可选)Goaway → (可选)WatchTermination → WithWaitGroup
 ↓
RequestDeadline → TimeoutForNonLongRunning
 ↓
WarningRecorder → CORS
 ↓
WithAuthentication（失败走 Unauthorized + FailedAuth Audit）
 ↓
WithTracing → WithAudit → WithImpersonation（或 Constrained）
 ↓
PriorityAndFairness（或 MaxInFlightLimit）
 ↓
WithAuthorization（最内，紧邻 director）
 ↓
director
```
**关键分支：** FlowControl 配置存在时用 P&F（+7 ～ +14），否则用简单并发数限制（+15）；ConstrainedImpersonation feature gate 切换 impersonation 实现（+19 ～ +25）。
**设计意图（源码可证）：** `filterlatency.TrackStarted/TrackCompleted` 成对包裹每个 filter 以分别记录排队与执行延迟；WithTimeout 把后续处理放进 goroutine，使长请求超时后仍能继续收尾（+45 ～ +47 注释）。

### 函数：NewAPIServerHandler
**源码：** `[staging] apiserver/pkg/server/handler.go:73`
**职责：** 组装 HTTP 入口四件套。
**执行流程：**
1. `mux.NewPathRecorderMux(name)` 建非 go-restful 路由，挂 NotFound（+1 ～ +4）
2. `restful.NewContainer()` + CurlyRouter（支持 proxy 类通配路径），设置 panic 恢复与错误处理（+6 ～ +12）
3. 构造 director（+15 ～ +18）
4. `FullHandlerChain = handlerChainBuilder(director)`（+22）——默认即 DefaultBuildHandlerChain

### 函数：director.ServeHTTP
**源码：** `[staging] apiserver/pkg/server/handler.go:120`
**职责：** HTTP 分流。
**内部逻辑：** 遍历已注册 webservice，判断路径是否由 go-restful 认领（`/apis` discovery 有特殊处理）；可能被认领则走 `goRestfulContainer`，否则走 `nonGoRestfulMux`。
**存在原因（注释可证）：** go-restful 的 `/apis` webservice 会强制对非 discovery 请求 404，破坏委托链；director 先自省路径再决定路由，解决 fallthrough。

### 函数：(*GenericAPIServer).InstallLegacyAPIGroup / installAPIResources
**源码：** `[staging] apiserver/pkg/server/genericapiserver.go:859 / :794`
**职责：** 安装核心组（前缀 `/api`，无 group 名）；对每个 groupVersion 调 `APIGroupVersion.InstallREST`。

### 函数：(*APIGroupVersion).InstallREST
**源码：** `[staging] endpoints/groupversion.go:106`
**职责：** 单个 group/version 的路由落地。
**执行流程：**
1. 拼前缀：`path.Join(g.Root, group, version)`（+1，核心 v1 即 `/api/v1`）
2. 构造 `APIInstaller`（+3 ～ +7）
3. `installer.Install()`：遍历 Storage map 产出 resources、resourceInfos、go-restful WebService（+8）
4. 挂版本级 discovery handler（+9 ～ +10）
5. `container.Add(ws)` 注册到 GoRestfulContainer（+11）
**错误处理：** 注册错误聚合返回；单个资源失败不影响已注册部分（聚合错误供启动日志暴露）。

### 函数：(*APIInstaller).registerResourceHandlers
**源码：** `[staging] endpoints/installer.go:288`
**职责：** 为单个资源生成全部动作（GET/POST/PUT/PATCH/DELETE 及子资源）的路由。
**关键机制：**
- 函数开头对 storage 做能力接口断言（+50 起，:338 `creater, isCreater := storage.(rest.Creater)`；同时断言 getter/lister/updater/patcher/gracefulDeleter 等）。
- 根据动作列表循环，POST 分支（:923）：named creater 用 `restfulCreateNamedResource`，否则 `restfulCreateResource(creater, reqScope, admit)`（:927 ～ +929）。
- handler 先被 `metrics.InstrumentRouteFunc` 包指标（:930），再 `AddWarningsHandler`（:931）。
- `ws.POST(action.Path).To(handler)...`（:936 ～ +948）注册路由、文档、出入参类型。
**数据流：** rest.Storage 能力 → handler 闭包 → go-restful Route。

### 函数：restfulCreateResource
**源码：** `[staging] endpoints/installer.go:1307`
**职责：** 适配函数：返回的 RouteFunction 直接调用 `handlers.CreateResource(creater, scope.RequestScope, admit)`（请求处理细节在下一篇 06）。

## 4. 路由生成总览
```text
NewConfig → Complete
 ↓
KubeAPIServer.New
 └── legacyProvider.NewRESTStorage（PodStorage：pod + 子资源）
      ↓ APIGroupInfo
InstallLegacyAPIGroup → installAPIResources
 └── APIGroupVersion.InstallREST
      └── APIInstaller.Install → registerResourceHandlers
           ├── 断言 rest.Creater 等能力接口
           └── ws.POST(...).To(restfulCreateResource → handlers.CreateResource)
```
请求运行时方向：SecureServing → FullHandlerChain（filters）→ director → GoRestfulContainer → CreateResource。

[上一篇](04-kubectl客户端.md) · [总目录](README.md) · [下一篇](06-apiserver请求处理与etcd.md)
