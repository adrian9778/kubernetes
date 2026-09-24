[上一篇](16-kube-proxy网络代理.md) · [总目录](README.md)

# CRD 扩展与聚合层
> **层级**：第四层——函数级源码解析。
> **范围**：CRD（apiextensions-apiserver）的动态注册与懒分发；kube-aggregator 的 APIService 注册与后端代理。

## 第一部分：CRD

### 1. 模块级
| 项 | 内容 |
|---|---|
| 模块 | `[staging] k8s.io/apiextensions-apiserver` |
| 装配 | `pkg/apiserver/apiserver.go`（CustomResourceDefinitions、completedConfig.New） |
| 动态处理 | `pkg/apiserver/customresource_handler.go`（crdHandler，懒安装与请求分发，本分支无 CustomResourceHandler 接口） |
| 存储 | `pkg/registry/customresourcedefinition/`（CRD 对象自身）、`pkg/registry/customresource/`（CR 实例） |
| 关键机制 | CR 实例**不**经 go-restful 路由；crdHandler 注册在 NonGoRestfulMux 的 `/apis` 前缀，请求时才按 CRD 构造 serving info |

### 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `CustomResourceDefinitions` | `apiserver.go:108` | 内嵌 GenericAPIServer 与 Informers；CRD server 主体 |
| `Config/CompletedConfig` | :93/:103 | 装配配置 |
| `crdHandler` | `customresource_handler.go:91` | 持有 `customStorage atomic.Value`（crdStorageMap）、crdLister、restOptionsGetter、admission、converterFactory |
| `crdInfo` | :136 | 单个 CRD 的运行态：spec、acceptedNames、`storages map[string]CustomResourceStorage`、requestScopes、storageVersion |
| `crdStorageMap` | :167 | `map[types.UID]*crdInfo` |
| `CustomResourceStorage` | `pkg/registry/customresource/etcd.go:39` | 一个版本的 CR 存储集合：CustomResource/Status/Scale 三个 REST |
| `REST`（customresource） | `etcd.go:105` | 内嵌 genericregistry.Store，CR 实例的实际存储 |

### 3. 装配函数级

### 函数：completedConfig.New
**源码：** `apiserver.go:128`
**执行流程：**
1. `customresourcedefinition.NewREST`（:150）+ NewStatusREST 放入 storage map
2. `GenericAPIServer.InstallAPIGroup` 注册 `customresourcedefinitions` 资源本身（:162）
3. `NewCustomResourceDefinitionHandler`（:188）并注册到 NonGoRestfulMux 的 `/apis`、`/apis/`（:208 ～ +209）
4. `NewDiscoveryController`（:216）

### 4. 动态安装与请求分发函数级

### 函数：NewCustomResourceDefinitionHandler
**源码：** `customresource_handler.go:169`
**注册：** `crdInformer.AddEventHandler`（:201）→ createCustomResourceDefinition:459 / updateCustomResourceDefinition:478；spec 或 acceptedNames 变化时 `removeStorage_locked`（:519，tearDown:565）使对应 crdInfo 缓存失效。

### 函数：(*crdHandler).ServeHTTP
**源码：** `customresource_handler.go:228`
**职责：** 全部 CR 实例请求的统一入口。
**执行流程：**
```text
ServeHTTP (+0)
├── 1. crdLister.Get(resource+"."+group) 找 CRD
├── 2. getOrCreateServingInfoFor（:616）：
│      ├─ crdStorageMap 命中 → 直接返回
│      └─ 未命中：建 crdInfo，每版本调 customresource.NewStorage（:855），
│         注入 structural schema、converter、scope、协商序列化，写回 atomic map
├── 3. serveResource（:368）按 verb 分派：
│      handlers.GetResource/ListResource/CreateResource/UpdateResource
│      /PatchResource/DeleteResource
└── 4. 子资源：serveStatus（:418）/serveScale（:438）
```
**懒安装语义：** CRD 对象存在后，第一次 CR 请求才触发版本存储构造；CRD 更新则先失效缓存，下次请求重建。

### 5. CR 实例存储
| 函数/类型 | 文件行 | 职责 |
|---|---|---|
| `NewStorage` | `pkg/registry/customresource/etcd.go:45` | 构造一个版本的 CustomResourceStorage（本分支无 NewResourceHandler） |
| `customResourceStrategy` | `strategy.go` | CR 的默认化与声明式校验 |
| `REST` | etcd.go:105 | 复用 generic Store，CR 的增删改查行为与内置资源一致 |
| `StatusREST/ScaleREST` | :119/:150 | 子资源 |

## 第二部分：聚合层

### 6. 模块级
| 项 | 内容 |
|---|---|
| 模块 | `[staging] k8s.io/kube-aggregator` |
| 装配 | `pkg/apiserver/apiserver.go`（APIAggregator） |
| 控制器 | `apiservice_controller.go`（APIService 增删同步） |
| 代理 | `handler_proxy.go`（把请求转发给后端 Service） |
| registry | `pkg/registry/apiservice/`（本分支实际路径，非 apiregistration/apiservice） |

### 7. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `APIAggregator` | `apiserver.go:148` | 内嵌 GenericAPIServer、delegateHandler、`proxyHandlers map[string]*proxyHandler`、handledGroupVersions、lister、serviceResolver、proxyTransportDial |
| `APIServiceRegistrationController` | `apiservice_controller.go:45` | watch APIService，调 APIHandlerManager（Add/RemoveAPIService） |
| `proxyHandler` | `handler_proxy.go:51` | 单个 group/version 的代理：localDelegate、proxyTransportDial、handlingInfo atomic |
| `proxyHandlingInfo` | :71 | local 标记、TLS 配置、proxyRoundTripper、后端 service/namespace/port、serviceAvailable |
| `ServiceResolver` | `resolvers.go:27` | 解析后端 Service 的 endpoint（EndpointSlice/ClusterIP/Loopback 三种实现） |

### 8. 装配函数级

### 函数：completedConfig.NewWithDelegate
**源码：** `apiserver.go:212`
**执行流程：**
1. `apiservicerest.NewRESTStorage`（:266）+ InstallAPIGroup（:267）
2. `NewAPIServiceRegistrationController`（:303）
3. 装配 proxyTransportDial（egress dialer 或 ExtraConfig，:244/:247）

### 函数：NewAPIServiceRegistrationController
**源码：** `apiservice_controller.go:60`
**sync（:82）：** NotFound → RemoveAPIService；存在 → AddAPIService；标准 workqueue worker 模式（:136）。

### 9. 注册与代理函数级

### 函数：(*APIAggregator).AddAPIService
**源码：** `apiserver.go:530`
**执行流程：**
1. 已存在 → 对应 proxyHandler.updateAPIService
2. 新建：proxyPath = `/apis/<group>/<version>`（legacy `v1.` 特例为 `/api`），构造 proxyHandler
3. 注册到 NonGoRestfulMux；记录 handledGroupVersions

### 函数：(*APIAggregator).RemoveAPIService
**源码：** `apiserver.go:614`
**动作：** NonGoRestfulMux.Unregister + 删除 proxyHandlers 条目。

### 函数：(*proxyHandler).ServeHTTP
**源码：** `handler_proxy.go:107`
**执行流程：**
```text
ServeHTTP (+0)
├── 1. handlingInfo.local → localDelegate（APIService 指向本 apiserver）
├── 2. serviceAvailable=false → 503
├── 3. serviceResolver.ResolveEndpoint(ns, name, port) 取后端地址
├── 4. 构造 https location + NewRequestForProxy
├── 5. transport.NewAuthProxyRoundTripper（TLS 凭据 + 认证头透传）
└── 6. NewUpgradeAwareHandler(location, roundTripper, true, upgrade, responder)
         .ServeHTTP（支持 WebSocket/SPDY 升级与流式响应）
```
`NewUpgradeAwareHandler` 定义于 `[staging] k8s.io/apiserver/pkg/util/proxy`。

### 函数：(*proxyHandler).updateAPIService
**源码：** `handler_proxy.go:215`
**职责：** `remote.BuildTransportConfig`（`pkg/controllers/status/remote/remote_available_controller.go:163`，TLS ServerName `<svc>.<ns>.svc`，CA/Cert/Key）→ `transport.New(...)` 构造 RoundTripper，更新 handlingInfo。

### APIService registry
| 函数/文件 | 行 | 职责 |
|---|---|---|
| `NewREST/NewStatusREST` | `pkg/registry/apiservice/etcd/etcd.go:42/130` | APIService 对象存储 |
| `NewRESTStorage` | `pkg/registry/apiservice/rest/storage_apiservice.go:33` | APIGroupInfo 装配 |

## 10. 两种扩展机制对比
| 机制 | 对象 | 请求落点 | 后端 |
|---|---|---|---|
| CRD | CustomResourceDefinition | crdHandler 懒分发，与内置资源同一存储框架 | etcd（apiserver 自身） |
| Aggregation | APIService | proxyHandler 反向代理 | 用户注册的 Service（自定义 API server） |

## 11. 调用关系总览
```text
CRD：
APIServer 启动 → InstallAPIGroup(customresourcedefinitions)
NonGoRestfulMux:/apis → crdHandler.ServeHTTP
 └── getOrCreateServingInfoFor → customresource.NewStorage
      └── serveResource → handlers.* → generic Store → etcd

Aggregator：
APIService informer → RegistrationController.sync
 └── AddAPIService → NonGoRestfulMux 注册 proxyHandler
请求 → proxyHandler.ServeHTTP
 ├── local → delegate
 └── remote → ResolveEndpoint → UpgradeAwareHandler → 后端 Service
```

[上一篇](16-kube-proxy网络代理.md) · [总目录](README.md)
