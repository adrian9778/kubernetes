[上一篇](16-kube-proxy网络代理.md) · [总目录](README.md)

# CRD 扩展与聚合层
> **层级**：第四层——函数级源码解析。
> **范围**：CRD（apiextensions-apiserver）的动态注册与懒分发；kube-aggregator 的 APIService 注册与后端代理。
> **代码基准**：master@`8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，工作区干净），Go 1.27.4。
> **记法**：源码锚点一律「符号+偏移」，函数定义行 = +0；行号随基准变化时只改偏移。

## 第一部分：生产视角（对外行为与可见结果）

### 1. CRD：用户眼中的效果
- 用户创建 `CustomResourceDefinition` 后，`/apis/<group>/<version>/<resource>` 即出现一组可增删改查的 REST 端点，行为与内置资源一致（同样走 generic Store、支持 list/watch/status/scale 子资源、结构化 schema 校验与默认值注入）。
- CR 请求**不**经 go-restful 路由：`crdHandler` 注册在 NonGoRestfulMux 的 `/apis` 与 `/apis/` 前缀（`c.New+80` ～ +81），请求到达时才按 CRD 构造 serving info，即「懒安装」。
- 对用户透明：CRD 更新（spec 或 acceptedNames 变化）后，旧版本存储缓存先失效，下次请求自动重建；CRD 删除后，对应存储随 tearDown 释放。
- 边界行为：请求的 scope 与 CRD 不符、版本未 served、NamespaceAccepted/Established 条件不满足时，请求落到 delegate（404 语义）；`crdLister.Get` 未命中同样委托 delegate。

### 2. 聚合层：用户眼中的效果
- 用户创建 `APIService` 对象后，kube-aggregator 在 NonGoRestfulMux 上注册 `/apis/<group>/<version>`（legacy `v1.` 特例为 `/api`），把该 group/version 的请求**反向代理**到用户注册的 Service 指向的自定义 API server。
- `APIService.spec.service == nil` 时视为 `local`，请求转给 delegateHandler（指向本 apiserver 的内置处理）。
- 后端不可达（`serviceAvailable=false`）返回 503；后端地址由 `ServiceResolver` 从 Service/EndpointSlice 解析（ClusterIP 或 Loopback 三种实现）。
- 代理支持 WebSocket/SPDY 升级与流式响应，凭据透传（认证头 + 客户端证书）。

## 第二部分：开发视角——CRD

### 3. 模块级
| 项 | 内容 |
|---|---|
| 模块 | `[staging] k8s.io/apiextensions-apiserver` |
| 装配 | `pkg/apiserver/apiserver.go`（CustomResourceDefinitions、completedConfig.New） |
| 动态处理 | `pkg/apiserver/customresource_handler.go`（crdHandler，懒安装与请求分发；本分支无 `CustomResourceHandler` 接口，全仓 grep 无） |
| 存储（CRD 对象自身） | `pkg/registry/customresourcedefinition/` |
| 存储（CR 实例 + strategy） | `staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresource/` |
| 关键机制 | CR 实例**不**经 go-restful 路由；crdHandler 注册在 NonGoRestfulMux 的 `/apis` 前缀，请求时才按 CRD 构造 serving info |

### 4. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `CustomResourceDefinitions` | `pkg/apiserver/apiserver.go CustomResourceDefinitions+0`（:108） | 内嵌 GenericAPIServer 与 Informers；CRD server 主体 |
| `Config/CompletedConfig` | :93/:103 | 装配配置 |
| `crdHandler` | `pkg/apiserver/customresource_handler.go crdHandler+0`（:92） | 持有 `customStorage atomic.Value`（crdStorageMap）、crdLister、restOptionsGetter、admission、converterFactory |
| `crdInfo` | :137 | 单个 CRD 的运行态：spec、acceptedNames、`storages map[string]CustomResourceStorage`、requestScopes、storageVersion、deprecated、warnings、waitGroup |
| `crdStorageMap` | :168 | `map[types.UID]*crdInfo` |
| `CustomResourceStorage` | `pkg/registry/customresource/etcd.go CustomResourceStorage+0`（:39） | 一个版本的 CR 存储集合：CustomResource/Status/Scale 三个 REST |
| `REST`（customresource） | `etcd.go REST+0`（:105） | 内嵌 genericregistry.Store，CR 实例的实际存储 |

### 5. 装配函数级

#### 函数：completedConfig.New
**源码：** `pkg/apiserver/apiserver.go c.New+0`（:128）
**执行流程：**
1. `customresourcedefinition.NewREST`（:151）+ NewStatusREST 放入 storage map
2. `GenericAPIServer.InstallAPIGroup` 注册 `customresourcedefinitions` 资源本身（:162）
3. `NewCustomResourceDefinitionHandler`（:188）并注册到 NonGoRestfulMux 的 `/apis`、`/apis/`（:208 ～ +209）
4. `NewDiscoveryController`（:216）

### 6. 动态安装与请求分发函数级

#### 函数：NewCustomResourceDefinitionHandler
**源码：** `pkg/apiserver/customresource_handler.go NewCustomResourceDefinitionHandler+0`（:170）
**注册：** `crdInformer.AddEventHandler`（:202）→ createCustomResourceDefinition:460 / updateCustomResourceDefinition:479 / removeDeadStorage；spec 或 acceptedNames 变化时 `removeStorage_locked`（:520，tearDown:566）使对应 crdInfo 缓存失效。

#### 函数：(*crdHandler).ServeHTTP
**源码：** `pkg/apiserver/customresource_handler.go r.ServeHTTP+0`（:229）
**职责：** 全部 CR 实例请求的统一入口。
**执行流程：**
```text
ServeHTTP (+0)
├── 1. RequestInfoFrom 取请求信息；非资源请求走 /apis/<group>[/<version>] discovery 或 delegate（+2 ～ +25）
├── 2. crdLister.Get(resource+"."+group) 找 CRD（+29）
├── 3. scope/版本/条件校验：不符或未就绪 → delegate（+45 ～ +69）
├── 4. getOrCreateServingInfoFor(crd.UID, crd.Name)（+72）：
│      ├─ crdStorageMap 命中 → 直接返回
│      └─ 未命中：建 crdInfo，每版本调 customresource.NewStorage，
│         注入 structural schema、converter、scope、协商序列化，写回 atomic map
├── 5. 警告与 verb 准备（+90 ～ +107）
├── 6. GetSubresourcesForVersion 取子资源定义（+109）
└── 7. 子资源分派 switch（+118）：
       ├─ serveStatus（+120）
       ├─ serveScale（+122）
       ├─ serveResource（+124）
       └─ default → 404 MethodNotSupported
```
**懒安装语义：** CRD 对象存在后，第一次 CR 请求才触发版本存储构造；CRD 更新则先失效缓存，下次请求重建。

#### 函数：(*crdHandler).serveResource
**源码：** `pkg/apiserver/customresource_handler.go r.serveResource+0`（:369）
**流程：** 取 `requestScopes[version]` 与 `storages[version].CustomResource`，按 verb 分派：get（+5）、list（+7）、watch（+10）、create（+13，HA 下 Established 后 2s 内创建会 sleep 2s）、update（+27）、patch（+29）、delete、deletecollection。

#### 函数：(*crdHandler).serveStatus / serveScale
**源码：** :419 / :439
**流程：** 分别取 `statusRequestScopes`/`scaleRequestScopes` 与 `Status`/`Scale` 存储，按 get/update/patch 分派。

#### 函数：(*crdHandler).getOrCreateServingInfoFor
**源码：** `pkg/apiserver/customresource_handler.go r.getOrCreateServingInfoFor+0`（:617）
**流程：** `customStorage.Load`（+1）命中即返回；未命中加锁（+6），锁内重新 `crdLister.Get`（+13）双检，再按版本构造 structural schema（PruneDefaults 注入默认值）、`customresource.NewStorage`、converter、scope 并写回 atomic map。

#### CR 创建请求流解剖（第二层补充视角）

CR（Custom Resource）实例的创建与内置资源走**不同的路由机制**：CR 不经 go-restful，而是 crdHandler 注册在 NonGoRestfulMux 的 `/apis` 前缀。本节走一个 `POST /apis/example.com/v1/namespaces/default/widgets` 请求，看它如何被 crdHandler 处理。

**第一步：crdHandler.ServeHTTP 入口分流（`customresource_handler.go r.ServeHTTP+0`，:229）**
```text
ServeHTTP (+0)
├── RequestInfoFrom(ctx) 无 → InternalError
├── !IsResourceRequest（discovery 路径）
│    ├── pathParts == 3 → versionDiscoveryHandler
│    ├── pathParts == 2 → groupDiscoveryHandler
│    └── 否则 → delegate.ServeHTTP（交给 kubeAPIServer）
├── crdName = resource + "." + group（如 widgets.example.com）
├── crdLister.Get(crdName) NotFound → delegate.ServeHTTP（非 CRD）
└── 命中 CRD → getOrCreateServingInfoFor(crd.UID, crd.Name)
```

**第二步：getOrCreateServingInfoFor 懒构造存储（:617）**
1. `customStorage.Load()`（atomic.Value）命中即返回（快路径）。
2. 未命中加锁，锁内**重新** `crdLister.Get` 双检（防与 updateCustomResourceDefinition 竞争）。
3. 按版本遍历 `crd.Spec.Versions`，`GetSchemaForVersion` 取 schema → `structuralschema.NewStructural` 构造结构化 schema → `PruneDefaults` 注入默认值。
4. `customresource.NewStorage` 构造该版本的 storage，建 converter、requestScope，写回 atomic map。

**懒构造语义：** CRD 对象存在后，**第一次 CR 请求**才触发版本存储构造；CRD 更新则先失效缓存，下次请求重建。

**第三步：serveResource 按 verb 分派（:369）**
```text
serveResource（:369）
├── 取 requestScopes[version] 与 storages[version].CustomResource
└── switch requestInfo.Verb:
     ├── get       → handlers.GetResource(storage, requestScope)
     ├── list      → handlers.ListResource(storage, storage, requestScope, forceWatch, minRequestTimeout)
     ├── watch     → handlers.ListResource(...)（同 list，forceWatch=true）
     ├── create    → handlers.CreateResource(storage, requestScope, a)
     │                （HA 下 Established 后 2s 内创建会 sleep 2s，等 CRD 传播）
     ├── update    → handlers.UpdateResource(storage, requestScope, admission)
     ├── patch     → handlers.PatchResource(storage, requestScope, a, supportedTypes)
     ├── delete    → handlers.DeleteResource(storage, allowsOptions, requestScope, admission)
     └── deletecollection → handlers.DeleteCollection(...)
```

**第四步：CR 创建落到 etcd**
- `handlers.CreateResource` → `customresource` 的 `REST.Create`（内嵌 `genericregistry.Store`）→ 与内置资源同一存储框架 → etcd。
- CR 实例的 key 形如 `/registry/example.com/widgets/default/<name>`，与内置资源同一 etcd。

#### CR 创建 vs 内置资源创建对照

| 维度 | 内置资源（如 Pod） | CR 实例（如 Widget） |
|---|---|---|
| 路由 | go-restful（installer.go 注册） | crdHandler（NonGoRestfulMux `/apis` 前缀） |
| 存储构造 | 启动时静态注册 | 首次请求懒构造（getOrCreateServingInfoFor） |
| schema 校验 | 各资源的 strategy.Validate | structural schema（CRD spec.versions[].schema） |
| 存储 | generic Store | 同 generic Store（customresource.REST 内嵌） |
| HA 创建保护 | 无 | Established 后 2s 内 sleep 2s |

**设计要点（源码可证）：**
1. **懒构造存储**：CRD 存储不启动时预建，而是首次请求时按需构造（getOrCreateServingInfoFor 双检），避免为未使用的 CRD 浪费资源。
2. **HA 建立保护**：create verb 在 CRD `Established` 后 2s 内会 sleep 2s（serveResource +13 ～ +19），等 CRD 传播到所有 apiserver 实例，避免部分实例还没准备好。
3. **与内置资源同一存储框架**：CR 实例复用 `genericregistry.Store`，使 CR 享有与内置资源相同的 watch、分页、校验能力。
4. **NotFound 回落 delegate**：crdLister 查不到 CRD 时交给 delegate（kubeAPIServer），使非 CRD 的 `/apis` 请求能继续走内置路由。

### 7. CR 实例存储
| 函数/类型 | 文件 | 职责 |
|---|---|---|
| `NewStorage` | `staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresource/etcd.go NewStorage+0`（:45） | 构造一个版本的 CustomResourceStorage（本分支无 `NewResourceHandler`，全仓 grep 无） |
| `customResourceStrategy` | `strategy.go customResourceStrategy+0`（:54） | CR 的默认化与声明式校验（含 structural schema、CEL 校验） |
| `REST` | etcd.go `REST+0`（:105） | 复用 generic Store，CR 的增删改查行为与内置资源一致 |
| `StatusREST/ScaleREST` | :119/:150 | 子资源 |

## 第三部分：开发视角——聚合层

### 8. 模块级
| 项 | 内容 |
|---|---|
| 模块 | `[staging] k8s.io/kube-aggregator` |
| 装配 | `pkg/apiserver/apiserver.go`（APIAggregator） |
| 控制器 | `pkg/apiserver/apiservice_controller.go`（APIService 增删同步） |
| 代理 | `pkg/apiserver/handler_proxy.go`（把请求转发给后端 Service） |
| registry | `staging/src/k8s.io/kube-aggregator/pkg/registry/apiservice/`（实际路径；`apiregistration/apiservice` 全仓无） |

### 9. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `APIAggregator` | `pkg/apiserver/apiserver.go APIAggregator+0`（:148） | 内嵌 GenericAPIServer、delegateHandler、`proxyHandlers map[string]*proxyHandler`、handledGroupVersions、lister、serviceResolver、proxyTransportDial |
| `APIServiceRegistrationController` | `pkg/apiserver/apiservice_controller.go APIServiceRegistrationController+0`（:45） | watch APIService，调 APIHandlerManager（Add/RemoveAPIService） |
| `proxyHandler` | `pkg/apiserver/handler_proxy.go proxyHandler+0`（:51） | 单个 group/version 的代理：localDelegate、proxyTransportDial、handlingInfo atomic |
| `proxyHandlingInfo` | :71 | local 标记、TLS 配置、proxyRoundTripper、后端 service/namespace/port、serviceAvailable |
| `ServiceResolver` | `pkg/apiserver/resolvers.go ServiceResolver+0`（:27） | 解析后端 Service 的 endpoint（EndpointSlice/ClusterIP/Loopback 三种实现） |

### 10. 装配函数级

#### 函数：completedConfig.NewWithDelegate
**源码：** `pkg/apiserver/apiserver.go c.NewWithDelegate+0`（:212）
**执行流程：**
1. `apiservicerest.NewRESTStorage`（:266）+ InstallAPIGroup（:267）
2. `NewAPIServiceRegistrationController`（:303）
3. 装配 proxyTransportDial（egress dialer 或 ExtraConfig，:244/:247）

#### 函数：NewAPIServiceRegistrationController
**源码：** `pkg/apiserver/apiservice_controller.go NewAPIServiceRegistrationController+0`（:60）
**sync（:82）：** NotFound → RemoveAPIService；存在 → AddAPIService；标准 workqueue worker 模式（Run :96，单 worker :131）。

### 11. 注册与代理函数级

#### 函数：(*APIAggregator).AddAPIService
**源码：** `pkg/apiserver/apiserver.go s.AddAPIService+0`（:530）
**执行流程：**
1. 已存在 → 对应 proxyHandler.updateAPIService（:534）
2. 新建：proxyPath = `/apis/<group>/<version>`（legacy `v1.` 特例为 `/api`），构造 proxyHandler（:557）
3. 注册到 NonGoRestfulMux（Handle + UnlistedHandlePrefix，:577/578）；记录 handledGroupVersions（:608）

#### 函数：(*APIAggregator).RemoveAPIService
**源码：** `pkg/apiserver/apiserver.go s.RemoveAPIService+0`（:614）
**动作：** NonGoRestfulMux.Unregister(proxyPath 及 /) + 删除 proxyHandlers 条目；group 下无剩余版本时注销 groupPath。

#### 函数：(*proxyHandler).ServeHTTP
**源码：** `pkg/apiserver/handler_proxy.go r.ServeHTTP+0`（:107）
**执行流程：**
```text
ServeHTTP (+0)
├── 1. handlingInfo.Load（+1）；nil → localDelegate
├── 2. handlingInfo.local（+7）→ localDelegate（+3，nil 则 404）
├── 3. serviceAvailable=false（+16）→ 503 proxyError（+17）
├── 4. 校验 transportBuildingError、user（+21 ～ +29）
├── 5. serviceResolver.ResolveEndpoint(ns, name, port) 取后端地址（+35）
├── 6. 构造 https location + NewRequestForProxy（+40 ～ +46）
├── 7. NewAuthProxyRoundTripper（+61，认证头透传 + 客户端证书）
└── 8. NewUpgradeAwareHandler（+75，upgrade 走 SetAuthProxyHeaders 直连）
         .ServeHTTP（支持 WebSocket/SPDY 升级与流式响应）
```
`NewUpgradeAwareHandler` 定义于 `[staging] k8s.io/apimachinery/pkg/util/proxy/upgradeaware.go NewUpgradeAwareHandler+0`（:181）（handler_proxy.go:25 经 `k8s.io/apimachinery/pkg/util/proxy` 导入）。

#### ServeHTTP 五个早退守卫与升级分支深度拆解

`proxyHandler.ServeHTTP`（`handler_proxy.go r.ServeHTTP+0`，:107）是聚合层请求的必经点。它在真正转发前有 **5 个顺序早退守卫**，每个对应一种「无法代理」的形态：

| 序 | 守卫条件 | 动作 | 含义 |
|---|---|---|---|
| 1 | `handlingInfo.Load()` 返回 nil | `localDelegate.ServeHTTP` | APIService 尚未注册运行态 → 交给本地委托（内置 API 组） |
| 2 | `handlingInfo.local == true` | `localDelegate`（nil 则 404） | APIService 标记为本地（本 apiserver 自己提供）→ 本地处理 |
| 3 | `!handlingInfo.serviceAvailable` | 503 `proxyError("service unavailable")` | 后端 APIService 的 Available condition 为假 |
| 4 | `transportBuildingError != nil` | 500 `proxyError` | 构造到后端的 RoundTripper 失败（TLS 配置等） |
| 5 | `UserFrom(req.Context())` 不存在 | 500 `proxyError("missing user")` | 认证中间件未注入 user（不应发生） |

**转发构造（守卫全过之后）：**
1. **`serviceResolver.ResolveEndpoint(ns, name, port)`**（+35 附近）：把 APIService 指向的 Service 解析为具体后端地址 `rloc`；失败 → 503。这是聚合层「APIService → 后端 Service endpoint」的唯一寻址步骤。
2. **构造 `location`**（+42 ～ +47）：`scheme=https`、`Host=rloc.Host`、`Path=req.URL.Path`、`RawQuery=req.URL.Query().Encode()`——**保留原请求路径与 query，仅替换 host**。
3. `apiserverproxyutil.NewRequestForProxy(location, req)`（+49）造新请求，`defer cancelFn()`。
4. `proxyRoundTripper == nil` → 404（RoundTripper 未就绪）。
5. **认证身份透传**：`transport.NewAuthProxyRoundTripper(user.GetName(), userUID, user.GetGroups(), user.GetExtra(), proxyRoundTripper)`——把认证得到的 user 身份写成透传头，使后端聚合 apiserver 能识别原始用户。`RemoteRequestHeaderUID` feature 开启时额外带 `userUID`。
6. **非升级请求**包一层 `tracing.WrapperFor(tracerProvider)` 做链路追踪。

**升级（WebSocket/SPDY）分支（关键差异）：**
- `upgrade := httpstream.IsUpgradeRequest(req)` 判定是否升级请求。
- **升级请求不走 `proxyRoundTripper`**：`NewUpgradeAwareHandler` 的升级路径是**直连拨号**、绕过 RoundTripper，因此必须在构造请求时就用 `transport.SetAuthProxyHeaders(newReq, ...)` 把 user 身份头**预先**写入请求，而非依赖 RoundTripper 注入。
- 非升级请求则由 `proxyRoundTripper`（含 AuthProxy 头注入 + tracing）处理。
- `rejectForwardingRedirects` 开启时 `handler.RejectForwardingRedirects = true`，阻止后端返回的重定向被转发给客户端。
- `utilflowcontrol.RequestDelegated(req.Context())`：向 P&F 归还并发额度——请求已从聚合层委托给后端 apiserver，不再占用本层的并发配额。

**设计要点（源码可证）：**
1. **local 与 remote 统一入口**：同一个 `proxyHandler` 既处理「本地内置 API 组」（`localDelegate`）也处理「远端聚合 API」（反向代理），由 `handlingInfo.local` 区分，使 APIService 的注册/摘除对路由层透明。
2. **运行态原子保存、请求路径无锁**：`handlingInfo` 经 `atomic.Value` 保存（`proxyHandlingInfo`：local/serviceAvailable/transportBuildingError/proxyRoundTripper），`updateAPIService` 原子替换整个结构，ServeHTTP 只 `Load` 一次——避免读写锁开销。
3. **身份透传而非重新认证**：聚合层不重新认证，而是把 apiserver 认证出的 user 身份经 `NewAuthProxyRoundTripper`/`SetAuthProxyHeaders` 透传给后端，保持端到端身份一致。
4. **升级与非升级双路径**：WebSocket/SPDY 升级必须绕过 RoundTripper 直连，故身份头需提前写入请求；这是聚合层最易出错的分支。

#### 函数：(*proxyHandler).updateAPIService
**源码：** `r.updateAPIService+0`
**职责：** `BuildTransportConfig+0`（TLS ServerName `<svc>.<ns>.svc`，CA/Cert/Key，InsecureSkipTLSVerify）→ `transport.New(transportConfig)`（:237）构造 RoundTripper，更新 handlingInfo（:241）。

### 12. APIService registry
| 函数/文件 | 行 | 职责 |
|---|---|---|
| `NewREST/NewStatusREST` | `NewREST+0`/`NewStatusREST+0` | APIService 对象存储 |
| `NewRESTStorage` | `NewRESTStorage+0` | APIGroupInfo 装配 |

## 第四部分：两种扩展机制对比

### 13. 机制对比
| 机制 | 对象 | 请求落点 | 后端 |
|---|---|---|---|
| CRD | CustomResourceDefinition | crdHandler 懒分发，与内置资源同一存储框架 | etcd（apiserver 自身） |
| Aggregation | APIService | proxyHandler 反向代理 | 用户注册的 Service（自定义 API server） |

## 第五部分：调用关系总览

### 14. 调用关系
```text
CRD：
APIServer 启动 → InstallAPIGroup(customresourcedefinitions)
NonGoRestfulMux:/apis → crdHandler.ServeHTTP
  ├── crdLister.Get → getOrCreateServingInfoFor → customresource.NewStorage
  │     └── (每版本注入 schema/converter/scope)
  └── serveResource/serveStatus/serveScale → handlers.* → generic Store → etcd

Aggregator：
APIService informer → RegistrationController.sync
  └── AddAPIService → NonGoRestfulMux 注册 proxyHandler
请求 → proxyHandler.ServeHTTP
  ├── local → delegateHandler
  └── remote → ResolveEndpoint → NewAuthProxyRoundTripper → UpgradeAwareHandler → 后端 Service
```

## 第六部分：设计意图与验证

### 15. 设计意图
- **懒安装的动机**：CRD 是运行时注册的资源，不能在启动时静态构造全部版本存储；只有第一次 CR 请求才按需建存储，避免 CRD 数量多时启动开销过大。`atomic.Value` 承载 `crdStorageMap` 保证读路径无锁，写路径在 `customStorageLock` 下双检，与 CRD 更新事件去重。
- **CR 与内置资源同构**：CR 复用 `genericregistry.Store` + `customResourceStrategy`，因此 get/list/watch/create/update/patch/delete 的行为、分页、watch 语义与内置资源一致，仅 schema/校验来源不同（CRD 的 structural schema + CEL）。
- **聚合层的设计**：`proxyHandler` 把「APIService 对象 → 后端 Service」的映射做成单 handler，`proxyHandlingInfo` 以 atomic 保存运行态（local/服务可用性/roundTripper），更新时原子替换，请求路径无锁。
- **验证**：本基准锚点已由 `docs/sourceReader/.progress/evidence/17.md` 逐项核对；customresource_handler.go 全文件 +1（头部新增一行），其余聚合层与 registry 零漂移。未确认项：无。

[上一篇](16-kube-proxy网络代理.md) · [总目录](README.md)
