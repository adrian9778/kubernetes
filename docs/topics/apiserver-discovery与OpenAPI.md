[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# apiserver discovery 与 OpenAPI

> **层级**：跨篇机制纵深专题——把 apiserver 的 **discovery（服务发现）与 OpenAPI 生成**讲透：`/api`/`/apis` discovery 层级、aggregated discovery、OpenAPI v2/v3 生成。
>
> **代码基准**：adrian @ `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。所有「符号+偏移」均在该基准上用 MCP `codegraph_explore` 回核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `NewRootAPIsHandler+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[05-apiserver启动与路由](../sourceReader/05-apiserver启动与路由.md) 和 [17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md) 提到 discovery；本专题纵向展开 discovery 层级与 OpenAPI 生成。

---

## 0. 结论速览

### 0.1 discovery 解决什么问题

客户端（kubectl、client-go）需要知道集群里**有哪些 API group/version/resource**、每个资源的动词、字段。discovery 端点（`/api`、`/apis`、`/apis/<group>`、`/apis/<group>/<version>`）提供这些元信息——是 client-go 构造请求、kubectl 补全的基础。

### 0.2 关键结论

1. **discovery 是层级化的**：`/api`（核心组）+ `/apis`（所有组）→ `/apis/<group>`（组的 version 列表）→ `/apis/<group>/<version>`（version 的资源列表）。
2. **三个 handler 层级**：`rootAPIsHandler`（顶层）→ `APIGroupHandler`（组）→ `APIVersionHandler`（版本+资源）。
3. **aggregated discovery**：聚合所有组/版本的资源列表为一次请求（`resourceDiscoveryManager`），减少客户端往返。
4. **OpenAPI 双版本**：v2（Swagger，`DefaultOpenAPIConfig`）+ v3（`DefaultOpenAPIV3Config`）。
5. **CRD/聚合 API 动态并入 discovery**：CRD 和 APIService 注册后自动出现在 discovery 列表。

---

## 1. 生产视角：discovery 对外行为

### 1.1 discovery 端点层级

| 端点 | 返回 |
|---|---|
| `/api` | 核心组（v1） |
| `/apis` | 所有非核心 APIGroup 列表 |
| `/apis/<group>` | 该组的 version 列表 |
| `/apis/<group>/<version>` | 该 version 的资源列表（名称、动词、kind） |
| `/openapi/v2`、`/openapi/v3` | OpenAPI 文档 |

### 1.2 可观测信号

- `kubectl api-resources`、`kubectl api-versions` 调 discovery。
- client-go 的 `DiscoveryClient`、RESTMapper 依赖 discovery。

---

## 2. 开发视角：discovery 三层 handler

### 2.1 顶层 `/apis`（`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/root.go` · `NewRootAPIsHandler` `+0`）

`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/root.go` · `rootAPIsHandler.ServeHTTP` `+0` 返回 `metav1.APIGroupList`（所有非核心 group）：
```text
ServeHTTP                                            rootAPIsHandler.ServeHTTP+0
├── groupList = APIGroupList{Groups: groupsLocked(ctx, req)}
└── WriteObjectNegotiated(serializer, ..., &groupList)   序列化返回
```

### 2.2 组级 `/apis/<group>`（`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/group.go` · `NewAPIGroupHandler` `+0`）

`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/group.go` · `APIGroupHandler.ServeHTTP` `+0` 返回该组的 version 列表（`APIGroup`）。

### 2.3 版本级 `/apis/<group>/<version>`（`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/version.go` · `NewAPIVersionHandler` `+0`）

`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/version.go` · `APIVersionHandler.ServeHTTP` `+0` 返回该 version 的资源列表（`APIResourceList`：每个资源的 name、singularName、namespaced、kind、verbs）。

**resource 列表来源：** `APIResourceLister`（由 APIInstaller 注册资源时填充，见 [api-server](api-server.md) §2.2）。

### 2.4 aggregated discovery（`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/aggregated/handler.go` · `NewResourceManager` `+0`）

`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/aggregated/handler.go` · `resourceDiscoveryManager.serveHTTP` `+0`：把所有 group/version 的资源列表**聚合为一次请求**返回（`/apis` 带 Accept: `application/json;g=apidiscovery.k8s.io;v=v2`）——减少客户端逐组请求的往返。支持 ETag 缓存（`ServeHTTPWithETag` 包裹，`aggregated/etag.go`）。

---

## 3. 开发视角：OpenAPI 生成

### 3.1 OpenAPI v2（`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIConfig` `+0`）

`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIConfig(getDefinitions, defNamer)`：构造 OpenAPI v2（Swagger）配置。`getDefinitions` 从各 API 类型的 `zz_generated.openapi.go` 取 schema 定义，`defNamer` 生成 schema 名。

### 3.2 OpenAPI v3（`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIV3Config` `+0`）

`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIV3Config`：构造 OpenAPI v3 配置，写入 `Config` 结构体的 `OpenAPIV3Config` 字段（`staging/src/k8s.io/apiserver/pkg/server/config.go` · `Field:Config.OpenAPIV3Config`）。v3 按 group/version 分文件（`/openapi/v3/apis/<group>/<version>`），比 v2 更精确。

### 3.3 CRD 的 OpenAPI（apiextensions）

apiextensions-apiserver 有独立的 OpenAPI 控制器（`pkg/controller/openapi/controller.go`、`openapiv3/controller.go`）：CRD 的 schema 转成 OpenAPI 并发布，使 CR 也出现在 `/openapi/v3`。

---

## 4. 全链路时序（kubectl api-resources）

```text
kubectl api-resources
└── GET /apis（Accept: apidiscovery v2）→ aggregated resourceDiscoveryManager
     │   `staging/src/k8s.io/apiserver/pkg/endpoints/discovery/aggregated/handler.go` · `resourceDiscoveryManager.serveHTTP` `+0`
     └── 聚合所有 group/version 的 APIResourceList → 一次返回
          └── kubectl 解析资源列表（name、verbs、kind、namespaced）
（client-go RESTMapper 也用 discovery 构造 GVK→resource 映射）
```

---

## 5. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| 层级化 discovery | root→group→version 三层 handler（`discovery/root.go` / `group.go` / `version.go`） | 客户端按层级发现 API |
| 聚合 discovery | `staging/src/k8s.io/apiserver/pkg/endpoints/discovery/aggregated/handler.go` · `resourceDiscoveryManager.serveHTTP` `+0` | 减少客户端往返 |
| OpenAPI 双版本 | `staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIConfig` `+0` / `DefaultOpenAPIV3Config` `+0` | v2 兼容 + v3 精确 |
| CRD 动态并入 | apiextensions openapi controller | CR 也出现在 discovery/OpenAPI |

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在基准 `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4` 上用 MCP `codegraph_explore` 回核：
- discovery 顶层 `/apis`：`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/root.go` · `NewRootAPIsHandler` `+0`；`rootAPIsHandler.ServeHTTP` `+0`（返回 `APIGroupList`）
- discovery 组级：`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/group.go` · `NewAPIGroupHandler` `+0`；`APIGroupHandler.ServeHTTP` `+0`（返回 `APIGroup`）
- discovery 版本级：`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/version.go` · `NewAPIVersionHandler` `+0`；`APIVersionHandler.ServeHTTP` `+0`（返回 `APIResourceList`）
- aggregated discovery：`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/aggregated/handler.go` · `NewResourceManager` `+0`；`resourceDiscoveryManager.serveHTTP` `+0`
- OpenAPI v2/v3：`staging/src/k8s.io/apiserver/pkg/server/config.go` · `DefaultOpenAPIConfig` `+0` / `DefaultOpenAPIV3Config` `+0`；`Field:Config.OpenAPIV3Config`
- storageversionhash：`staging/src/k8s.io/apiserver/pkg/endpoints/discovery/storageversionhash.go` · `StorageVersionHash` `+0`

### 6.2 生产实测

**未实测** `[待运行验证]`。discovery 实际返回内容、aggregated discovery 聚合行为、OpenAPI 生成细节均为代码推断，未在真实集群 `curl /apis`/`/openapi/v3` 验证。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| aggregated discovery 缓存 | 未深挖 | ETag/peer 缓存（peer_aggregated_handler.go）细节未展开 |
| OpenAPI schema 合并 | 未深挖 | 多 API group 的 schema 合并（builder/merge.go）未读 |
| storageversionhash | 未深挖 | storage version hash 计算未展开 |
| CRD OpenAPI 发布 | 未深挖 | apiextensions openapi controller 的发布循环未读 |

### 6.4 修改联动

- 改 discovery 层级 → `rootAPIsHandler`/`APIGroupHandler`/`APIVersionHandler`，同步本文 §2。
- 改聚合 discovery → `resourceDiscoveryManager`，同步 §2.4。
- 改 OpenAPI → `DefaultOpenAPIConfig`/`DefaultOpenAPIV3Config`，同步 §3。

**关联文档：** [05-apiserver启动与路由](../sourceReader/05-apiserver启动与路由.md)（路由）、[api-server](api-server.md) §2.3（discovery 浓缩）、[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md)（CRD discovery）。

[总目录](../sourceReader/README.md)
