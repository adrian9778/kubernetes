[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# CRD 建立与懒分发

> **层级**：跨篇机制纵深专题——把 **CRD（apiextensions-apiserver）** 的动态注册与**懒分发**讲透：crdHandler 懒安装、请求时构造 serving info、establish 控制器、CRD 清理 finalizer。
>
> **代码基准**：adrian @ `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `r.getOrCreateServingInfoFor+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md) 是浓缩版；本专题纵向展开 CRD 懒分发与 establish 语义。聚合层代理见 [API Server 全景](api-server.md) §6。

---

## 0. 结论速览

### 0.1 CRD 解决什么问题

CRD 让用户**不写代码**就能给集群加自定义资源（Custom Resource, CR）。用户定义一个 CRD（声明 group/version/kind/schema），apiserver 就能 serve 这个资源的 CRUD——**动态扩展 API**。

### 0.2 关键结论

1. **懒分发（lazy serving）**：CR 请求**不走 go-restful 路由**，`crdHandler` 注册在 `NonGoRestfulMux` 的 `/apis` 前缀；请求到达时才按 CRD 构造 serving info——即「懒安装」。
2. **serving info 缓存**：每个 CRD 的运行态（`crdInfo`，含各版本 storage）按 UID 缓存在 `atomic.Value`（`crdStorageMap`），首次请求构造，之后复用。
3. **CRD 变更失效缓存**：CRD spec 变化 → 旧 storage 缓存失效 → 下次请求重建。
4. **establish 控制器**：CRD 创建后由 establish 控制器置 `Established` 条件（表示可服务）。
5. **CRD 清理 finalizer**：CRD 删除时加 `CustomResourceCleanupFinalizer`，确保其 CR 实例被清理后才真正删除 CRD。

### 0.3 CR vs CRD 请求路径对比

| | 内置资源（Pod） | 自定义资源（CR） |
|---|---|---|
| 路由 | go-restful（`APIInstaller` 注册） | `NonGoRestfulMux` `/apis` 前缀 → `crdHandler` |
| handler | 编译期固定 | 请求时按 CRD 动态构造 |
| 存储 | 内置 etcd storage | `customresource.REST`（包装 genericregistry.Store） |

---

## 1. 生产视角：用户眼中的效果

### 1.1 CRD 对外行为

- 创建 CRD 后，`kubectl get <plural>` 可用（CR 请求透明处理）。
- CRD 更新（spec/acceptedNames 变化）后旧存储缓存失效，下次请求自动重建。
- CRD 删除后对应存储随 tearDown 释放。

### 1.2 边界行为

- 请求 scope 与 CRD 不符、版本未 served、Established 条件不满足 → 请求落到 **delegate**（404 语义）。
- `crdLister.Get` 未命中 → 同样委托 delegate。

---

## 2. 开发视角：crdHandler 懒分发

### 2.1 注册（apiextensions-apiserver apiserver.go）

`CustomResourceDefinitions`（`staging/src/k8s.io/apiextensions-apiserver/pkg/apiserver/apiserver.go` · `CustomResourceDefinitions` `+0`）是 CRD server 主体。`New` 内把 `crdHandler` 注册到 `NonGoRestfulMux`：
```text
s.GenericAPIServer.Handler.NonGoRestfulMux.Handle("/apis", crdHandler)   staging/src/k8s.io/apiextensions-apiserver/pkg/apiserver/apiserver.go · c.New +80
```
**关键：** CR 走 `NonGoRestfulMux`（不是 go-restful），因为 CRD 是运行时动态的，无法在编译期注册 go-restful 路由。

### 2.2 `crdHandler`（customresource_handler.go `crdHandler+0`）

持有 `customStorage atomic.Value`（`crdStorageMap`，UID→`crdInfo` 缓存）、`crdLister`、`restOptionsGetter`、`admission`、`converterFactory`、`delegate`（`+13`，落到内置 handler/404）。

### 2.3 `ServeHTTP`——请求分发（customresource_handler.go `r.ServeHTTP+0`）

```text
ServeHTTP(w, req)                                    r.ServeHTTP+0
├── 解析 RequestInfo（group/version/resource）
├── crdLister.Get(name) 找 CRD
│     └── 未命中 → delegate.ServeHTTP（404）
├── 版本未 served / scope 不符 → delegate
└── getOrCreateServingInfoFor(uid, name)            r.getOrCreateServingInfoFor+0
     └── serveResource(...)                          r.serveResource+0
          └── 按 HTTP verb 分发到 CR storage 的 REST 方法
```

### 2.4 `getOrCreateServingInfoFor`——懒安装 + 缓存（`r.getOrCreateServingInfoFor+0`）

```text
getOrCreateServingInfoFor(uid, name)                 r.getOrCreateServingInfoFor+0
├── storageMap = customStorage.Load().(crdStorageMap)   (+13)  atomic.Value 读缓存
├── 缓存命中 → 返回 crdInfo
└── 未命中（首次/失效）→ 构造：
     ├── 为每个 served version 建 storage（customresource.NewStorage）
     ├── 组装 crdInfo{spec, storages, requestScopes, storageVersion, ...}
     └── customStorage.Store(新 map)   原子更新缓存
```
**懒安装语义：** CRD 创建后**不立即**构造 storage；第一次 CR 请求到来时才构造并缓存。CRD 变更时旧缓存失效，下次请求重建。

### 2.5 `serveResource`——分发到 CR storage（`r.serveResource+0`）

按请求 verb（GET/POST/PUT/DELETE/LIST/WATCH）分发到对应版本的 `CustomResourceStorage`（`CustomResource`/`Status`/`Scale` 三个 REST）。

---

## 3. 开发视角：CR 存储（customresource.REST）

### 3.1 `NewStorage`（`staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresource/etcd.go` · `NewStorage` `+0`）

构造一个版本的 CR 存储集合 `CustomResourceStorage`：
- `CustomResource`（`REST`，同文件 · `REST` `+0`）：内嵌 `genericregistry.Store`，CR 实例的实际 etcd 存储。
- `Status`/`Scale`：status/scale 子资源的 REST。

### 3.2 CR 实例存储

`REST` 包装 `genericregistry.Store`——CR 实例像内置资源一样存 etcd，但 schema 校验用 CRD 定义的 OpenAPIV3Schema。

---

## 4. 开发视角：establish 控制器

### 4.1 `EstablishingController`（establishing_controller.go）

`NewEstablishingController`（`establishing_controller.go` · `NewEstablishingController` `+0`）→ `ec.RunWithContext`（`ec.RunWithContext` `+0`）→ `ec.sync`（`ec.sync` `+0`）。

### 4.2 `sync`——置 Established 条件（`ec.sync+0`，:127）

```text
sync(ctx, key)                                       ec.sync+0
├── 取 CRD；已 Established → 返回
├── 检查 conversion webhook CABundle（无效则 Established=false + reason）
├── 构造 Established condition = True
└── crdClient.CustomResourceDefinitions().UpdateStatus(...)   写 Established 条件
```
**意义：** `Established=True` 表示该 CRD 可被 apiserver serve。用户/控制器可据此判断 CRD 是否 ready。

---

## 5. 开发视角：CRD 清理 finalizer

### 5.1 CRD 删除与 finalizer（`staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresourcedefinition/etcd.go`）

CRD 删除时加 `CustomResourceCleanupFinalizer`，确保其 CR 实例先被清理：
```text
Delete(...)                                           r.Delete
├── 首次删除请求 → 加 CustomResourceCleanupFinalizer   r.Delete +34
├── CRDHasFinalizer(existingCRD, CustomResourceCleanupFinalizer)?   r.Delete +62
│     └── 没有则追加 finalizer
└── 等 CR 实例清理完 → 摘 finalizer → CRD 真正删除
```

**意义：** 防止 CRD 删了但其 CR 实例成孤儿残留。

---

## 6. 全链路时序（创建 CRD → 第一次 CR 请求）

```text
用户创建 CRD
├── CRD 存 etcd（customresourcedefinition registry）
├── establish 控制器 sync (establishing_controller.go · ec.sync +0) → 置 Established=True
└── 第一次 CR 请求 GET /apis/<group>/<version>/<plural>
     └── NonGoRestfulMux /apis → crdHandler.ServeHTTP (customresource_handler.go · r.ServeHTTP +0)
          ├── crdLister.Get → 找到 CRD
          └── getOrCreateServingInfoFor (r.getOrCreateServingInfoFor +0)
               ├── 缓存未命中 → customresource.NewStorage (customresource/etcd.go · NewStorage +0) 建 storage
               ├── 缓存 Store（atomic.Value）
               └── serveResource (r.serveResource +0) → 分发到 CR storage REST → etcd
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 上复核：
- `CustomResourceDefinitions`（`staging/src/k8s.io/apiextensions-apiserver/pkg/apiserver/apiserver.go` · `CustomResourceDefinitions` `+0`）、`c.New`（同文件 · `c.New` `+80`，crdHandler 注册 `/apis`）
- `crdHandler`（`customresource_handler.go` · `crdHandler` `+0`）、`crdInfo`（同文件 `crdInfo` `+0`）、`r.ServeHTTP`（`r.ServeHTTP` `+0`）、`r.serveResource`（`r.serveResource` `+0`）、`r.getOrCreateServingInfoFor`（`r.getOrCreateServingInfoFor` `+0`，缓存 Load `+13`、Store `+30`）
- `NewStorage`（`staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresource/etcd.go` · `NewStorage` `+0`）、`REST`（同文件 · `REST` `+0`）
- `NewEstablishingController`（`establishing_controller.go` · `NewEstablishingController` `+0`）、`ec.RunWithContext`（`ec.RunWithContext` `+0`）、`ec.sync`（`ec.sync` `+0`）
- CRD 清理 finalizer：`r.Delete`（`staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresourcedefinition/etcd.go` · `r.Delete` `+34`、`+62`，`CustomResourceCleanupFinalizer`）

### 7.2 生产实测

**未实测** `[待运行验证]`。懒安装缓存失效时机、establish 实际时序、CRD 删除清理窗口均为代码推断，未在真实集群观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| conversion webhook | 未深挖 | `webhook_converter.go` 的多版本转换未展开 |
| CRD validation schema | 未深挖 | OpenAPIV3Schema 校验细节未读 |
| subresource（status/scale） | 未深挖 | Status/Scale REST 的具体处理未展开 |
| customresource_discovery | 未深挖 | CRD 的 discovery（/apis 列表）生成未读 |
| 聚合层 APIService | 见 api-server 专题 | APIService 代理在本专题不覆盖 |

### 7.4 修改联动

- 改懒分发 → `crdHandler.ServeHTTP`/`getOrCreateServingInfoFor`，同步本文 §2。
- 改 establish → `EstablishingController.sync`，同步 §4。
- 改 CRD 清理 → `Delete` finalizer 逻辑，同步 §5。

**关联文档：** [17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md)（浓缩版）、[api-server](api-server.md)（三层委托链与聚合代理）、[garbage-collector依赖图与级联删除](garbage-collector依赖图与级联删除.md)（CRD 资源 GC）。

[总目录](../sourceReader/README.md)
