# 证据：Kubernetes API Server — 请求处理中间件链 + 认证/鉴权/Admission/审计/Impersonation

- 仓库：`/Users/vincent/workspace/12_GO/src/github.com/kubernetes/kubernetes`
- 基线：**`3596eb69d25`**（2026-11-26 按 source-doc-refresh 从 `b5bcafc8ce9` 刷新：跨 166 提交/210 文件，逐条回核全部绝对行锚点并修正 24 处漂移，0 符号消失；行号以 `3596eb69d25` 工作区为准）
- 记法：本篇锚点采用「文件路径:符号名」+ 函数内偏移 `+N`（`+0`=签名行），按 source-doc-refresh §2 / source-doc-4layer 事实红线不钉绝对行号（行号随重构漂移）。需要当前行号时按符号 `grep -n` 或用 `sr_tools.py offset` 现场取。
- 路径校正（⚠️ 任务描述与实际不符）：
  - 任务写 `pkg/server/filters/audit.go`、`pkg/server/filters/impersonation.go` → 实际在 **`staging/src/k8s.io/apiserver/pkg/endpoints/filters/`**（`audit.go`、`impersonation/impersonation.go`）；`pkg/server/filters/` 下只有 cors/goaway/hsts/longrunning/maxinflight/priority-and-fairness/timeout/waitgroup/watch_termination/with_retry_after/wrap。
  - 任务写 RBAC 在 `plugin/pkg/authorizer/rbac/` → 实际为 **`plugin/pkg/auth/authorizer/rbac/`**。

---

## 1. Handler chain 装配

### 1.1 入口与 wrap 顺序

`DefaultBuildHandlerChain` — `staging/src/k8s.io/apiserver/pkg/server/config.go:DefaultBuildHandlerChain`
`NewBaseHandlerChain` — 该符号在当前 HEAD **不存在**（grep 全仓无命中）；chain 构建的另两个入口是
`BuildHandlerChainWithStorageVersionPrecondition` — `config.go:BuildHandlerChainWithStorageVersionPrecondition`（先套 `WithStorageVersionPrecondition`，再调 `DefaultBuildHandlerChain`）。
`CompletedConfig.New` 实际是 `New(name, delegationTarget, ...)` 内的 wiring，见 1.3。

**完整 wrap 顺序（外→内，即代码中自上而下的赋值顺序；后赋值=更外层）**，来自 `config.go:DefaultBuildHandlerChain(+1)` 起逐行：

1. `filterlatency.TrackCompleted(handler)` — `config.go:+2`
2. 条件分支：feature gate `ConditionalAuthorization` 且 `ConditionalAuthorizationRequestClassifier != nil` 且 `ConditionsEnforcerPluginEnabled` →
   `genericapifilters.WithConditionsAwareAuthorization(handler, c.Authorization.Authorizer, c.Serializer, classifier)` — `config.go:+4~+6`
   否则 → `genericapifilters.WithAuthorization(handler, c.Authorization.Authorizer, c.Serializer)` — `config.go:WithAuthorization 调用点`
3. `filterlatency.TrackStarted(handler, c.TracerProvider, "authorization")` — `config.go:+24`
4. FlowControl 非空分支：
   - `flowcontrolrequest.NewWorkEstimator(...)` — `config.go:+29~+31`
   - `genericfilters.WithPriorityAndFairness(handler, c.LongRunningFunc, c.FlowControl, requestWorkEstimator, c.RequestTimeout/4)` — `config.go:WithPriorityAndFairness 调用点`
   - 否则 `genericfilters.WithMaxInFlightLimit(handler, c.MaxRequestsInFlight, c.MaxMutatingRequestsInFlight, c.LongRunningFunc)` — `config.go:WithMaxInFlightLimit 调用点`
5. Impersonation 分支：feature gate `ConstrainedImpersonation` →
   `impersonation.WithConstrainedImpersonation(...)` / 否则 `impersonation.WithImpersonation(handler, c.Authorization.Authorizer, c.Serializer)` — `config.go:WithImpersonation 调用点`
6. `genericapifilters.WithAudit(handler, c.AuditBackend, c.AuditPolicyRuleEvaluator, c.LongRunningFunc)` — `config.go:WithAudit 调用点`
7. 失败处理链（authentication 失败时走）：
   ```go
   failedHandler := genericapifilters.Unauthorized(c.Serializer)
   failedHandler = genericapifilters.WithFailedAuthenticationAudit(failedHandler, c.AuditBackend, c.AuditPolicyRuleEvaluator)
   ``` — `config.go:failedHandler 定义`、`config.go:WithFailedAuthenticationAudit 调用点`
8. `genericapifilters.WithTracing(handler, c.TracerProvider)`（注释：放在 authentication 之后，让已认证客户端可影响采样）— `config.go:WithTracing 调用点`
9. `genericapifilters.WithAuthentication(handler, c.Authentication.Authenticator, failedHandler, c.Authentication.APIAudiences, c.Authentication.RequestHeaderConfig)` — `config.go:WithAuthentication 调用点`
10. `genericfilters.WithCORS(handler, c.CorsAllowedOriginList, nil, nil, nil, "true")` — `config.go:WithCORS 调用点`
11. `genericapifilters.WithWarningRecorder(handler)`（注释：必须被 timeout handler 包裹以保证 warning header 线程安全）— `config.go:WithWarningRecorder 调用点`
12. `genericfilters.WithTimeoutForNonLongRunningRequests(handler, c.LongRunningFunc)` — `config.go:WithTimeoutForNonLongRunningRequests 调用点`
13. `genericapifilters.WithRequestDeadline(handler, c.AuditBackend, c.AuditPolicyRuleEvaluator, c.LongRunningFunc, c.Serializer, c.RequestTimeout)` — `config.go:WithRequestDeadline 调用点`
14. `genericfilters.WithWaitGroup(handler, c.LongRunningFunc, c.NonLongRunningRequestWaitGroup)` — `config.go:WithWaitGroup 调用点`
15. 条件 `c.ShutdownWatchTerminationGracePeriod > 0` → `genericfilters.WithWatchTerminationDuringShutdown(...)` — `config.go:+66~+68`
16. 条件 `c.SecureServing != nil && !DisableHTTP2 && c.GoawayChance > 0` → `genericfilters.WithProbabilisticGoaway(...)` — `config.go:+69~+71`
17. `genericapifilters.WithCacheControl(handler)` — `config.go:WithCacheControl 调用点`
18. `genericfilters.WithHSTS(handler, c.HSTSDirectives)` — `config.go:+74`
19. 条件 `c.ShutdownSendRetryAfter` → `genericfilters.WithRetryAfter(...)` — `config.go:+75~+77`
20. `genericfilters.WithHTTPLogging(handler)` — `config.go:WithHTTPLogging 调用点`
21. `genericapifilters.WithLatencyTrackers(handler)` — `config.go:WithLatencyTrackers 调用点`
22. 条件 feature gate `APIServingWithRoutine` → `routine.WithRoutine(handler, c.LongRunningFunc)`（注释：必须在 `WithPanicRecovery` 之后以受 panic 保护）— `config.go:+65~+67` 区域
23. `genericapifilters.WithRequestInfo(handler, c.RequestInfoResolver)` — `config.go:WithRequestInfo 调用点`
24. `genericapifilters.WithRequestReceivedTimestamp(handler)` — `config.go:WithRequestReceivedTimestamp 调用点`
25. 条件 `c.LoadReporter != nil` → `loadmetrics.WithEndpointLoadMetrics(...)` — `config.go`
26. `genericapifilters.WithMuxAndDiscoveryComplete(handler, c.lifecycleSignals.MuxAndDiscoveryComplete.Signaled())` — `config.go:WithMuxAndDiscoveryComplete 调用点`
27. `genericfilters.WithPanicRecovery(handler, c.RequestInfoResolver)` — `config.go:WithPanicRecovery 调用点`
28. `genericapifilters.WithAuditInit(handler)` — `config.go:WithAuditInit 调用点`，返回 `handler`

关键语义：
- **最内层（最先执行）是 `WithAuditInit` → `WithPanicRecovery` → `WithRequestInfo`**：即 audit-id 注入、panic 兜底、RequestInfo 解析都在最早期。
- **请求生命周期的三大守门在最外**：`WithAuthorization`（最外，紧随 TrackCompleted）、`WithAuthentication`（中间靠外）、`WithAudit`/`WithImpersonation` 夹在两者之间。
- 偏移为 `config.go` 内 `DefaultBuildHandlerChain` 函数体的行偏移（相对其签名行）；各步骤同时标了调用点符号名，按符号 `grep -n` 即可取到当前绝对行号。

### 1.2 `APIServerHandler` / `Director` / `NotFoundHandler`

`staging/src/k8s.io/apiserver/pkg/server/handler.go`

- `type APIServerHandler struct` — `handler.go:APIServerHandler`，四个字段：
  - `FullHandlerChain http.Handler`（+1 区，注释："eventually served with… include the full filter chain and then call the Director"）
  - `GoRestfulContainer *restful.Container`（+5）
  - `NonGoRestfulMux *mux.PathRecorderMux`（+8，注释："the final HTTP handler in the chain… comes after all filters and the API handling"）
  - `Director http.Handler`（+31 区，长注释解释为何 mux 要放在 gorestful 前面）
- `type HandlerChainBuilderFn func(apiHandler http.Handler) http.Handler` — `handler.go:HandlerChainBuilderFn`
- `NewAPIServerHandler(name, s runtime.NegotiatedSerializer, handlerChainBuilder HandlerChainBuilderFn, notFoundHandler http.Handler) *APIServerHandler` — `handler.go:NewAPIServerHandler`
  - 构造 `nonGoRestfulMux := mux.NewPathRecorderMux(name)`；若 `notFoundHandler != nil` 则 `nonGoRestfulMux.NotFoundHandler(notFoundHandler)` — `+3~+7`
  - `gorestfulContainer := restful.NewContainer()`，`Router(restful.CurlyRouter{})`，设 `RecoverHandler`/`ServiceErrorHandler` — `+9~+17`
  - `director := director{name, goRestfulContainer, nonGoRestfulMux}` — `+19~+23`
  - 返回 `&APIServerHandler{FullHandlerChain: handlerChainBuilder(director), ...}` — **chain 在此被套上 Director** — `+25`
- `type director struct` — `handler.go:director`；字段 `name` / `goRestfulContainer` / `nonGoRestfulMux`
- `func (d director) ServeHTTP(w, req)` — `handler.go:director.ServeHTTP`，mux vs gorestful 分流逻辑：
  - 遍历 `d.goRestfulContainer.RegisteredWebServices()`；`ws.RootPath() == "/apis"` 时只对 `path == "/apis" || path == "/apis/"` 特判走 `d.goRestfulContainer.Dispatch(w, req)` — `+8~+18`
  - `case strings.HasPrefix(path, ws.RootPath())` 且「精确等长或下一字符为 `/`」走 `Dispatch` — `+20~+30`
  - 都不匹配 → `d.nonGoRestfulMux.ServeHTTP(w, req)` — `+34`(=154 前)
- `func (a *APIServerHandler) ServeHTTP(w, r)` — `handler.go:APIServerHandler.ServeHTTP`，仅 `a.FullHandlerChain.ServeHTTP(w, r)`
- `func (a *APIServerHandler) ListedPaths() []string` — `handler.go:ListedPaths`
- `NotFoundHandler` 是 `mux.PathRecorderMux` 上的方法（`staging/src/k8s.io/apiserver/pkg/server/mux/pathrecorder.go`，本篇未逐行核对该文件）；在 `config.go` 的调用点：`s.Handler.NonGoRestfulMux.NotFoundHandler(routes.IndexLister{StatusCode: http.StatusNotFound, PathProvider: s.listedPathProvider})`，且前置条件为 `delegationTarget.UnprotectedHandler() == nil && c.EnableIndex`（`config.go:+1058~+1066`，函数 `New` 内）

### 1.3 `CompletedConfig.New` wiring

- `func (c *Config) Complete(informers informers.SharedInformerFactory) CompletedConfig` — `config.go:Complete`
- chain 构造闭包（在 `New` 内，`config.go`）：
  ```go
  handlerChainBuilder := func(handler http.Handler) http.Handler {
      return c.BuildHandlerChainFunc(handler, c.Config)
  }
  ```
  即 `BuildHandlerChainFunc` 字段（默认 = `DefaultBuildHandlerChain`）决定真正用的链。
- `apiServerHandler := NewAPIServerHandler(name, c.Serializer, handlerChainBuilder, delegationTarget.UnprotectedHandler())` — `config.go`
- `s := &GenericAPIServer{ ... Handler: apiServerHandler, admissionControl: c.AdmissionControl, AuditBackend: c.AuditBackend, Authorizer: c.Authorization.Authorizer, ... }` — `config.go+`
  - 关键字段绑定：`admissionControl`、`AuditBackend`、`Authorizer`、`Handler` 全部在这里从 `CompletedConfig` 注入 `GenericAPIServer`。

---

## 2. 认证 (Authentication)

### 2.1 authenticator 接口

`staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go`

- `type Token interface` — `interfaces.go:Token`，方法 `AuthenticateToken(ctx, token string) (*Response, bool, error)`（+1）
- `type Request interface` — `interfaces.go:Request`，方法 `AuthenticateRequest(req *http.Request) (*Response, bool, error)`（+1）
- `type TokenFunc func(...)` — `interfaces.go:TokenFunc`，`AuthenticateToken` 实现（+2）
- `type RequestFunc func(...)` — `interfaces.go:RequestFunc`，`AuthenticateRequest` 实现（+2）
- `type Response struct` — `interfaces.go:Response(=58 区域)`，含 `Audiences`（"the set of audiences the authenticator was able to validate the token against"）等字段
- `staging/src/k8s.io/apiserver/pkg/authentication/authenticator/audiences.go` / `audagnostic.go`：`Audiences` 类型与 `WrapAudienceAgnosticRequest`/`WrapAudienceAgnosticToken`（在 config.go 中被大量调用；本篇未逐行打开这两个文件，仅确认调用点）

### 2.2 `WithAuthentication` filter

`staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go`

- `WithAuthentication(handler, auth authenticator.Request, failed http.Handler, apiAuds authenticator.Audiences, requestHeaderConfig *authenticatorfactory.RequestHeaderConfig) http.Handler` — `authentication.go:WithAuthentication`，薄包装转调 `withAuthentication(..., metrics)`
- `withAuthentication(...)` — `authentication.go:withAuthentication`，核心逻辑偏移：
  - `auth == nil` → `klog.Warning("Authentication is disabled"); return handler` — `+3~+5`
  - 构造 `standardRequestHeaderConfig`（`X-Remote-User` / `X-Remote-Uid` / `X-Remote-Group` / `X-Remote-Extra-`）— `+7~+12`
  - `if len(apiAuds) > 0 { req = req.WithContext(authenticator.WithAudiences(req.Context(), apiAuds)) }` — `+15~+17`
  - `resp, ok, err := auth.AuthenticateRequest(req)` — `+18`
  - `err != nil || !ok` → `failed.ServeHTTP(w, req); return` — `+25~+31`
  - `!audiencesAreAcceptable(apiAuds, resp.Audiences)` → 走 failed — `+33~+37`
  - 成功后 `req.Header.Del("Authorization")` — `+41`(=90 前)
- `Unauthorized(s runtime.NegotiatedSerializer) http.Handler` — `authentication.go:Unauthorized`：即 chain 中 `failedHandler` 的最内层
- `audiencesAreAcceptable(...)` — `=153`；`isAnonymousUser(u user.Info) bool` — `=161`

### 2.3 认证器链（kube-apiserver 侧）

`pkg/kubeapiserver/authenticator/config.go`

- `type Config struct` — `config.go:Config`
- `func (config Config) New(serverLifecycle context.Context) (authenticator.Request, func(...) error, *spec.SecurityDefinitions, spec3.SecuritySchemes, error)` — `config.go:Config.New`

构造顺序（偏移相对 `+107`）：
1. **requestheader**：`config.RequestHeaderConfig != nil` → `headerrequest.NewDynamicVerifyOptionsSecure(VerifyOptions, AllowedClientNames, UsernameHeaders, UIDHeaders, GroupHeaders, ExtraHeaderPrefixes)`，再 `authenticator.WrapAudienceAgnosticRequest(config.APIAudiences, requestHeaderAuthenticator)` 追加进 `authenticators` — `+9~+16`
2. **client cert (x509)**：`config.ClientCAContentProvider != nil` → `x509.NewDynamic(VerifyOptions, x509.CommonNameUserConversion)` — `+20~+23`
3. **bearer token 组**（依次 append 进 `tokenAuthenticators`）：
   - token file：`len(config.TokenAuthFile) > 0` → `newAuthenticatorFromTokenFile(config.TokenAuthFile)` — `+27~+31`
   - legacy service account：`config.ServiceAccountPublicKeysGetter != nil` → `newLegacyServiceAccountAuthenticator(...)` — `+32~+37`
   - 新式 service account（JWT）：`len(config.ServiceAccountIssuers) > 0 && PublicKeysGetter != nil` → `newServiceAccountAuthenticator(...)` — `+38~+43`
   - bootstrap token：`config.BootstrapToken && BootstrapTokenAuthenticator != nil` → `WrapAudienceAgnosticToken(...)` — `+45~+47`
   - **OIDC/JWT**（注释：有意放在 service account 之后，避免两者互相造成 token 缓存 miss）→ `newJWTAuthenticator(...)` + `authenticationConfigUpdater`，以 `authenticator.TokenFunc` 包装 `jwtAuthenticatorPtr.Load().jwtAuthenticator.AuthenticateToken` — `+56~+80`
   - webhook token：`len(config.WebhookTokenAuthnConfigFile) > 0` → `newWebhookTokenAuthenticator(config)` — `+82~+87`
4. **token union + 缓存**：`len(tokenAuthenticators) > 0` → `tokenunion.New(tokenAuthenticators...)`；若 `TokenSuccessCacheTTL > 0 || TokenFailureCacheTTL > 0` 再套 `tokencache.New(tokenAuth, true, ...)`；最后 `authenticators = append(authenticators, bearertoken.New(tokenAuth), websocket.NewProtocolAuthenticator(tokenAuth))` — `+89~+93`；同时注册 v2/v3 `BearerToken` securityScheme — `+95~+110`
5. 空链兜底：`len(authenticators) == 0` → 若 `Anonymous.Enabled` 返回 `anonymous.NewAuthenticator(config.Anonymous.Conditions)`，否则返回 nil — `+112~+116`
6. **request union**：`authenticator := union.New(authenticators...)` — `+118`(偏移；实际行 ≈ `config.go`)
7. `authenticator = group.NewAuthenticatedGroupAdder(authenticator)` — `+119`(≈`config.go`)
8. Anonymous：`config.Anonymous.Enabled` → `authenticator = union.NewFailOnError(authenticator, anonymous.NewAuthenticator(config.Anonymous.Conditions))`（注释：链返回 error 时按 error 处理，不当作匿名）— `+121~+124`(≈`config.go`)
9. `return authenticator, updateAuthenticationConfig, &securityDefinitionsV2, securitySchemesV3, nil` — `+126`

helper 符号：
- `newAuthenticatorFromTokenFile(tokenAuthFile string)` — `config.go:newAuthenticatorFromTokenFile`
- `newLegacyServiceAccountAuthenticator(...)` — `config.go:newLegacyServiceAccountAuthenticator`；内部 `serviceaccount.NewLegacyValidator(lookup, serviceAccountGetter, secretsWriter)` — `+8`
- `newServiceAccountAuthenticator(issuers, publicKeysGetter, apiAudiences, serviceAccountTokenGetter)` — `config.go:newServiceAccountAuthenticator`；核心 `serviceaccount.JWTTokenAuthenticator(issuers, publicKeysGetter, apiAudiences, serviceaccount.NewValidator(serviceAccountGetter))` — `+4`
- `newWebhookTokenAuthenticator(config Config)` — `config.go:newWebhookTokenAuthenticator`
- `newJWTAuthenticator(...)` — `config.go:newJWTAuthenticator`；内部 `oidc.New(ctx, oidc.Options{...})` — `+20`，并 `authenticator.WrapAudienceAgnosticToken(apiAudiences, tokenunion.NewFailOnError(jwtAuthenticators...))` — `+35`
- `type jwtAuthenticatorWithCancel struct` — `config.go:jwtAuthenticatorWithCancel`
- `type authenticationConfigUpdater struct` — `config.go:authenticationConfigUpdater`；`updateAuthenticationConfig(...)` — `=314`

### 2.4 union authenticator

`staging/src/k8s.io/apiserver/pkg/authentication/request/union/union.go`

- `type unionAuthRequestHandler struct` — `union.go:unionAuthRequestHandler`；字段 `Handlers []authenticator.Request`、`FailOnError bool`
- `New(authRequestHandlers ...authenticator.Request) authenticator.Request` — `union.go:New`（单元素直接返回，否则 `FailOnError:false`）
- `NewFailOnError(...)` — `union.go:NewFailOnError`
- `AuthenticateRequest(req)` — `union.go:unionAuthRequestHandler.AuthenticateRequest(=53 区域)`：遍历链，`err != nil && FailOnError` 立即返回；最终聚合错误

token 侧 union：`staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go`
- `type unionAuthTokenHandler struct` — `union.go:unionAuthTokenHandler`
- `New(authTokenHandlers ...authenticator.Token)` — `union.go:New`
- `NewFailOnError(...)` — `union.go:NewFailOnError`
- `AuthenticateToken(ctx, token)` — `union.go:unionAuthTokenHandler.AuthenticateToken`

### 2.5 requestheader authenticator

`staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go`

- `type requestHeaderAuthRequestHandler struct` — `requestheader.go:requestHeaderAuthRequestHandler`
- `New(nameHeaders, uidHeaders, groupHeaders, extraHeaderPrefixes []string)` — `requestheader.go:New`
- `NewDynamic(...)` — `requestheader.go:NewDynamic`
- `NewDynamicVerifyOptionsSecure(verifyOptionFn x509request.VerifyOptionFunc, proxyClientNames, nameHeaders, uidHeaders, groupHeaders, extraHeaderPrefixes StringSliceProvider)` — `requestheader.go:NewDynamicVerifyOptionsSecure`（kube-apiserver 用的就是这个）
- `type StringSliceProvider interface` — `=31`；`StringSliceProviderFunc` — `=37`；`StaticStringSlice` — `=45`

### 2.6 options.ApplyTo（认证）

kube-apiserver 侧：`pkg/kubeapiserver/options/authentication.go`
- `func (o *BuiltInAuthenticationOptions) ApplyTo(ctx, authInfo *genericapiserver.AuthenticationInfo, secureServing, egressSelector, openAPIConfig, openAPIV3Config, extclient, versionedInformer, apiServerID) error` — `authentication.go:BuiltInAuthenticationOptions.ApplyTo`
  - `authenticatorConfig, err := o.ToAuthenticationConfig()` — `+25`
  - client cert → `authInfo.ApplyClientCert(ClientCAContentProvider, secureServing)` — `+27~+30`
  - requestheader CA → `authInfo.ApplyClientCert(RequestHeaderConfig.CAContentProvider, secureServing)` — `+31~+34`
  - `authInfo.RequestHeaderConfig = ...`；`authInfo.APIAudiences = o.APIAudiences`；service account issuers 非空且 `len(APIAudiences)==0` 时 `authInfo.APIAudiences = authenticator.Audiences(o.ServiceAccounts.Issuers)` — `+36~+40`
  - token getter：`o.ServiceAccounts.OptionalTokenGetter != nil` 用之，否则 `serviceaccountcontroller.NewGetterFromClient(extclient, Secrets, ServiceAccounts, Pods, Nodes, ValidatingWebhookConfigurations, MutatingWebhookConfigurations listers)` — `+42~+53`
  - `authenticatorConfig.SecretsWriter = extclient.CoreV1()` — `+55`
  - bootstrap token：`authenticatorConfig.BootstrapTokenAuthenticator = bootstrap.NewTokenAuthenticator(versionedInformer.Core().V1().Secrets().Lister().Secrets(metav1.NamespaceSystem))` — `+57~+61`
  - egressSelector → `authenticatorConfig.CustomDial` / `EgressLookup` — `+63~+70`
  - `authenticatorConfig.APIServerID = apiServerID` — `+72`
  - **`authenticator, updateAuthenticationConfig, openAPIV2SecurityDefinitions, openAPIV3SecuritySchemes, err := authenticatorConfig.New(ctx)`** — `authentication.go:New 调用点`
  - `authInfo.Authenticator = authenticator` — `+76`
  - `len(o.AuthenticationConfigFile) > 0` → 注册 metric + `go filesystem.WatchUntil(ctx, time.Minute, o.AuthenticationConfigFile, func(){...})` 热重载（含 anonymous 配置不被 reload 改动的保护）— `+78~+115`

aggregated/delegating 侧：`staging/src/k8s.io/apiserver/pkg/server/options/authentication.go`
- `func (s *DelegatingAuthenticationOptions) ApplyTo(authenticationInfo *server.AuthenticationInfo, servingInfo *server.SecureServingInfo, openAPIConfig *openapicommon.Config) error` — `authentication.go:DelegatingAuthenticationOptions.ApplyTo`

---

## 3. 鉴权 (Authorization)

### 3.1 authorizer 接口

`staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go`

- `type Attributes interface` — `interfaces.go:Attributes`，方法（偏移相对 `+35`）：`GetUser` `GetVerb` `IsReadOnly` `GetNamespace` `GetResource` `GetSubresource` `GetName` `GetAPIGroup` `GetAPIVersion` `IsResourceRequest` `GetPath` `GetFieldSelector` `GetLabelSelector`
- `type UnconditionalAuthorizer interface` — `interfaces.go:UnconditionalAuthorizer(=86 区域)`，唯一方法 `Authorize(ctx, a Attributes) (authorized Decision, reason string, err error)` — `interfaces.go:UnconditionalAuthorizer.Authorize`
- `type Authorizer interface` — `interfaces.go:Authorizer`，内嵌 `UnconditionalAuthorizer` + `ConditionsAwareAuthorize(ctx, a) ConditionsAwareDecision`（`=114`）+ `EvaluateConditions(ctx, decision, data)`（`=123`）
- `var ErrorConditionEvaluationNotSupported` — `interfaces.go:ErrorConditionEvaluationNotSupported`
- `var _ Authorizer = AuthorizerFunc(nil)` — `=133`
- `type AuthorizerFunc func(...)` — `interfaces.go:AuthorizerFunc`；`Authorize` `ConditionsAwareAuthorize` `EvaluateConditions`(=143，返回 `DecisionDeny,"",ErrorConditionEvaluationNotSupported`)
- `type RuleResolver interface` — `interfaces.go:RuleResolver`，方法 `RulesFor(ctx, user, namespace) ([]ResourceRuleInfo, []NonResourceRuleInfo, bool, error)`
- `type RequestAttributesGetter interface` — `=154`，`GetRequestAttributes(user.Info, *http.Request) Attributes`
- `type AttributesRecord struct` — `interfaces.go:AttributesRecord`，字段：`User user.Info` `Verb` `Namespace` `APIGroup` `APIVersion` `Resource` `Subresource` `Name` `ResourceRequest bool` `Path` `FieldSelectorRequirements` `FieldSelectorParsingErr` `LabelSelectorRequirements` `LabelSelectorParsingErr`
- 其余同包：`Decision`/`DecisionAllow|Deny|NoOpinion`（在 `evaluate.go`/`conditions.go`，本篇未逐行打开）；`ConditionsAwareDecisionFromParts` 被 RBAC 等大量调用

### 3.2 `WithAuthorization` filter

`staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go`

- `type ConditionalAuthorizationRequestClassifier func(attrs authorizer.Attributes) bool` — `authorization.go:ConditionalAuthorizationRequestClassifier`
- `WithAuthorization(hhandler http.Handler, auth authorizer.UnconditionalAuthorizer, s runtime.NegotiatedSerializer) http.Handler` — `authorization.go:WithAuthorization`（薄包装 → `withAuthorization(..., metrics)`）
- `withAuthorization(handler, a, s, metrics)` — `authorization.go:withAuthorization`，偏移：
  - `a == nil` → `klog.Warning("Authorization is disabled"); return handler` — `+2~+4`
  - `liftedAuthorizer := authorizer.AuthorizerFunc(a.Authorize)` — `+6`
  - `attributes, err := GetAuthorizerAttributes(ctx)` — `+11~+13`
  - `authorized, reason, err := a.Authorize(ctx, attributes)` — `+15`
  - `request.TrackAuthorizationLatency(...)` + defer metrics — `+20~+24`
  - `authorized == authorizer.DecisionAllow`（注释：RBAC 可能带 error 仍 Allow，故先判 decision）→ `audit.AddAuditAnnotations(ctx, DecisionAnnotationKey, DecisionAllow, ReasonAnnotationKey, reason)` — `+26~+30`
- `WithConditionsAwareAuthorization(...)` — `authorization.go:WithConditionsAwareAuthorization`；`withConditionsAwareAuthorization(...)` — `=143`
- `GetAuthorizerAttributes(ctx context.Context) (authorizer.Attributes, error)` — `authorization.go:GetAuthorizerAttributes`（审计 filter 也复用它取 attributes）

### 3.3 authorizer 链（kube-apiserver 侧）

`pkg/kubeapiserver/authorizer/config.go`
- `type Config struct` — `config.go:Config(=52 区)`，含 `PolicyFile`（ABAC）、`WebhookRetryBackoff` 等
- `func (c Config) New(ctx)` 实际为 `func (r *...) ... New`（构造器在 `config.go:~60-190` 区）；**按模式逐个构建**（偏移相对链构建块起点，`config.go:~108` 起）：
  - `ModeNode` → `node.RegisterMetrics()` + `node.NewGraph()` + `node.AddGraphEventHandlers(ctx, graph, Nodes, Pods, PersistentVolumes, VolumeAttachments, ResourceSlices, podCertificateRequestInformer)` + `r.nodeAuthorizer = node.NewAuthorizer(graph, nodeidentifier.NewDefaultNodeIdentifier(), bootstrappolicy.NodeRules())` — `config.go:New 内 Node 分支(≈119~138)`
  - `ModeABAC` → `r.abacAuthorizer, err = abac.NewFromFile(config.PolicyFile)` — `config.go:ABAC 分支(≈143~147)`
  - `ModeRBAC` →
    ```go
    r.rbacAuthorizer = rbac.New(
        &rbac.RoleGetter{Lister: ...Rbac().V1().Roles().Lister()},
        &rbac.RoleBindingLister{Lister: ...RoleBindings().Lister()},
        &rbac.ClusterRoleGetter{Lister: ...ClusterRoles().Lister()},
        &rbac.ClusterRoleBindingLister{Lister: ...ClusterRoleBindings().Lister()},
    )
    ``` — `config.go:RBAC 分支(≈149~157)`
  - `seenTypes.Delete(authzconfig.TypeWebhook)`; `r.requireNonWebhookTypes = seenTypes` — `≈160~163`
  - 最终 `authorizer, ruleResolver, err := r.newForConfig(r.initialConfig.AuthorizationConfiguration)`；`r.current.Store(&authorizerResolver{authorizer, ruleResolver})`；`ReloadFile != ""` 则 `go r.runReload(ctx)` — `≈166~180`

⚠️ 注意：本 HEAD 把模式→具体 authorizer 的分发拆到 `pkg/kubeapiserver/authorizer/reload.go`（`ModeNode`=125、`ModeAlwaysAllow`=134、`ModeABAC`=148、`ModeWebhook`=157、`ModeRBAC`=231）。`config.go` 内那段 switch 是「跨 reload 持久化」的那部分构建。两处需合看。

options 侧：`pkg/kubeapiserver/options/authorization.go`
- `func (o *BuiltInAuthorizationOptions) ToAuthorizationConfig(versionedInformerFactory, client) (*authorizer.Config, error)` — `authorization.go:ToAuthorizationConfig`：`--authorization-config` 文件走 `authorizer.LoadAndValidateFile(...)`，否则 `o.buildAuthorizationConfiguration()`（legacy flags）；返回 `&authorizer.Config{PolicyFile, VersionedInformerFactory, KubeClient, WebhookRetryBackoff, ReloadFile, AuthorizationConfiguration, InitialAuthorizationConfigurationData}` — `+57~+66`
- `buildAuthorizationConfiguration()` — `authorization.go:buildAuthorizationConfiguration(=255 区域)`：`ModeWebhook` 单独展开成 `authzconfig.WebhookConfiguration{...}`（含 `FailurePolicy: FailurePolicyNoOpinion`、`Timeout: 30s`、`ConnectionInfo.Type = ...KubeConfigFile`），其余 `default` 走 `authzconfig.AuthorizerConfiguration{Type: mode, Name: authorizer.GetNameForAuthorizerMode(mode)}`
- 调用方：`pkg/controlplane/apiserver/config.go` → `s.Authorization.ToAuthorizationConfig(versionedInformers, client)`

aggregated/delegating 侧：`staging/src/k8s.io/apiserver/pkg/server/options/authorization.go`
- `func (s *DelegatingAuthorizationOptions) ApplyTo(c *server.AuthorizationInfo) error` — `authorization.go:DelegatingAuthorizationOptions.ApplyTo`
- `func (s *DelegatingAuthorizationOptions) toAuthorizer(client kubernetes.Interface) (authorizer.Authorizer, error)` — `authorization.go:toAuthorizer`：`AlwaysAllowGroups` → `authorizerfactory.NewPrivilegedGroups(...)`（名 `always-allow-groups.authorizer.kubernetes.io`）；`AlwaysAllowPaths` → `path.NewAuthorizer(...)`；`client != nil` → `authorizerfactory.DelegatingAuthorizerConfig{SubjectAccessReviewClient, AllowCacheTTL, DenyCacheTTL, WebhookRetryBackoff, AuthorizationConditionsReviewClient}.New()`；最后 `union.New(authorizers...)` — `+7~+58`
- 字段默认值：`AlwaysAllowPaths: []string{"/healthz","/readyz","/livez"}`、`AlwaysAllowGroups: []string{"system:masters"}` — `authorization.go:New 默认值(84~92)`
- `authorizerfactory` 侧：`NewAlwaysAllowAuthorizer` — `staging/src/k8s.io/apiserver/pkg/authorization/authorizerfactory/builtin.go:NewAlwaysAllowAuthorizer`；`NewAlwaysDenyAuthorizer`；`NewPrivilegedGroups`

### 3.4 RBAC authorizer

`plugin/pkg/auth/authorizer/rbac/rbac.go`

- `type RBACAuthorizer struct` — `rbac.go:RBACAuthorizer`，单字段 `authorizationRuleResolver RequestToRuleMapper`
- `type authorizingVisitor struct` — `rbac.go:authorizingVisitor`，字段 `requestAttributes authorizer.Attributes`、`allowed bool`、`reason string`、`errors []error`
- `func (v *authorizingVisitor) visit(source fmt.Stringer, rule *rbacv1.PolicyRule, err error) bool` — `rbac.go:visit`：`rule != nil && RuleAllows(v.requestAttributes, rule)` → `allowed=true; reason=fmt.Sprintf("RBAC: allowed by %s", source.String()); return false`（短路）
- `func (r *RBACAuthorizer) Authorize(ctx, requestAttributes authorizer.Attributes) (authorizer.Decision, string, error)` — `rbac.go:RBACAuthorizer.Authorize`：
  - `r.authorizationRuleResolver.VisitRulesFor(ctx, requestAttributes.GetUser(), requestAttributes.GetNamespace(), ruleCheckingVisitor.visit)` — `+3`
  - `allowed` → `return authorizer.DecisionAllow, reason, nil` — `+4~+6`
  - klog V5 拼装详细 deny 日志（resource vs nonResourceURL、namespace/cluster-wide）— `+9~+50`
  - 返回 `authorizer.DecisionNoOpinion, reason, nil`（有聚合错误时 `reason = fmt.Sprintf("RBAC: %v", utilerrors.NewAggregate(errors))`）— `+52~+58`
- `func (r *RBACAuthorizer) ConditionsAwareAuthorize(...)` — `rbac.go:ConditionsAwareAuthorize`，仅 `return authorizer.ConditionsAwareDecisionFromParts(r.Authorize(...))`
- `func (r *RBACAuthorizer) RulesFor(...)` — `rbac.go:RulesFor`
- `func New(roles rbacregistryvalidation.RoleGetter, roleBindings rbacregistryvalidation.RoleBindingLister, clusterRoles rbacregistryvalidation.ClusterRoleGetter, clusterRoleBindings rbacregistryvalidation.ClusterRoleBindingLister) *RBACAuthorizer` — `rbac.go:New`
- 辅助判定 `RuleAllows`（同包，供 `visit` 调用；未逐行打开其定义文件）

---

## 4. Admission

### 4.1 接口

`staging/src/k8s.io/apiserver/pkg/admission/interfaces.go`

- `type Interface interface` — `interfaces.go:Interface`，唯一方法 `Handles(operation Operation) bool`
- `type MutationInterface interface` — `interfaces.go:MutationInterface`，内嵌 `Interface` + `Admit(ctx context.Context, a Attributes, o ObjectInterfaces) (err error)`（注释：Context 仅用于 timeout/deadline/cancel/tracing）
- `type ValidationInterface interface` — `interfaces.go:ValidationInterface`，内嵌 `Interface` + `Validate(...)`（注释：NOT allowed to mutate）
- `type Operation = authorizer.AdmissionOperation` — `interfaces.go:Operation(=149 区域)`；常量 `Create="CREATE"` `Update="UPDATE"` `Delete="DELETE"` `Connect="CONNECT"` — `=152~156`
- `type PluginInitializer interface` — `interfaces.go:PluginInitializer`，方法 `Initialize(plugin Interface)`
- `type InitializationValidator interface` — `=167`，`ValidateInitialization() error`
- `type ConfigProvider interface` — `=172`，`ConfigFor(pluginName string) (io.Reader, error)`
- `Attributes` 接口的 reinvocation 相关方法（`IsReinvoke`/`SetIsReinvoke`/`ShouldReinvoke`/`SetShouldReinvoke`/`SetValue`/`Value`）在 `interfaces.go:~100-122` 区

### 4.2 Handler / Chain

`staging/src/k8s.io/apiserver/pkg/admission/handler.go`
- `type ReadyFunc func() bool` — `handler.go:ReadyFunc`
- `type Handler struct` — `handler.go:Handler`，字段 `operations sets.Set[string]`、`readyFunc ReadyFunc`
- `func (h *Handler) Handles(operation Operation) bool` — `handler.go:Handles` → `h.operations.Has(string(operation))`
- `func NewHandler(ops ...Operation) *Handler` — `handler.go:NewHandler`
- `SetReadyFunc` — `=59`；`WaitForReady() bool` — `=64`（`timeToWaitForReady` 超时，100ms 轮询）

`staging/src/k8s.io/apiserver/pkg/admission/chain.go`
- `type chainAdmissionHandler []Interface` — `chain.go:chainAdmissionHandler`
- `func NewChainHandler(handlers ...Interface) chainAdmissionHandler` — `chain.go:NewChainHandler`（注释：用于测试）
- `func (admissionHandler chainAdmissionHandler) Admit(ctx, a, o) error` — `chain.go:Admit`：遍历，`!handler.Handles(a.GetOperation())` 跳过；`handler.(MutationInterface)` 类型断言成功才 `Admit`；首个 error 立即返回 — `+3~+11`
- `func (admissionHandler chainAdmissionHandler) Validate(ctx, a, o) error` — `chain.go:Validate`：同构，断言 `ValidationInterface` — `+3~+11`
- `func (admissionHandler chainAdmissionHandler) Handles(operation Operation) bool` — `chain.go:Handles`：任一 handler 支持即 true

配套（admission 的 audit / reinvocation 包装，均在 `staging/src/k8s.io/apiserver/pkg/admission/`）：
- `audit.go` → `err = mutator.Admit(ctx, a, o)`（`WithAudit` 包装）
- `reinvocation.go`/`47` → `mutator.Admit(...)`（`WithReinvocation`，mutating webhook 可请求重入）
- `metrics/metrics.go` → `err := mutatingHandler.Admit(ctx, a, o)`

### 4.3 PluginInitializer / 初始化

- `PluginInitializer` 接口见 4.1（`interfaces.go`）。
- 插件注册：`staging/src/k8s.io/apiserver/pkg/admission/plugins.go`（`type Plugins struct` / `Register`；本篇未逐行打开，仅确认被各 plugin 的 `Register(plugins *admission.Plugins)` 调用）。

### 4.4 插件：mutating vs validating webhook + 内建插件

目录 `staging/src/k8s.io/apiserver/pkg/admission/plugin/`：`authorizer/` `cel/` `manifest/` `namespace/` `policy/` `resourcequota/` `webhook/`

**mutating webhook** — `plugin/webhook/mutating/plugin.go`
- `PluginName = "MutatingAdmissionWebhook"` — `plugin.go:PluginName`
- `func Register(plugins *admission.Plugins)` — `plugin.go:Register` → `plugins.Register(PluginName, func(configFile io.Reader) (admission.Interface, error){...})`
- `type Plugin struct` — `plugin.go:Plugin`
- `SetManifestLoaders(loaders *initializer.ManifestLoaders)` — `=57`
- `NewMutatingWebhook(configFile io.Reader) (*Plugin, error)` — `plugin.go:NewMutatingWebhook`
- `ValidateInitialization() error` — `=84`
- **`func (a *Plugin) Admit(ctx, attr, o) error`** — `plugin.go:Plugin.Admit`
- dispatcher：`plugin/webhook/mutating/dispatcher.go`
  - `type mutatingDispatcher struct` — `dispatcher.go:mutatingDispatcher`
  - `newMutatingDispatcher(p *Plugin) func(cm *webhookutil.ClientManager) generic.Dispatcher` — `dispatcher.go:newMutatingDispatcher`
  - `type versionedAttributeAccessor struct` — `=81`；`VersionedAttribute(gvk)` — `=87`
  - **`func (a *mutatingDispatcher) Dispatch(ctx, attr, o, hooks []webhook.WebhookAccessor) error`** — `dispatcher.go:mutatingDispatcher.Dispatch`（含 reinvocation ctx：`reinvokeCtx.SetValue(PluginName, webhookReinvokeCtx)` — `+7~+11`）
  - `callAttrMutatingHook(ctx, h *admissionregistrationv1.MutatingWebhook, invocation, attr, annotator, o, round, idx) (bool, error)` — `dispatcher.go:callAttrMutatingHook`
  - `type webhookAnnotator struct` — `=393`；`newWebhookAnnotator(...)` — `=402`；`addFailedOpenAnnotation`；`addMutationAnnotation(mutated bool)`

**validating webhook** — `plugin/webhook/validating/plugin.go`
- `PluginName = "ValidatingAdmissionWebhook"` — `plugin.go:PluginName`
- `Register(plugins)` — `=39`
- `type Plugin struct` — `=51`；`SetManifestLoaders` — `=59`
- `NewValidatingAdmissionWebhook(configFile io.Reader) (*Plugin, error)` — `plugin.go:NewValidatingAdmissionWebhook`（校验 static order 只支持 `webhookadmission.StaticFirst`，否则 `fmt.Errorf("only %q is supported by %s", ...)` — `=87`）
- **`func (a *Plugin) Validate(ctx, attr, o) error`** — `plugin.go:Plugin.Validate`
- dispatcher：`plugin/webhook/validating/dispatcher.go`（未逐行打开，结构与 mutating 对称）

**内建插件示例**
- NamespaceLifecycle — `plugin/namespace/lifecycle/admission.go`
  - `PluginName = "NamespaceLifecycle"` — `admission.go:PluginName`
  - `Register(plugins)` — `=55`
  - `type Lifecycle struct` — `=63`
  - **`func (l *Lifecycle) Admit(ctx, a, o) error`** — `admission.go:Lifecycle.Admit`
  - `NewLifecycle(immortalNamespaces sets.Set[string]) (*Lifecycle, error)` — `=194`；`newLifecycleWithClock(...)` — `=198`
  - `SetExternalKubeInformerFactory(f)` — `=208`（`PluginInitializer` 实现）；`SetExternalKubeClientSet(client)` — `=215`
- AuthorizationConditionsEnforcer（`conditionsenforcer.PluginName`）在 chain 装配里被引用：`config.go:+22` 的 warning `"ConditionalAuthorization feature gate is on, but the %s admission plugin is disabled"`（实现在 `plugin/authorizer/`，本篇未逐行打开）
- pod security：`plugin/policy/validating/plugin.go`（目录存在；`PluginName` 未在本篇逐行核对）

### 4.5 Admission 在请求路径上的运行位置

mutating admission（进 storage 前同步调用）：
- create — `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go`
  - `admissionAttributes := admission.NewAttributesRecord(obj, nil, scope.Kind, namespace, name, scope.Resource, scope.Subresource, admission.Create, options, dryrun.IsDryRun(options.DryRun), userInfo)` — `create.go` 前一行（`+199` 区，绝对行 `create.go:~201`）
  - `if mutatingAdmission, ok := admit.(admission.MutationInterface); ok && mutatingAdmission.Handles(admission.Create) { if err := mutatingAdmission.Admit(ctx, admissionAttributes, scope); err != nil {...} }` — **`create.go:mutatingAdmission.Admit`**
  - `admit = fieldmanager.NewManagedFieldsValidatingAdmissionController(admit)` — `create.go:+205`(≈205)
  - `requestFunc := func() { return r.Create(ctx, name, obj, rest.AdmissionToValidateObjectFunc(admit, admissionAttributes, scope), options) }` — `create.go:+193~+197`
- update — `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go`
  - `func UpdateResource(r rest.Updater, scope *RequestScope, admit admission.Interface) http.HandlerFunc` — `update.go:UpdateResource`
  - `admit = admission.WithAudit(admit)` — `update.go`
  - `admit = fieldmanager.NewManagedFieldsValidatingAdmissionController(admit)` — `update.go`
  - mutating：`if mutatingAdmission, ok := admit.(admission.MutationInterface); ok {...}` — `update.go`
  - 两分支 `mutatingAdmission.Admit(ctx, admission.NewAttributesRecord(..., admission.Create, updateToCreateOptions(options), ...))` / `(..., admission.Update, options, ...)` — **`update.go`** 与 **`update.go`**
  - validating（交给 rest 层在写 storage 时触发）：`withAuthorization(rest.AdmissionToValidateObjectFunc(admit, ...))` — `update.go`
- patch — `handlers/patch.go` → `return patchedObject, p.admissionCheck.Admit(ctx, attributes, p.objectInterfaces)`
- connect — `handlers/rest.go`（mutating，`admission.Connect`）/ `handlers/rest.go`（validating）
- delete — `staging/src/k8s.io/apiserver/pkg/registry/rest/delete.go` → `mutatingAdmission.Admit(ctx, finalAttributes, objInterfaces)`

validating admission（真正落库时校验）：
- `func AdmissionToValidateObjectFunc(admit admission.Interface, staticAttributes admission.Attributes, o admission.ObjectInterfaces) ValidateObjectFunc` — `staging/src/k8s.io/apiserver/pkg/registry/rest/create.go:AdmissionToValidateObjectFunc`
  - `validatingAdmission, ok := admit.(admission.ValidationInterface)`；断言失败返回 no-op `func(ctx, obj) error { return nil }` — `+2~+4`
  - 生成 name 为空时从 metadata 补 — `+7~+12`
  - 用 `admission.NewAttributesRecord(obj, staticAttributes.GetOldObject(), ...)` 构造 finalAttributes — `+16~+27`
- `func AdmissionToValidateObjectUpdateFunc(...)` — `staging/src/k8s.io/apiserver/pkg/registry/rest/update.go:AdmissionToValidateObjectUpdateFunc`，对称；含 `!validatingAdmission.Handles(finalAttributes.GetOperation()) → return nil` — `+24~+26`，末尾 `return validatingAdmission.Validate(ctx, finalAttributes, o)` — `+28`
- `GenericAPIServer.admissionControl` 字段来源见 1.3（`config.go+` 的 `admissionControl: c.AdmissionControl`）。

---

## 5. 审计 (Audit) + Impersonation

### 5.1 audit 核心类型

`staging/src/k8s.io/apiserver/pkg/audit/`

- `type Sink interface` — `types.go:Sink`，方法 `ProcessEvents(events ...*auditinternal.Event) bool`（注释：per audit id 最多被调 3 次；error 应 panic 才是 fatal；event 不可变、caller 会复用）
- `type Backend interface` — `types.go:Backend`，内嵌 `Sink` + `Run(stopCh <-chan struct{}) error` + `Shutdown()` + `String() string`
- `type PolicyRuleEvaluator interface` — `evaluator.go:PolicyRuleEvaluator`，唯一方法 `EvaluatePolicyRule(authorizer.Attributes) RequestAuditConfig`（注释：返回适用于该请求的 audit 配置）
- `func Union(backends ...Backend) Backend` — `union.go:Union`
- `type union struct` — `union.go:union`；`ProcessEvents(...)` — `union.go:union.ProcessEvents`；`Run` `Shutdown` `String`
- `NewPolicyRuleEvaluator(policy *audit.Policy) auditinternal.PolicyRuleEvaluator` — `policy/checker.go:NewPolicyRuleEvaluator`
- `type policyRuleEvaluator struct` — `policy/checker.go:policyRuleEvaluator`
- `func (p *policyRuleEvaluator) EvaluatePolicyRule(attrs authorizer.Attributes) auditinternal.RequestAuditConfig` — `policy/checker.go:EvaluatePolicyRule`
- 辅助：`unionStages(...)` `NewFakePolicyRuleEvaluator(...)` `isOmitManagedFields(...)` `ruleMatches(r, attrs)` `ruleMatchesNonResource(...)` `pathMatches(...)` `ruleMatchesResource(...)`
- 事件写入 helper — `audit/request.go`：
  - `LogRequestMetadata(ctx, req, requestReceivedTimestamp, attribs)` — `request.go:LogRequestMetadata`
  - `LogImpersonatedUser(ctx, user, constraint)` — `request.go:LogImpersonatedUser`
  - `LogRequestObject(ctx, obj, objGV, gvr, subresource, s)` — `request.go:LogRequestObject`
  - `LogRequestPatch(ctx, patch)` — `=162`
  - `LogResponseObject(ctx, obj, gv, s)` — `request.go:LogResponseObject`
  - `encodeObject` `maybeTruncateUserAgent` `copyWithoutManagedFields` `removeManagedFields` `shouldOmitManagedFields(ac *AuditContext)`
- ⚠️ 任务提到的 `type AuditBackend` / `type AuditEvent` 这两个**确切名**未在 `pkg/audit/` 顶层命中：实际是 `Backend`（`types.go`）与 `auditinternal.Event`（`staging/src/k8s.io/apiserver/pkg/apis/audit/types.go`，本篇未逐行打开）。`AuditContext` 在 `audit/context.go`（被 filter 大量使用，`AuditContextFrom` / `Init` / `Enabled` / `ProcessEventStage` / `SetEventResponseStatus` / `GetEventResponseStatus`），本篇未逐行打开该文件。

### 5.2 `WithAudit` filter

`staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go`

- `func WithAudit(handler http.Handler, sink audit.Sink, policy audit.PolicyRuleEvaluator, longRunningCheck request.LongRunningRequestCheck) http.Handler` — `audit.go:WithAudit`
  - `sink == nil || policy == nil` → 原样返回 handler（审计未配置即旁路）— `+2~+4`
  - `ac, err := evaluatePolicyAndCreateAuditEvent(req, policy, sink)` — `+7`；err → `responsewriters.InternalError(..., errors.New("failed to create audit event"))`
  - `!ac.Enabled()` → 直接 `handler.ServeHTTP` — `+16~+18`
  - `ac.ProcessEventStage(ctx, auditinternal.StageRequestReceived)`；未 processed → `audit.ApiserverAuditDroppedCounter.WithContext(ctx).Inc()` + InternalError — `+20~+25`
  - long-running 判定：`request.RequestInfoFrom(ctx)` + `longRunningCheck(req, ri)` — `+28~+34`
  - `respWriter := decorateResponseWriter(ctx, w, isLongRunning)` — `+36`
  - defer：recover → 设 500 Status + `ProcessEventStage(StagePanic)`；否则若 `GetEventResponseStatus()==nil` 伪造 `fakedSuccessStatus`（"Connection closed early"），long-running 补 `StageResponseStarted` — `+39~+66`
  - `writeLatencyToAnnotation(ctx)` + `ac.ProcessEventStage(ctx, auditinternal.StageResponseComplete)` — `+67~+68`
  - `handler.ServeHTTP(respWriter, req)` — `+70`
- `evaluatePolicyAndCreateAuditEvent(req, policy, sink) (*audit.AuditContext, error)` — `audit.go:evaluatePolicyAndCreateAuditEvent`：
  - `ac := audit.AuditContextFrom(ctx)`；nil → 返回 `(nil, nil)`（审计未配置）— `+5~+8`
  - `attribs, err := GetAuthorizerAttributes(ctx)` — `+11~+13`
  - `rac := policy.EvaluatePolicyRule(attribs)` — `+15`
  - `audit.ObservePolicyLevel(ctx, rac.Level)` — `+16`
  - `ac.Init(rac, sink)` — `+17~+19`
  - `rac.Level == auditinternal.LevelNone` → 不审计直接返回 — `+20~+22`
  - `requestReceivedTimestamp, ok := request.ReceivedTimestampFrom(ctx)`（取不到则 `time.Now()`）— `+24~+28`
  - `audit.LogRequestMetadata(ctx, req, requestReceivedTimestamp, attribs)` — `+30`
- `writeLatencyToAnnotation(ctx)` — `=152`
- `decorateResponseWriter(ctx, responseWriter, processResponseStartedStage bool) http.ResponseWriter` — `audit.go:decorateResponseWriter`
- `type auditResponseWriter struct` — `audit.go:auditResponseWriter`；`Unwrap` `processCode(code int)` `Write` `WriteHeader` `Hijack`；`var _ http.ResponseWriter = &auditResponseWriter{}` `var _ responsewriter.UserProvidedDecorator = ...`
- 配套 filter：
  - `WithAuditInit(handler http.Handler)` — `audit_init.go:WithAuditInit`；`withAuditInit(handler, newAuditIDFunc)` — `=39`（最内层，注入 audit-id）
  - `WithFailedAuthenticationAudit(failedHandler, sink, policy)` — `authn_audit.go:WithFailedAuthenticationAudit`；`getAuthMethods(req)` — `=58`（认证失败也留审计痕迹，挂在 chain 里 `failedHandler` 上）

### 5.3 `WithImpersonation` filter

`staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go`

- `func WithImpersonation(handler http.Handler, a authorizer.UnconditionalAuthorizer, s runtime.NegotiatedSerializer) http.Handler` — `impersonation.go:WithImpersonation`，偏移：
  - `impersonationRequests, err := buildImpersonationRequests(req.Header)` — `+3~+5`；err → `responsewriters.RespondWithError`
  - `len(impersonationRequests) == 0` → 直接放行 — `+9~+11`
  - `requestor, exists := request.UserFrom(ctx)`；不存在 → InternalError "no user found for request" — `+14~+18`
  - `groupsSpecified := len(req.Header[authenticationv1.ImpersonateGroupHeader]) > 0` — `+22`
  - 遍历每个 impersonation request，构造 `actingAsAttributes := &authorizer.AttributesRecord{User: requestor, Verb: "impersonate", ...}` — `+30~+40`
  - 按 `gvk.GroupKind()` 分派：`ServiceAccount` → `serviceaccount.MakeUsername/MakeGroupNames`；`User`；`Group`；`UserExtra`（`Resource="userextras"`, `Subresource=extraKey`）；`UID`（`Resource="uids"`）；default → `responsewriters.Forbidden(..., "unknown impersonation request type")` — `+42~+80`
  - **`decision, reason, err := a.Authorize(ctx, actingAsAttributes)`**，`err != nil || decision != authorizer.DecisionAllow` → `responsewriters.Forbidden(..., reason, s)` — `+82~+88`
  - 非 anonymous 用户且未显式指定 `system:authenticated`/`system:unauthenticated` → 追加 `user.AllAuthenticated`；anonymous 分支同理追加 `user.AllUnauthenticated` — `+90~+125`
  - `newUser := &user.DefaultInfo{Name, Groups, Extra, UID}`；`req = req.WithContext(request.WithUser(ctx, newUser))` — `+127~+131`
  - `httplog.LogOf(req, w).Addf("%v is impersonating %v", ...)` — `+134`
  - **`audit.LogImpersonatedUser(audit.WithAuditContext(ctx), newUser, "")`** — `+136`
  - 清除所有 impersonation header（`ImpersonateUserHeader` / `ImpersonateGroupHeader` / `ImpersonateUIDHeader` + `ImpersonateUserExtraHeaderPrefix` 前缀）— `+139~+146`
  - `handler.ServeHTTP(w, req)` — `+148`
- `userString(u user.Info) string` — `impersonation.go:userString`
- `unescapeExtraKey(encodedKey string) string` — `=204`
- `buildImpersonationRequests(headers http.Header) ([]v1.ObjectReference, error)` — `impersonation.go:buildImpersonationRequests`
- Constrained 版本：`impersonation.WithConstrainedImpersonation(...)`（feature gate `ConstrainedImpersonation`），实现同文件/同包内（`grep` 命中 `impersonation.go` 包，本篇未逐行定位其定义行 ⚠️ 未验证具体行号）。

### 5.4 Filter 顺序（任务点名的五个）

在 `config.go:DefaultBuildHandlerChain`（外→内）中的相对次序（见 §1.1 锚点）：

```
WithAuthorization        (config.go，或 WithConditionsAwareAuthorization)
  └─ TrackStarted "authorization"        (config.go)
WithPriorityAndFairness  (config.go)  |  WithMaxInFlightLimit (config.go)
WithImpersonation        (config.go)  |  WithConstrainedImpersonation
WithAudit                (config.go)
WithTracing              (config.go 区)
WithAuthentication       (config.go)   ← failedHandler = Unauthorized + WithFailedAuthenticationAudit
WithWarningRecorder      (config.go)
WithTimeoutForNonLongRunningRequests (config.go)
```

即任务要求的次序 **WithAuthentication 在 WithAudit 之内（更内层）**：请求先过 authorization → priority/fairness → impersonation → audit → authentication。⚠️ 这与任务描述里隐含的 "WithAuthentication → WithAudit" 直觉顺序相反——**audit 包在 authentication 外面**，因此认证失败也会被 `WithFailedAuthenticationAudit` 记录，而 `WithAudit` 拿到的 `RequestInfo` 尚未解析（RequestInfo 在最内层 `config.go`）。

---

## 符号 → 文件 索引表

| 符号 | 文件 |
|---|---|
| DefaultBuildHandlerChain | staging/src/k8s.io/apiserver/pkg/server/config.go |
| BuildHandlerChainWithStorageVersionPrecondition | staging/src/k8s.io/apiserver/pkg/server/config.go |
| Config.Complete | staging/src/k8s.io/apiserver/pkg/server/config.go |
| handlerChainBuilder 闭包 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| NewAPIServerHandler 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| NonGoRestfulMux.NotFoundHandler 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithAuthorization 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithPriorityAndFairness 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithMaxInFlightLimit 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithImpersonation 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithAudit 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithAuthentication 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithWarningRecorder 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithTimeoutForNonLongRunningRequests 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithWaitGroup 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithCacheControl 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithHTTPLogging 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithLatencyTrackers 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithRequestInfo 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| WithAuditInit 调用点 | staging/src/k8s.io/apiserver/pkg/server/config.go |
| APIServerHandler | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| HandlerChainBuilderFn | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| NewAPIServerHandler | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| ListedPaths | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| director | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| director.ServeHTTP | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| logStackOnRecover | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| serviceErrorHandler | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| APIServerHandler.ServeHTTP | staging/src/k8s.io/apiserver/pkg/server/handler.go |
| authenticator.Token | staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go |
| authenticator.Request | staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go |
| TokenFunc | staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go |
| RequestFunc | staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go |
| authenticator.Response | staging/src/k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go |
| unionAuthRequestHandler | staging/src/k8s.io/apiserver/pkg/authentication/request/union/union.go |
| request/union.New | staging/src/k8s.io/apiserver/pkg/authentication/request/union/union.go |
| request/union.NewFailOnError | staging/src/k8s.io/apiserver/pkg/authentication/request/union/union.go |
| unionAuthRequestHandler.AuthenticateRequest | staging/src/k8s.io/apiserver/pkg/authentication/request/union/union.go |
| token/union.unionAuthTokenHandler | staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go |
| token/union.New | staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go |
| token/union.NewFailOnError | staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go |
| token/union.AuthenticateToken | staging/src/k8s.io/apiserver/pkg/authentication/token/union/union.go |
| requestHeaderAuthRequestHandler | staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go |
| headerrequest.New | staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go |
| headerrequest.NewDynamic | staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go |
| headerrequest.NewDynamicVerifyOptionsSecure | staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go |
| kubeapiserver/authenticator.Config | pkg/kubeapiserver/authenticator/config.go |
| kubeapiserver/authenticator.Config.New | pkg/kubeapiserver/authenticator/config.go |
| newJWTAuthenticator | pkg/kubeapiserver/authenticator/config.go |
| jwtAuthenticatorWithCancel | pkg/kubeapiserver/authenticator/config.go |
| authenticationConfigUpdater | pkg/kubeapiserver/authenticator/config.go |
| newAuthenticatorFromTokenFile | pkg/kubeapiserver/authenticator/config.go |
| newLegacyServiceAccountAuthenticator | pkg/kubeapiserver/authenticator/config.go |
| newServiceAccountAuthenticator | pkg/kubeapiserver/authenticator/config.go |
| newWebhookTokenAuthenticator | pkg/kubeapiserver/authenticator/config.go |
| BuiltInAuthenticationOptions.ApplyTo | pkg/kubeapiserver/options/authentication.go |
| authenticatorConfig.New 调用点 | pkg/kubeapiserver/options/authentication.go |
| DelegatingAuthenticationOptions.ApplyTo | staging/src/k8s.io/apiserver/pkg/server/options/authentication.go |
| WithAuthentication | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go |
| withAuthentication | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go |
| Unauthorized | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go |
| audiencesAreAcceptable | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go |
| isAnonymousUser | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authentication.go |
| authorizer.Attributes | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| UnconditionalAuthorizer | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| UnconditionalAuthorizer.Authorize | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| authorizer.Authorizer | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| ConditionsAwareAuthorize (接口方法) | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| EvaluateConditions (接口方法) | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| ErrorConditionEvaluationNotSupported | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| AuthorizerFunc | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| RuleResolver | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| RequestAttributesGetter | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| AttributesRecord | staging/src/k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go |
| ConditionalAuthorizationRequestClassifier | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| WithAuthorization | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| withAuthorization | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| WithConditionsAwareAuthorization | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| withConditionsAwareAuthorization | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| GetAuthorizerAttributes | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authorization.go |
| NewAlwaysAllowAuthorizer | staging/src/k8s.io/apiserver/pkg/authorization/authorizerfactory/builtin.go |
| NewAlwaysDenyAuthorizer | staging/src/k8s.io/apiserver/pkg/authorization/authorizerfactory/builtin.go |
| NewPrivilegedGroups | staging/src/k8s.io/apiserver/pkg/authorization/authorizerfactory/builtin.go |
| ToAuthorizationConfig | pkg/kubeapiserver/options/authorization.go |
| buildAuthorizationConfiguration | pkg/kubeapiserver/options/authorization.go |
| DelegatingAuthorizationOptions.ApplyTo | staging/src/k8s.io/apiserver/pkg/server/options/authorization.go |
| toAuthorizer | staging/src/k8s.io/apiserver/pkg/server/options/authorization.go |
| kubeapiserver/authorizer Config.New 内 Node 分支 | pkg/kubeapiserver/authorizer/config.go:~119 |
| kubeapiserver/authorizer Config.New 内 ABAC 分支 | pkg/kubeapiserver/authorizer/config.go:~143 |
| kubeapiserver/authorizer Config.New 内 RBAC 分支 | pkg/kubeapiserver/authorizer/config.go:~149 |
| kubeapiserver/authorizer reload.go ModeNode | pkg/kubeapiserver/authorizer/reload.go |
| kubeapiserver/authorizer reload.go ModeAlwaysAllow | pkg/kubeapiserver/authorizer/reload.go |
| kubeapiserver/authorizer reload.go ModeABAC | pkg/kubeapiserver/authorizer/reload.go |
| kubeapiserver/authorizer reload.go ModeWebhook | pkg/kubeapiserver/authorizer/reload.go |
| kubeapiserver/authorizer reload.go ModeRBAC | pkg/kubeapiserver/authorizer/reload.go |
| ToAuthorizationConfig 调用点 | pkg/controlplane/apiserver/config.go |
| RBACAuthorizer | plugin/pkg/auth/authorizer/rbac/rbac.go |
| authorizingVisitor | plugin/pkg/auth/authorizer/rbac/rbac.go |
| authorizingVisitor.visit | plugin/pkg/auth/authorizer/rbac/rbac.go |
| RBACAuthorizer.Authorize | plugin/pkg/auth/authorizer/rbac/rbac.go |
| RBACAuthorizer.ConditionsAwareAuthorize | plugin/pkg/auth/authorizer/rbac/rbac.go |
| RBACAuthorizer.RulesFor | plugin/pkg/auth/authorizer/rbac/rbac.go |
| rbac.New | plugin/pkg/auth/authorizer/rbac/rbac.go |
| admission.Interface | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| MutationInterface | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| ValidationInterface | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| Operation (= authorizer.AdmissionOperation) | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| PluginInitializer | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| InitializationValidator | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| ConfigProvider | staging/src/k8s.io/apiserver/pkg/admission/interfaces.go |
| ReadyFunc | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| admission.Handler | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| Handler.Handles | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| NewHandler | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| SetReadyFunc | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| WaitForReady | staging/src/k8s.io/apiserver/pkg/admission/handler.go |
| chainAdmissionHandler | staging/src/k8s.io/apiserver/pkg/admission/chain.go |
| NewChainHandler | staging/src/k8s.io/apiserver/pkg/admission/chain.go |
| chainAdmissionHandler.Admit | staging/src/k8s.io/apiserver/pkg/admission/chain.go |
| chainAdmissionHandler.Validate | staging/src/k8s.io/apiserver/pkg/admission/chain.go |
| chainAdmissionHandler.Handles | staging/src/k8s.io/apiserver/pkg/admission/chain.go |
| admission/audit.go Admit 调用点 | staging/src/k8s.io/apiserver/pkg/admission/audit.go |
| reinvocation.go Admit 调用点 | staging/src/k8s.io/apiserver/pkg/admission/reinvocation.go |
| metrics.go Admit 调用点 | staging/src/k8s.io/apiserver/pkg/admission/metrics/metrics.go |
| MutatingAdmissionWebhook PluginName | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/plugin.go |
| mutating Register | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/plugin.go |
| mutating Plugin | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/plugin.go |
| NewMutatingWebhook | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/plugin.go |
| Plugin.Admit (mutating) | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/plugin.go |
| mutatingDispatcher | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| newMutatingDispatcher | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| versionedAttributeAccessor | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| mutatingDispatcher.Dispatch | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| callAttrMutatingHook | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| webhookAnnotator | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| newWebhookAnnotator | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/mutating/dispatcher.go |
| ValidatingAdmissionWebhook PluginName | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/validating/plugin.go |
| validating Register | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/validating/plugin.go |
| validating Plugin | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/validating/plugin.go |
| NewValidatingAdmissionWebhook | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/validating/plugin.go |
| Plugin.Validate (validating) | staging/src/k8s.io/apiserver/pkg/admission/plugin/webhook/validating/plugin.go |
| NamespaceLifecycle PluginName | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| Lifecycle | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| Lifecycle.Admit | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| NewLifecycle | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| newLifecycleWithClock | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| Lifecycle.SetExternalKubeInformerFactory | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| Lifecycle.SetExternalKubeClientSet | staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go |
| UpdateResource | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| update.WithAudit(admit) | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| update.NewManagedFieldsValidatingAdmissionController | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| update mutatingAdmission.Admit (Create 分支) | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| update mutatingAdmission.Admit (Update 分支) | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| update AdmissionToValidateObjectFunc 调用点 | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/update.go |
| create mutatingAdmission.Admit | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go |
| create AdmissionToValidateObjectFunc 调用点 | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go |
| patch admissionCheck.Admit | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/patch.go |
| rest.go mutatingAdmission.Admit (Connect) | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/rest.go |
| rest.go validatingAdmission.Validate (Connect) | staging/src/k8s.io/apiserver/pkg/endpoints/handlers/rest.go |
| delete mutatingAdmission.Admit | staging/src/k8s.io/apiserver/pkg/registry/rest/delete.go |
| AdmissionToValidateObjectFunc | staging/src/k8s.io/apiserver/pkg/registry/rest/create.go |
| AdmissionToValidateObjectUpdateFunc | staging/src/k8s.io/apiserver/pkg/registry/rest/update.go |
| audit.Sink | staging/src/k8s.io/apiserver/pkg/audit/types.go |
| audit.Backend | staging/src/k8s.io/apiserver/pkg/audit/types.go |
| PolicyRuleEvaluator | staging/src/k8s.io/apiserver/pkg/audit/evaluator.go |
| audit.Union | staging/src/k8s.io/apiserver/pkg/audit/union.go |
| union (audit) | staging/src/k8s.io/apiserver/pkg/audit/union.go |
| union.ProcessEvents | staging/src/k8s.io/apiserver/pkg/audit/union.go |
| union.Run | staging/src/k8s.io/apiserver/pkg/audit/union.go |
| union.Shutdown | staging/src/k8s.io/apiserver/pkg/audit/union.go |
| NewPolicyRuleEvaluator | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| policyRuleEvaluator | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| EvaluatePolicyRule | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| ruleMatches | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| ruleMatchesNonResource | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| ruleMatchesResource | staging/src/k8s.io/apiserver/pkg/audit/policy/checker.go |
| LogRequestMetadata | staging/src/k8s.io/apiserver/pkg/audit/request.go |
| LogImpersonatedUser | staging/src/k8s.io/apiserver/pkg/audit/request.go |
| LogRequestObject | staging/src/k8s.io/apiserver/pkg/audit/request.go |
| LogRequestPatch | staging/src/k8s.io/apiserver/pkg/audit/request.go |
| LogResponseObject | staging/src/k8s.io/apiserver/pkg/audit/request.go |
| WithAudit | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go |
| evaluatePolicyAndCreateAuditEvent | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go |
| writeLatencyToAnnotation | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go |
| decorateResponseWriter | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go |
| auditResponseWriter | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit.go |
| WithAuditInit | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit_init.go |
| withAuditInit | staging/src/k8s.io/apiserver/pkg/endpoints/filters/audit_init.go |
| WithFailedAuthenticationAudit | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authn_audit.go |
| getAuthMethods | staging/src/k8s.io/apiserver/pkg/endpoints/filters/authn_audit.go |
| WithImpersonation | staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go |
| WithConstrainedImpersonation | staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/constrained_impersonation.go |
| userString | staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go |
| unescapeExtraKey | staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go |
| buildImpersonationRequests | staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go |

---

## 未能验证 / 需修正的点（flag）

> ✅ 2026-11-26 刷新：基线已从 `b5bcafc8ce9` 对齐到 `3596eb69d25`，上述「基线不符」已消解；本次逐条回核全部绝对行锚点并修正漂移（见篇首基线说明）。以下 Flag 中标注「未逐行打开/未核实」的项仍为原文记录，未在本次逐一展开。

1. **基线不符**：任务给定 `782c049`，实际 HEAD `b5bcafc8ce9`。全部行号基于后者。若需对齐 `782c049`，本篇行号需重核。
2. **`NewBaseHandlerChain` 不存在**：grep 全仓无此符号；chain 装配入口为 `DefaultBuildHandlerChain` 与 `BuildHandlerChainWithStorageVersionPrecondition`。
3. **`CompletedConfig.New`**：并非以该接收者名定义；实际是包级 `New(name string, delegationTarget ...)`（`config.go:~800` 区），chain wiring 在其内部（`config.go`/`:853`）。任务表述的 "CompletedConfig.New" 应理解为 `Config.Complete → CompletedConfig → New` 这条链。
4. **审计 filter 路径**：`pkg/server/filters/audit.go`、`pkg/server/filters/impersonation.go` 均不存在；正确路径 `pkg/endpoints/filters/audit.go`、`pkg/endpoints/filters/impersonation/impersonation.go`。
5. **RBAC 路径**：`plugin/pkg/authorizer/rbac/` 不存在；正确为 `plugin/pkg/auth/authorizer/rbac/`。
6. **`AuditBackend` / `AuditEvent` 命名**：顶层符号为 `audit.Backend`（`types.go`）与 `auditinternal.Event`；未在 `pkg/audit/` 找到名为 `AuditBackend`/`AuditEvent` 的定义。
7. **`WithConstrainedImpersonation` 定义行（已核实）**：`staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/constrained_impersonation.go:WithConstrainedImpersonation`，签名 `func WithConstrainedImpersonation(handler http.Handler, a authorizer.UnconditionalAuthorizer, s runtime.NegotiatedSerializer) http.Handler`；与 `WithImpersonation` **不同文件**（后者在 `impersonation.go`）。其函数体本篇未逐行展开。
8. **`AuditContext` 结构体**：在 `audit/context.go`，本篇仅引用其方法调用点（`AuditContextFrom`/`Init`/`Enabled`/`ProcessEventStage`/`SetEventResponseStatus`/`GetEventResponseStatus`），未逐行打开该文件核对字段。
9. **`Decision`/`DecisionAllow|Deny|NoOpinion` 常量**、`ConditionsAwareDecisionFromParts` 的定义文件（`authorization/authorizer/evaluate.go` / `conditions.go`）未逐行打开。
10. **`mux.PathRecorderMux.NotFoundHandler`** 的定义（`pkg/server/mux/pathrecorder.go`）未逐行打开，仅引用其调用点 `config.go`。
11. **`RuleAllows`**（RBAC `visit` 内使用）、`admission/plugins.go` 的 `Plugins.Register`、`authorizer/` 与 `policy/validating/` 插件的 `PluginName` 均未逐行核对。
12. **`config.go` 内其余调用点均已用 grep 核实符号存在**（记法迁移后不再钉绝对行号，按符号取行）：`WithTracing`、`WithCORS`、`WithWarningRecorder`、`WithTimeoutForNonLongRunningRequests`、`WithRequestDeadline`、`WithWaitGroup`、`WithCacheControl`、`WithHSTS`、`WithRetryAfter`、`WithHTTPLogging`、`WithLatencyTrackers`、`routine.WithRoutine`、`WithRequestInfo`、`WithRequestReceivedTimestamp`、`WithMuxAndDiscoveryComplete`、`WithPanicRecovery`、`WithAuditInit`、`failedHandler`、`WithFailedAuthenticationAudit`。仍未取单行锚点的区间项：`WithConditionsAwareAuthorization` 分支（`DefaultBuildHandlerChain` 的 feature-gate 分支内，`~1083~1089` 区）、`NewWorkEstimator`（`~1102~1104` 区）、`WithWatchTerminationDuringShutdown`、`WithProbabilisticGoaway`、`loadmetrics.WithEndpointLoadMetrics`。。