[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# kube-proxy 规则生成

> **层级**：跨篇机制纵深专题——把 kube-proxy 的**规则生成**讲透：Service/EndpointSlice 事件接入、iptables 三级链（KUBE-SERVICES/SVC/SEP）生成、ipvs/nftables 模式定位、原子下发。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `proxier.syncProxyRules+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[16-kube-proxy网络代理](../sourceReader/16-kube-proxy网络代理.md) §4 是浓缩版；本专题纵向展开规则生成的链结构与原子下发语义。

---

## 0. 结论速览

### 0.1 kube-proxy 是配置面，不是数据面

kube-proxy 是 DaemonSet，每节点一个。它**只做配置面**：把 Service/Endpoint 变化翻译成节点内核规则。数据面流量**不经过** kube-proxy 进程——规则下发后，内核直接 DNAT 转发到 Pod。

### 0.2 iptables 三级链

| 链 | 作用 |
|---|---|
| `KUBE-SERVICES` | 入口：按 ServiceIP:Port 分发到 `KUBE-SVC-*` |
| `KUBE-SVC-<hash>` | 一个 ServicePort 一条：按概率分发到各 `KUBE-SEP-*` |
| `KUBE-SEP-<hash>` | 一个 endpoint 一条：DNAT 到具体 Pod IP:Port |

辅助链：`KUBE-NODEPORTS`（NodePort 入口）、`KUBE-POSTROUTING`（SNAT/MASQ）、`KUBE-MARK-MASQ`（打 masquerade 标记）、`KUBE-FW-*`（ExternalIP/LoadBalancer 防火墙）。

### 0.3 关键结论

1. **全量重建**：每次 Sync 不是增量改规则，而是**全量重建**所有链，`iptables-restore` **原子下发**（避免中间态）。
2. **概率负载均衡**：`KUBE-SVC-*` 链里各 endpoint 按统计概率（`computeProbability`）跳转，实现负载均衡。
3. **事件驱动 + 限频**：Service/EndpointSlice 变化只更新内存 changes 缓存，由 `BoundedFrequencyRunner` 限频触发 Sync。
4. **三种模式同构**：iptables/ipvs/nftables 三个 Proxier 实现同一 `Provider` 接口，数据流同构。

---

## 1. 生产视角：可见结果

### 1.1 可观测判据

- iptables 模式：`iptables-save -t nat | grep KUBE-` 见三级链。
- ipvs 模式：`ipvsadm -Ln` 见 VirtualServer/RealServer。
- nftables 模式：`nft list ruleset` 见规则集。

### 1.2 可见行为

Service ClusterIP/NodePort/ExternalIP/LoadBalancer 流量被 DNAT 到后端 Pod；`sessionAffinity`、`externalTrafficPolicy=Local` 影响规则。

---

## 2. 开发视角：事件接入

### 2.1 Proxier 创建（cmd/kube-proxy/app/server_linux.go）

按模式创建 Proxier：`iptables.NewProxier+0`（:165）/ `ipvs.NewProxier+0`（:211）/ `nftables.NewProxier+0`（:248）。三者在 `metaproxier/meta_proxier.go` `metaProxier+0`（:26）做双栈包装（ipv4/ipv6）。

### 2.2 事件回调（只更新内存缓存）

iptables `Proxier`（proxier.go `Proxier+0`，:127）实现 Service/EndpointSlice handler，**回调只改内存 changes 缓存，不直接改规则**：

| 回调 | 锚点 | 动作 |
|---|---|---|
| `OnServiceAdd/Update/Delete` | `proxier.OnServiceAdd+0`（:448）等 | 更新 serviceChanges |
| `OnEndpointSliceAdd/Update/Delete` | `proxier.OnEndpointSliceAdd+0`（:481）等 | 更新 endpointsChanges |
| `OnServiceSynced`/`OnEndpointSlicesSynced` | :469/:505 | 标记初次同步完成 |

### 2.3 限频触发（`BoundedFrequencyRunner`）

`NewBoundedFrequencyRunner+0`（runner/bounded_frequency_runner.go :62）把 `Sync` 包成限频执行器：事件触发 `Run`（:149），但有 `minInterval` 下限；`Loop`（:89）周期兜底（`maxInterval`）。**目的：** 频繁变化时不会每次都重建规则，合并成一次。

---

## 3. 开发视角：规则生成（iptables `syncProxyRules`）

### 3.1 `Sync` / `SyncLoop`（proxier.go `proxier.Sync+0`，:413 / `proxier.SyncLoop+0`，:422）

`SyncLoop` 起 goroutine 调 `syncRunner.Loop`；`Sync` 触发一次重建（经 runner 限频）。

### 3.2 `syncProxyRules`——全量重建（proxier.go `proxier.syncProxyRules+0`，:625）

```text
syncProxyRules()                                          proxier.syncProxyRules+0 (:625)
├── EnsureChain + jump 规则（PREROUTING→KUBE-SERVICES 等）  (+65 附近)
│     ↑ jump 规则不进 iptables-restore（注释：避免每次 restore）
├── 初始化 natRules/filterRules LineBuffer
├── KUBE-MARK-MASQ / KUBE-POSTROUTING 基础规则              natRules.Write (:743-764)
├── for each ServicePort（servicePortMap）:
│    ├── 生成 KUBE-SVC-<hash> 链规则（按概率跳各 SEP）       natRules.Write (:928/:952/:978/:1020)
│    ├── ClusterIP/ExternalIP/NodePort/LB 各自入口规则
│    └── writeServiceToEndpointRules(...)                  proxier.writeServiceToEndpointRules+0 (:1434)
│          └── 生成 KUBE-SEP-<hash> 链：DNAT 到 Pod IP:Port
└── iptables-restore 原子下发全部 natRules + filterRules
```

### 3.3 链名与概率

| 函数 | 锚点 | 作用 |
|---|---|---|
| `portProtoHash` | `portProtoHash+0`（:533） | ServicePort → hash（生成 SVC/SEP 链名后缀） |
| `serviceFirewallChainName` | `serviceFirewallChainName+0`（:563） | `KUBE-FW-*`（ExternalIP/LB） |
| `servicePortEndpointChainName` | `servicePortEndpointChainName+0`（:577） | `KUBE-SEP-*` |
| `computeProbability`/`proxier.probability` | :390/:405 | 计算 KUBE-SVC 链里跳各 SEP 的概率 |

**概率负载均衡：** `KUBE-SVC-*` 链里有 N 条跳转规则，第 i 条以概率 `1/(N-i+1)` 跳到第 i 个 SEP，最后一条 `Accept`——实现统计均匀分发。

### 3.4 DNAT 与 MASQ

- ClusterIP 流量：`KUBE-SERVICES` → `KUBE-SVC-*` → `KUBE-SEP-*` → DNAT 到 Pod IP:Port。
- 离开节点的流量：`KUBE-POSTROUTING` + `KUBE-MARK-MASQ` 做 SNAT/MASQ（保证回程路由）。
- `externalTrafficPolicy=Local`：只转发到本节点 endpoint，否则 MASQ。

---

## 4. ipvs / nftables 模式定位

| 模式 | 实现 | 数据面 |
|---|---|---|
| **iptables** | iptables/proxier.go | 内核 iptables 链线性遍历 |
| **ipvs** | ipvs/proxier.go（`Proxier+0` :148） | 内核 IPVS 哈希表（VS/RS），性能更好 |
| **nftables** | nftables/proxier.go（`Proxier+0` :146） | nftables 规则集（结构同 iptables 三级链） |

三者实现同一 `Provider` 接口（pkg/proxy/types.go:28），数据流同构：事件 → changes 缓存 → Sync → 全量重建 → 原子下发。

---

## 5. 全链路时序（一次 Endpoint 变化）

```text
EndpointSlice 变化
└── informer → proxier.OnEndpointSliceUpdate (proxier.go:489)
     └── 更新 endpointsChanges（内存）
          └── BoundedFrequencyRunner 触发 Sync (proxier.go:413)
               └── syncProxyRules (proxier.go:625)
                    ├── 重建 KUBE-SERVICES/KUBE-SVC-*/KUBE-SEP-* 全部链
                    ├── writeServiceToEndpointRules (:1434) 生成 DNAT 规则
                    └── iptables-restore 原子下发
                         └── 内核按新规则 DNAT 流量到 Pod
```

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `Proxier+0`（proxier.go:127）、`NewProxier+0`（:209）、`proxier.Sync+0`（:413）、`proxier.SyncLoop+0`（:422）、`proxier.syncProxyRules+0`（:625）
- 事件回调：`OnServiceAdd+0`（:448）、`OnEndpointSliceAdd+0`（:481）、`OnServiceSynced+0`（:469）、`OnEndpointSlicesSynced+0`（:505）
- 链名/概率：`portProtoHash+0`（:533）、`serviceFirewallChainName+0`（:563）、`servicePortEndpointChainName+0`（:577）、`computeProbability`（:390）、`proxier.probability+0`（:405）
- 规则写出：`proxier.writeServiceToEndpointRules+0`（:1434）、natRules.Write（:743-764/:928-1020）
- runner：`NewBoundedFrequencyRunner+0`（runner/bounded_frequency_runner.go:62）、`Loop+0`（:89）、`Run+0`（:149）
- ipvs/nftables：`ipvs.Proxier+0`（ipvs/proxier.go:148）、`nftables.Proxier+0`（nftables/proxier.go:146）、`metaProxier+0`（metaproxier/meta_proxier.go:26）

### 6.2 生产实测

**未实测** `[待运行验证]`。三级链实际结构、概率分发实际分布、iptables-restore 原子性、Sync 限频实际间隔均为代码推断，未在真实节点 `iptables-save`/`ipvsadm` 验证。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| syncProxyRules 全文 | 未逐行读 | :625-1434 的完整规则生成逻辑（各 Service 类型分支）未逐行展开 |
| sessionAffinity | 未深挖 | sticky session 的 `KUBE-SEP` recent 模块规则未读 |
| conntrack 清理 | 未深挖 | `conntrack/` 的连接跟踪清理未展开 |
| healthcheck nodePort | 未深挖 | `healthcheck/` 的健康检查 nodePort 未读 |
| ipvs/nftables 规则生成 | 未深挖 | 只讲同构定位，具体规则生成未展开 |

### 6.4 修改联动

- 改链结构 → `syncProxyRules`/`writeServiceToEndpointRules`，同步本文 §3。
- 改负载均衡 → `computeProbability`/`probability`，同步 §3.3。
- 改限频 → `BoundedFrequencyRunner`，同步 §2.3。

**关联文档：** [16-kube-proxy网络代理](../sourceReader/16-kube-proxy网络代理.md)（kube-proxy 全貌）、[11-informer与workqueue机制](../sourceReader/11-informer与workqueue机制.md)（informer 事件接入）、[workqueue机制](workqueue机制.md)（背压范式）。

[总目录](../sourceReader/README.md)
