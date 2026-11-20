[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# conntrack 与 ServiceCIDR

> **层级**：跨篇机制纵深专题——把 kube-proxy 的 **conntrack 清理**（陈旧连接跟踪项删除）与 **ServiceCIDR 处理**（service 网段变化响应）讲透。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `CleanStaleEntries+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[16-kube-proxy网络代理](../16-kube-proxy网络代理.md) §conntrack 是浓缩版；本专题纵向展开 conntrack 清理算法与 ServiceCIDR 事件链。

---

## 0. 结论速览

### 0.1 conntrack 清理解决什么问题

kube-proxy 改写 Service 流量靠 conntrack（内核连接跟踪）。当 Service 的后端 endpoint 变化（缩容、删 Service），**旧的 conntrack 项可能指向已不存在的 Pod**，导致流量发往死 Pod。conntrack 清理负责删掉这些陈旧项。

### 0.2 ServiceCIDR 解决什么问题

Service ClusterIP 从 ServiceCIDR 网段分配。ServiceCIDR 对象（networking.k8s.io）声明这些网段，kube-proxy 监听其变化以更新路由/规则。

### 0.3 关键结论

1. **conntrack 清理按需触发**：endpoint 缩容/Service 删除后，`CleanStaleEntries` 删掉指向已消失 endpoint 的 conntrack 项。
2. **三层流程**：ListEntries（列全部）→ MatchConntrackFlow（筛陈旧）→ DeleteEntries（删）。
3. **flowFilter 去重**：`flowKey` 构造过滤键，避免重复删除。
4. **ServiceCIDR 是独立 informer**：`ServiceCIDRConfig` 监听 ServiceCIDR 对象变化，回调 proxier。
5. **iptables 模式 OnServiceCIDRsChanged 是 no-op**：iptables 规则按 Service 逐个生成，不依赖 ServiceCIDR 网段（ipvs 模式才用）。

---

## 1. 生产视角：对外行为

### 1.1 conntrack 清理的可见结果

| 场景 | 行为 |
|---|---|
| Service 缩容（删 endpoint） | 清理指向已删 Pod 的 conntrack 项 |
| Service 删除 | 清理该 Service frontend 的所有 conntrack 项 |
| 不清理的后果 | 流量继续发往死 Pod（连接超时/重置） |

### 1.2 可观测信号

- `entriesDeleted` 日志（清理了多少 conntrack 项）。
- 陈旧连接导致的连接超时/重置（未清理时）。

---

## 2. 开发视角：conntrack 清理

### 2.1 `CleanStaleEntries`——清理入口（cleanup.go `CleanStaleEntries+0`，:47）

```text
CleanStaleEntries(ct, ipFamily, svcPortMap, ...)
├── entries = ct.ListEntries(ipFamily)            列全部 conntrack 项   ct.ListEntries+0 (:59)
├── 构造 serving endpoints 集合（空集 = 所有 frontend 流量都陈旧）
├── for svcPortMap 中每个 service:
│    └── 筛出指向已消失 endpoint 的 flow（陈旧）
└── ct.DeleteEntries(ipFamily, staleFlows)        ct.DeleteEntries+0 (:69)
      ↑ 删除陈旧项，记 entriesDeleted
```
**语义：** 只删「指向已不存在的 endpoint」的 conntrack 项——活的连接不动。

### 2.2 conntracker（conntrack.go）

| 方法 | 锚点 | 作用 |
|---|---|---|
| `New` | `New+0`（:50） | 构造 conntracker（netlink handler） |
| `ListEntries` | `ct.ListEntries+0`（:59） | 列指定 ipFamily 的全部 conntrack 项 |
| `DeleteEntries` | `ct.DeleteEntries+0`（:69） | 删除指定 flow 列表 |

### 2.3 flowFilter 去重（filter.go）

- `newFlowFilter+0`（:36）：从已有 flow 构造过滤器。
- `flowKey+0`（:47）：构造 flow 的唯一键（协议+src/dst+port）。
- `MatchConntrackFlow+0`（:61）：判断一个 flow 是否匹配（是否陈旧）。

---

## 3. 开发视角：ServiceCIDR 处理

### 3.1 `ServiceCIDRConfig`——ServiceCIDR informer（config.go）

| 方法 | 锚点 | 作用 |
|---|---|---|
| `NewServiceCIDRConfig` | `NewServiceCIDRConfig+0`（:370） | 构造 ServiceCIDR informer |
| `RegisterEventHandler` | `c.RegisterEventHandler+0`（:397） | 注册 handler（proxier 实现） |
| `Run` | `c.Run+0`（:402） | 启动 informer（`go Run`） |
| `handleServiceCIDREvent` | `c.handleServiceCIDREvent+0`（:413） | 处理 ServiceCIDR 变化 |

### 3.2 proxier 的 ServiceCIDR 响应

`proxier.OnServiceCIDRsChanged`（proxier.go :525-527）：**iptables 模式下是 no-op**（空函数）。因为 iptables 规则按 Service 逐个生成，不依赖 ServiceCIDR 网段聚合。

**ipvs 模式**才用 ServiceCIDR——ipvs 需要知道 service 网段来配置虚拟服务器。

### 3.3 servicecidrs 控制器（控制面侧）

`NewController+0`（servicecidrs_controller.go :71）→ `Run+0`（:137）→ `sync+0`（:294）：控制面维护 ServiceCIDR 对象（默认 ServiceCIDR、动态网段）。这是 apiserver/控制面侧，与 kube-proxy 的 ServiceCIDRConfig（节点侧）对应。

---

## 4. 全链路时序（Service 缩容触发 conntrack 清理）

```text
Service 缩容（删 endpoint）
└── EndpointSlice 变化 → proxier.Sync → syncProxyRules（重建规则）
     └── 旧 endpoint 消失 → 触发 conntrack 清理
          └── CleanStaleEntries (cleanup.go:47)
               ├── ct.ListEntries (conntrack.go:59) 列全部
               ├── flowFilter.MatchConntrackFlow (filter.go:61) 筛指向死 Pod 的
               └── ct.DeleteEntries (conntrack.go:69) 删陈旧项
                    └── 后续流量不再发往死 Pod
```

---

## 5. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| 只删陈旧项 | `CleanStaleEntries+0`（筛指向消失 endpoint） | 活连接不动，只清理死路径 |
| 去重过滤 | `flowKey+0`/`MatchConntrackFlow+0` | 避免重复删除同一 flow |
| ServiceCIDR 独立监听 | `ServiceCIDRConfig.Run+0` | 网段变化（ipvs）及时响应 |
| iptables no-op | `OnServiceCIDRsChanged+0`（:525） | iptables 按 Service 生成，无需网段聚合 |

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- conntrack：`CleanStaleEntries+0`（cleanup.go:47）、`conntracker+0`（conntrack.go:44）、`New+0`（:50）、`ct.ListEntries+0`（:59）、`ct.DeleteEntries+0`（:69）
- filter：`newFlowFilter+0`（filter.go:36）、`flowKey+0`（:47）、`f.MatchConntrackFlow+0`（:61）
- ServiceCIDR：`NewServiceCIDRConfig+0`（config.go:370）、`c.RegisterEventHandler+0`（:397）、`c.Run+0`（:402）、`c.handleServiceCIDREvent+0`（:413）
- 控制面：`NewController+0`（servicecidrs_controller.go:71）、`Run+0`（:137）、`sync+0`（:294）
- proxier：`OnServiceCIDRsChanged+0`（proxier.go:525-527）

### 6.2 生产实测

**未实测** `[待运行验证]`。conntrack 清理实际删除数量、ServiceCIDR 变化响应时序均为代码推断，未在真实节点 `conntrack -L`/观测清理日志验证。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| CleanStaleEntries 全文 | 未逐行读 | :47 起的完整筛选逻辑（serving endpoints 集合构造）未展开 |
| ipvs ServiceCIDR | 未深挖 | ipvs 模式下 ServiceCIDR 网段的具体使用未读 |
| dual-stack | 未深挖 | ipv4/ipv6 双栈 conntrack 清理未展开 |
| sysctls | 未深挖 | `sysctls.go` 的 conntrack sysctl 调整未读 |

### 6.4 修改联动

- 改清理算法 → `CleanStaleEntries`/`MatchConntrackFlow`，同步本文 §2。
- 改 ServiceCIDR 响应 → `ServiceCIDRConfig`/`OnServiceCIDRsChanged`，同步 §3。

**关联文档：** [16-kube-proxy网络代理](../16-kube-proxy网络代理.md)（kube-proxy 全貌）、[kube-proxy规则生成](kube-proxy规则生成.md)（规则生成）、[workqueue机制](workqueue机制.md)（事件驱动）。

[总目录](../README.md)
