[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# aggregator 代理转发

> **层级**：跨篇机制纵深专题——把 **kube-aggregator 的代理转发**讲透：`proxyHandler.ServeHTTP` 全流程（APIService 解析 → 早退守卫 → 后端 endpoint 解析 → 请求改写 → transport 构建 → UpgradeAware 转发 → 错误处理）。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `r.ServeHTTP+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md) 和 [api-server](api-server.md) §6 是浓缩版；本专题纵向展开代理转发的完整语义。

---

## 0. 结论速览

### 0.1 聚合代理解决什么问题

聚合 API（如 metrics-server、自定义 apiserver）的请求由 kube-aggregator **反向代理**到后端 Service。`proxyHandler` 是这条链路的必经点——它把 `/apis/<group>/<version>` 请求转发到注册的 APIService 后端。

### 0.2 关键结论

1. **5 个顺序早退守卫**：ServeHTTP 在真正转发前有 5 个检查，每个对应一种「无法代理」的形态。
2. **local vs 远程**：`handlingInfo.local=true`（APIService 无后端 Service）→ 委托 `localDelegate`（内置处理）；否则代理到远端。
3. **用户身份透传**：转发时用 `NewAuthProxyRoundTripper` 注入原始用户身份（impersonation headers）。
4. **支持 WebSocket/upgrade**：用 `NewUpgradeAwareHandler` 处理协议升级。
5. **transport 复用**：`proxyRoundTripper` 按 APIService 构建一次，复用（不随请求变）。

---

## 1. 生产视角：代理转发对外行为

### 1.1 可见结果

| 形态 | HTTP 状态 |
|---|---|
| APIService 未注册/无 handlingInfo | 委托 localDelegate（404 语义） |
| 后端 Service 不可用 | 503 Service Unavailable |
| transport 构建失败 | 500 |
| 缺用户身份 | 500 |
| 后端正常 | 转发后端响应 |

### 1.2 可观测信号

- `service unavailable` / `missing user` 等错误（proxyError）。
- 后端 Service 的 endpoint 解析失败日志。

---

## 2. 开发视角：`proxyHandler` 与 `proxyHandlingInfo`

### 2.1 `proxyHandler`（handler_proxy.go `proxyHandler+0`，:51）

持有 `localDelegate`（:53，本地 APIService 委托）、`handlingInfo`（atomic.Value，按 APIService 缓存）、`serviceResolver`、`proxyTransportDial` 等。

### 2.2 `proxyHandlingInfo`（:71）——单个 APIService 的运行态

| 字段 | 作用 |
|---|---|
| `local`（:73） | APIService 是否本地满足（无后端 Service） |
| `transportBuildingError`（:81） | transport 构建错误 |
| `proxyRoundTripper`（:83） | 可复用的 transport（不随请求变） |
| `serviceAvailable`（:88） | APIService 后端是否可用 |
| `serviceNamespace/Name/Port` | 后端 Service 定位 |

### 2.3 transport 构建（`BuildTransportConfig`）

`updateAPIService+0`（:215）在 APIService 变更时重建 handlingInfo：`BuildTransportConfig+0`（remote_available_controller.go :163）构造 TLS 配置（ServerName `<svc>.<ns>.svc`、CA/Cert/Key、InsecureSkipTLSVerify），`transport.New` 生成 `proxyRoundTripper`。

---

## 3. 开发视角：`ServeHTTP` 五个早退守卫

### 3.1 `proxyHandler.ServeHTTP`（handler_proxy.go `r.ServeHTTP+0`，:107）

```text
ServeHTTP(w, req)                                          r.ServeHTTP+0
├── 守卫 1：handlingInfo.Load() == nil → localDelegate      （APIService 未加载）
├── 守卫 2：handlingInfo.local → localDelegate              （本地 APIService）
├── 守卫 3：!serviceAvailable → proxyError 503              （后端不可用）
├── 守卫 4：transportBuildingError != nil → proxyError 500  （transport 构建失败）
├── 守卫 5：缺用户身份 → proxyError 500                     （无法透传身份）
├── 解析后端 endpoint：
│     ResolveEndpoint(serviceNamespace, serviceName, servicePort)  → rloc
│     失败 → proxyError 503
├── 构造 location（https + rloc.Host + 原 Path/RawQuery）
├── NewRequestForProxy(location, req)                       改写请求
├── proxyRoundTripper 包装：
│     ├── NewAuthProxyRoundTripper(user, groups, extra)     注入原始用户身份
│     └── tracingWrapper（非 upgrade 时）
└── NewUpgradeAwareHandler(location, proxyRoundTripper, ...) 转发（支持 upgrade）
```

### 3.2 五个早退守卫（精确）

| 守卫 | 条件 | 结果 |
|---|---|---|
| 1 | `handlingInfo.Load() == nil` | `localDelegate.ServeHTTP` |
| 2 | `handlingInfo.local` | `localDelegate.ServeHTTP` |
| 3 | `!handlingInfo.serviceAvailable` | `proxyError 503` |
| 4 | `handlingInfo.transportBuildingError != nil` | `proxyError 500` |
| 5 | 缺用户（`ok=false`） | `proxyError 500` |
| 6（转发前） | `proxyRoundTripper == nil` | `proxyError 404` |

### 3.3 endpoint 解析（`ResolveEndpoint`）

`serviceResolver.ResolveEndpoint(namespace, name, port)`（resolvers.go `aggregatorEndpointRouting.ResolveEndpoint+0` :45 / `aggregatorClusterRouting.ResolveEndpoint+0` :61）：把后端 Service 解析为可拨号的 Host（集群内 vs 集群外两种 routing）。

### 3.4 用户身份透传（`NewAuthProxyRoundTripper`）

转发前用 `transport.NewAuthProxyRoundTripper(user.GetName(), userUID, groups, extra, proxyRoundTripper)` 包装——把原始用户身份作为 impersonation headers 传给后端，使后端 apiserver 看到真实用户（而非 aggregator）。

### 3.5 错误处理（`proxyError` + `responder`）

- `proxyError+0`（:94）：写错误响应（指定 status + message）。
- `responder.Object+0`（:197）/`Error+0`（:201）：UpgradeAware handler 的响应回调，Error 写 503。

---

## 4. 全链路时序（一次聚合 API 请求）

```text
GET /apis/custom.metrics.k8s.io/v1beta1/...
└── aggregator 路由匹配 APIService → proxyHandler.ServeHTTP (handler_proxy.go:107)
     ├── 守卫 1-5（local/不可用/transport 错/缺用户 → 早退）
     ├── ResolveEndpoint (resolvers.go:45) → 后端 Service Host
     ├── 构造 location（https + Host + 原 Path/Query）
     ├── NewRequestForProxy 改写请求
     ├── NewAuthProxyRoundTripper 注入原始用户身份
     └── NewUpgradeAwareHandler.ServeHTTP → 转发到后端 apiserver
          └── 后端响应 → 回写客户端
```

---

## 5. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| local/远程分流 | `r.ServeHTTP+0`（守卫 1-2） | 本地 APIService 不该代理 |
| 后端不可用早退 | 守卫 3（503） | 避免转发到无响应后端 |
| 用户身份透传 | `NewAuthProxyRoundTripper` | 后端看到真实用户（RBAC 正确） |
| transport 复用 | `proxyHandlingInfo.proxyRoundTripper`（:83） | 不每次重建 TLS 连接 |
| 协议升级支持 | `NewUpgradeAwareHandler` | WebSocket/watch 等 upgrade 场景 |

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `proxyHandler+0`（handler_proxy.go:51）、`localDelegate`（:53）、`proxyHandlingInfo`（:71，local :73/transportBuildingError :81/proxyRoundTripper :83/serviceAvailable :88）
- `proxyError+0`（:94）、`r.ServeHTTP+0`（:107）、`r.Object+0`（:197）、`r.Error+0`（:201）、`r.setServiceAvailable+0`（:209）、`r.updateAPIService+0`（:215）
- `aggregatorEndpointRouting.ResolveEndpoint+0`（resolvers.go:45）、`aggregatorClusterRouting.ResolveEndpoint+0`（:61）
- `BuildTransportConfig+0`（remote_available_controller.go:163）

### 6.2 生产实测

**未实测** `[待运行验证]`。五个早退守卫的实际触发、endpoint 解析、用户身份透传、upgrade 转发均为代码推断，未在真实聚合集群抓包验证。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| AddAPIService/RemoveAPIService | 未深挖 | handlingInfo 的注册/注销控制循环未展开 |
| serviceAvailable 判定 | 未深挖 | APIService 可用性（endpoint 就绪）判定细节未读 |
| aggregatorClusterRouting | 未深挖 | 集群外 routing 的拨号细节未读 |
| responder 完整语义 | 未深挖 | Object/Error 回调的完整处理未展开 |

### 6.4 修改联动

- 改代理转发 → `proxyHandler.ServeHTTP`，同步本文 §3。
- 改 transport → `BuildTransportConfig`/`updateAPIService`，同步 §2.3。
- 改 endpoint 解析 → `ResolveEndpoint`，同步 §3.3。

**关联文档：** [api-server](api-server.md) §6（聚合代理浓缩）、[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md)（CRD 侧）。

[总目录](../sourceReader/README.md)
