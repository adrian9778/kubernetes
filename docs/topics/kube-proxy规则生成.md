[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# kube-proxy 规则生成

> **层级**：跨篇机制纵深专题——把 kube-proxy 的**规则生成**讲透：Service/EndpointSlice 事件接入、iptables 三级链（KUBE-SERVICES/SVC/SEP）生成、ipvs/nftables 模式定位、原子下发。
>
> **代码基准**：adrian @ `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：完整文件路径 + 符号 + 偏移，形如 `` `pkg/proxy/iptables/proxier.go` · `proxier.syncProxyRules` `+0` ``。`+0` = 定义行（`func`/`type` 关键字行），`+N` = 符号体内第 N 行；偏移均落在符号体内。行号由 sr_tools.py 的 offset 子命令实时获取，本文不写绝对行号。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`；解析不了标 `[源码未确认]`。
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

辅助链常量集中定义在 `pkg/proxy/iptables/proxier.go` 顶部的 const 块（`kubeServicesChain` 等，整块为 const 组，未逐项建锚）：`KUBE-NODEPORTS`（NodePort 入口）、`KUBE-POSTROUTING`（SNAT/MASQ）、`KUBE-MARK-MASQ`（打 masquerade 标记）、`KUBE-PROXY-FIREWALL` / `KUBE-FW-*`（ExternalIP/LoadBalancer 防火墙）、`KUBE-FORWARD`、`KUBE-EXTERNAL-SERVICES`。

### 0.3 关键结论

1. **全量重建为主**：iptables/ipvs 每次 Sync 全量重建所有链，`iptables-restore` **原子下发**（避免中间态）。
2. **nftables 已改为「全量 + 增量」双轨**：nftables 的 `syncProxyRules` 按 `doFullSync` 决定走全量还是部分同步（`needFullSync` / `FullSyncPeriod` 到期触发全量），并靠 nftables 原生事务（`knftables.Transaction`）原子提交——这与 iptables 的「每次全量 restore」是实质差异，是本次漂移修正的重点。
3. **负载均衡实现不同**：iptables 用**统计概率**（`computeProbability` → `1/(N-i+1)`）；nftables 用内核 **`numgen random mod N`** 做随机分发（`writeServiceToEndpointDNATs`）。
4. **事件驱动 + 限频**：Service/EndpointSlice 变化只更新内存 changes 缓存，由 `BoundedFrequencyRunner` 限频触发 Sync。
5. **三种模式同构**：iptables/ipvs/nftables/winkernel 四个 Proxier 实现同一 `Provider` 接口，事件→缓存→Sync 的骨架同构，但规则下发的具体机制各异。

### 0.4 一句话总结

**生产侧**：Service/Endpoint 一变 → 节点内核规则被重建 → 后续流量在内核里被 DNAT 到健康 Pod，kube-proxy 进程本身不在转发路径上。**开发侧**：变化先进内存 changes 缓存，限频器合并成一次 Sync，Sync 里全量（或 nftables 部分同步）重算规则并原子下发。

---

## 1. 生产视角：谁触发、流量怎么走、看到什么

### 1.1 触发源（谁在哪动了一下）

| 生产动作 | 触发的 kube-proxy 事件 |
|---|---|
| `kubectl apply` 一个 Service（增/改/删 ClusterIP、NodePort、ExternalIP） | Service informer 回调 |
| `kubectl scale` Deployment、Pod 起伏、就绪探针翻转 | EndpointSlice informer 回调 |
| 后端 Pod 全部不就绪 | 该 Service 的 endpoint 列表清空（规则里保留入口但无 DNAT 目标） |
| 节点 topology 标签变化 | `OnTopologyChange`（nftables 侧会置 `needFullSync`） |
| ServiceCIDR 变化 | `OnServiceCIDRsChanged` |

### 1.2 流量真实流程（iptables 模式，代码推断）

```text
外部/集群内客户端发包
  → 命中节点内核 nat 表 PREROUTING → 跳 KUBE-SERVICES
  → 按 dst IP:Port vmap 到 KUBE-SVC-<hash>          （ServicePort 入口）
  → KUBE-SVC-<hash> 按概率跳某个 KUBE-SEP-<hash>     （负载均衡）
  → KUBE-SEP-<hash> DNAT 到 Pod IP:Port              （真实后端）
  → 离开节点时 KUBE-POSTROUTING + KUBE-MARK-MASQ 做 MASQ/SNAT（保证回程）
```

- ClusterIP / NodePort / ExternalIP / LoadBalancer 各自在 `KUBE-SERVICES`（或 `KUBE-NODEPORTS`、`KUBE-FW-*`）里挂入口规则。
- `externalTrafficPolicy=Local`：只转发到本节点 endpoint，否则 MASQ。
- `sessionAffinity`：靠 `KUBE-SEP` 上的 recent/affinity 规则粘住同一客户端（详见 §6.3 缺口）。

### 1.3 可观测判据（可见结果）

| 模式 | 验证命令 | 应看到 |
|---|---|---|
| iptables | `iptables-save -t nat \| grep KUBE-` | 三级链 `KUBE-SERVICES` / `KUBE-SVC-*` / `KUBE-SEP-*` |
| ipvs | `ipvsadm -Ln` | VirtualServer / RealServer 表 |
| nftables | `nft list ruleset` | kube-proxy 表内的 base chain、jump chain、set/map（`numgen random` 规则） |

`[待运行验证]` 以上命令输出、三级链实际结构、概率分发实际分布、restore/事务原子性、Sync 限频实际间隔均为**代码推断**，未在真实节点实测。

---

## 2. 开发视角：事件接入与限频

### 2.1 Proxier 创建（`cmd/kube-proxy/app/server_linux.go`）

按模式创建 Proxier，调用点在 `createProxier`：

| 模式 | 构造锚点 |
|---|---|
| iptables 单栈 | `iptables.NewProxier`（调用点在 `createProxier`）→ `` `pkg/proxy/iptables/proxier.go` · `NewProxier` `+0` `` |
| iptables 双栈 | `iptables.NewDualStackProxier`（调用点在 `createProxier`） |
| ipvs 单栈 | `ipvs.NewProxier`（调用点在 `createProxier`）→ `` `pkg/proxy/ipvs/proxier.go` · `NewProxier` `+0` `` |
| ipvs 双栈 | `ipvs.NewDualStackProxier`（调用点在 `createProxier`） |
| nftables 单栈 | `nftables.NewProxier`（调用点在 `createProxier`）→ `` `pkg/proxy/nftables/proxier.go` · `NewProxier` `+0` `` |
| nftables 双栈 | `nftables.NewDualStackProxier`（调用点在 `createProxier`） |

> 调用点行号仅标明「在哪调」，符号定义以锚点为准。双栈包装见 `` `pkg/proxy/metaproxier/meta_proxier.go` · `metaProxier` `+0` ``（ipv4/ipv6 各一个 Proxier，按包的 IP family 分发）。

每个 `NewProxier` 内部都会 `runner.NewBoundedFrequencyRunner("sync-runner", proxier.syncProxyRules, minSyncPeriod, syncPeriod, syncPeriod)` 装上限频器（iptables 实例见 `` `pkg/proxy/iptables/proxier.go` · `NewProxier` `+0` `` 体内）。

### 2.2 事件回调（只更新内存缓存，不改规则）

以 iptables Proxier 为例（`` `pkg/proxy/iptables/proxier.go` · `Proxier` `+0` ``）。**回调只改内存 changes 缓存，不直接碰内核规则**；只有缓存真的发生变化且 Proxier 已初始化，才调 `Sync()` 触发限频重建：

| 回调 | 锚点 | 动作 |
|---|---|---|
| `OnServiceAdd` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnServiceAdd` `+0` `` | `serviceChanges.Update` |
| `OnServiceUpdate` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnServiceUpdate` `+0` `` | `serviceChanges.Update` |
| `OnServiceDelete` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnServiceDelete` `+0` `` | `serviceChanges.Update` |
| `OnServiceSynced` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnServiceSynced` `+0` `` | 标记 servicesSynced |
| `OnEndpointSliceAdd` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnEndpointSliceAdd` `+0` `` | `endpointsChanges.EndpointSliceUpdate` |
| `OnEndpointSlicesSynced` | `` `pkg/proxy/iptables/proxier.go` · `proxier.OnEndpointSlicesSynced` `+0` `` | 标记 endpointSlicesSynced + 首次同步 |

nftables 侧同构（`` `pkg/proxy/nftables/proxier.go` · `proxier.OnServiceAdd` `+0` ``、`` `pkg/proxy/nftables/proxier.go` · `proxier.OnEndpointSliceAdd` `+0` ``），并在 `` `pkg/proxy/nftables/proxier.go` · `proxier.OnEndpointSlicesSynced` `+0` `` 里**无条件直接调一次 `syncProxyRules()`**（生命周期仅一次）。nftables 还额外实现 `OnTopologyChange`（置 `needFullSync`）与 `OnServiceCIDRsChanged`。

### 2.3 限频触发（`BoundedFrequencyRunner`）

`BoundedFrequencyRunner` 的真实位置是 **`pkg/proxy/runner/bounded_frequency_runner.go`**（kube-proxy 自带副本），**不在 client-go workqueue**：

| 符号 | 锚点 | 作用 |
|---|---|---|
| 类型 | `` `pkg/proxy/runner/bounded_frequency_runner.go` · `BoundedFrequencyRunner` `+0` `` | 持有 minInterval/retryInterval/maxInterval |
| 构造 | `` `pkg/proxy/runner/bounded_frequency_runner.go` · `NewBoundedFrequencyRunner` `+0` `` | `maxInterval < minInterval` 直接 panic |
| 周期兜底 | `` `pkg/proxy/runner/bounded_frequency_runner.go` · `Loop` `+0` `` | goroutine 内跑，`maxInterval` 到期必跑一次；`fn` 返回 error 则按 `retryInterval` 重试 |
| 事件触发 | `` `pkg/proxy/runner/bounded_frequency_runner.go` · `Run` `+0` `` | 往容量为 1 的 channel 投一个信号（已有待处理信号则忽略），合并突发 |

**目的**：频繁变化时不会每次都重建规则，合并成一次；同时保证最长 `maxInterval` 内至少同步一次。`fn` 失败（如 restore 报错）会以 `retryInterval` 重试。

---

## 3. 开发视角：规则生成（iptables `syncProxyRules`）

### 3.1 `Sync` / `SyncLoop`

- `` `pkg/proxy/iptables/proxier.go` · `proxier.Sync` `+0` ``：触发一次重建（经 runner 限频）；先更新 healthz「已排队」时间戳。
- `` `pkg/proxy/iptables/proxier.go` · `proxier.SyncLoop` `+0` ``：起 goroutine 调 `syncRunner.Loop(stopCh)`，做周期兜底。

### 3.2 `syncProxyRules`——全量重建

`` `pkg/proxy/iptables/proxier.go` · `proxier.syncProxyRules` `+0` ``（体较大，到 `writeServiceToEndpointRules` 之前结束）。

```text
proxier.syncProxyRules()                                   (+0)
├── 加锁；isInitialized() 未就绪直接返回                    (+5 附近)
├── doFullSync = needFullSync || 距上次全量 > FullSyncPeriod
│     且 非大集群模式(largeClusterMode)                     (+15 附近)
├── svcPortMap.Update(serviceChanges)                      刷 service 缓存
├── endpointsMap.Update(endpointsChanges)                  刷 endpoint 缓存
├── 重建 jump 规则（PREROUTING/OUTPUT/FORWARD→KUBE-SERVICES 等）
│     ↑ jump 规则用 iptables 命令单独维护，不进 iptables-restore
├── natRules/filterRules LineBuffer 写基础链规则
│     KUBE-POSTROUTING / KUBE-MARK-MASQ                     natRules.Write
├── for each ServicePort(servicePortMap):
│    ├── 生成 KUBE-SVC-<hash> 规则（按概率跳各 SEP）         natRules.Write
│    ├── ClusterIP / ExternalIP / NodePort / LB 各自入口规则
│    └── writeServiceToEndpointRules(...)                   proxier.writeServiceToEndpointRules +0
│          └── 生成 KUBE-SEP-<hash> 链：DNAT 到 Pod IP:Port
└── iptables-restore 原子下发全部 natRules + filterRules
```

### 3.3 链名与概率

| 函数 | 锚点 | 作用 |
|---|---|---|
| `portProtoHash` | `` `pkg/proxy/iptables/proxier.go` · `portProtoHash` `+0` `` | ServicePort → hash（SVC/SEP 链名后缀） |
| `serviceFirewallChainName` | `` `pkg/proxy/iptables/proxier.go` · `serviceFirewallChainName` `+0` `` | `KUBE-FW-*`（ExternalIP/LB） |
| `servicePortEndpointChainName` | `` `pkg/proxy/iptables/proxier.go` · `servicePortEndpointChainName` `+0` `` | `KUBE-SEP-*` |
| `computeProbability` | `` `pkg/proxy/iptables/proxier.go` · `computeProbability` `+0` `` | 计算跳各 SEP 的概率 |
| `proxier.probability` | `` `pkg/proxy/iptables/proxier.go` · `proxier.probability` `+0` `` | 概率取值（预计算缓存 `precomputedProbabilities`） |

**概率负载均衡**：`KUBE-SVC-*` 链里有 N 条跳转规则，第 i 条以概率 `1/(N-i+1)` 跳到第 i 个 SEP，最后一条 `Accept`——实现统计均匀分发。

### 3.4 DNAT 与 MASQ

- ClusterIP 流量：`KUBE-SERVICES` → `KUBE-SVC-*` → `KUBE-SEP-*` → DNAT 到 Pod IP:Port。
- 离开节点的流量：`KUBE-POSTROUTING` + `KUBE-MARK-MASQ` 做 SNAT/MASQ（保证回程路由）。
- `externalTrafficPolicy=Local`：只转发到本节点 endpoint，否则 MASQ。

### 3.5 失败与重试

`syncProxyRules` 里 `success` 标志决定收尾行为：restore 失败 → 记 `IPTablesPartialRestoreFailuresTotal`（部分同步失败时）→ 返回 `retryError`，由 runner 按 `retryInterval` 重试；大集群（`largeClusterMode`）会跳过常规全量同步以省开销。

---

## 4. ipvs / nftables 模式定位

| 模式 | 实现 | 数据面 | Sync 风格 |
|---|---|---|---|
| **iptables** | `` `pkg/proxy/iptables/proxier.go` · `Proxier` `+0` `` | 内核 iptables 链线性遍历 | 每次全量 restore |
| **ipvs** | `` `pkg/proxy/ipvs/proxier.go` · `Proxier` `+0` ``（`syncProxyRules` 见 `` `pkg/proxy/ipvs/proxier.go` · `proxier.syncProxyRules` `+0` ``） | 内核 IPVS 哈希表（VS/RS），性能更好 | 全量重算 VS/RS |
| **nftables** | `` `pkg/proxy/nftables/proxier.go` · `Proxier` `+0` ``（`syncProxyRules` 见 `` `pkg/proxy/nftables/proxier.go` · `proxier.syncProxyRules` `+0` ``） | nftables 规则集 + set/map | **全量 + 部分同步双轨** |
| **winkernel**（Windows） | `` `pkg/proxy/winkernel/proxier.go` · `Proxier` `+0` `` | HNS/HCN 策略 | 全量 |

四者实现同一 `Provider` 接口（`` `pkg/proxy/types.go` · `Provider` `+0` ``），数据流骨架同构：事件 → changes 缓存 → 限频 Sync → 重算 → 原子下发。

---

## 5. nftables 模式：本次漂移修正重点

nftables `pkg/proxy/nftables/proxier.go` 在漂移区间改动较大（+26/−80）。按当前代码，它与 iptables 的关键差异如下。

### 5.1 规则载体：链 + set/map（`nftElementStorage`）

nftables 用 nft 原生 **set/map** 存 Service/endpoint 映射，而非像 iptables 那样把每条 endpoint 写成一条链规则。内部用 `` `pkg/proxy/nftables/proxier.go` · `nftElementStorage` `+0` `` 封装一个 set/map 的元素缓存（`elements` / `leftoverKeys` / 容器类型与名字），构造见 `` `pkg/proxy/nftables/proxier.go` · `newNFTElementStorage` `+0` ``。Proxier 持有多个这样的存储：`clusterIPs`、`serviceIPs`、`firewallIPs`、`noEndpointServices`、`noEndpointNodePorts`、`serviceNodePorts`、`hairpinConnections`、`localhostNodePortRejects`。

`nftElementStorage` 的 `readOrReset` / `ensureElem` / `cleanupLeftoverKeys` 三个方法负责「读现有元素 → 只增删差异 → 清残留键」，这是**部分同步**能成立的基础。

### 5.2 全量 vs 部分同步（`doFullSync`）

`` `pkg/proxy/nftables/proxier.go` · `proxier.syncProxyRules` `+0` `` 开头：

- `doFullSync := proxier.needFullSync || (time.Since(proxier.lastFullSync) > proxyutil.FullSyncPeriod)`。
- 末尾按 `doFullSync` 分别记 `SyncFullProxyRulesLatency` / `SyncPartialProxyRulesLatency`。
- 收尾 `defer`：失败 → `needFullSync = true`（下次强制全量，因为 changes 已被 flush、无法再做部分同步）；成功且 `doFullSync` → 更新 `lastFullSync`。
- 入口处若 `staleChains` 里有超过 1 秒的残留链，先发一个事务删除；删除失败则置 `doFullSync = true` 并记 `NFTablesCleanupFailuresTotal`。

> 这是与 iptables「每次都全量 restore」的**实质差异**：nftables 在条件允许时走部分同步（只改 set/map 元素差异），降低大集群下的同步开销。

### 5.3 负载均衡：`numgen random`，不是概率链

- `` `pkg/proxy/nftables/proxier.go` · `proxier.writeServiceToEndpointDNATs` `+0` ``：
  - **单 endpoint 特例**：直接写一条 `dnat to <podIP>:<port>`，避免插入 map 的开销（注释说明 map 插入是 O(n) 或更差）。
  - 多 endpoint：写 `dnat ip addr . port to numgen random mod N map { 0 : ip.port , ... }`——**内核随机数取模**做分发。
- `` `pkg/proxy/nftables/proxier.go` · `proxier.writeServiceToEndpointJumps` `+0` ``：写 `ip saddr @<affinitySet> goto <epChain>` 粘性规则 + 一条 `numgen random mod N vmap { 0 : goto epChain , ... }`（无 affinity 命中时随机 goto）。

> 对比：iptables 靠用户态预计算的**概率链**；nftables 把负载均衡下推到内核 `numgen random`。

### 5.4 base chain / jump chain 与 localhost NodePort

- base chain 名形如 `nat-prerouting`（`${type}-${hook}`），常量见 `nftablesBaseChains`；顶层 jump chain 见 `nftablesJumpChains`；`servicesChain` 用 `vmap @serviceIPsMap` 把 dst IP:proto:port 映射到对应 svc chain。`[源码未确认]`：`nftablesBaseChains` / `nftablesJumpChains` / `servicesChain` 等常量块的具体成员行未逐一回核。
- localhost NodePort：`localhostNodePortRejects` 集合 + 按 protocol 的 reject chain，对 UDP/SCTP 的 localhost NodePort 做拒绝计数（`LocalhostNodePortRejectedCounterName`）；另有 `localhostNodePortProxy`（`localnodeportproxy.LocalNodePortProxy`）在 localhost 上做用户态代理。

### 5.5 nftables 事件接入

`Sync`（`` `pkg/proxy/nftables/proxier.go` · `proxier.Sync` `+0` ``）/ `SyncLoop`（`` `pkg/proxy/nftables/proxier.go` · `proxier.SyncLoop` `+0` ``）与 iptables 同形。事件回调见 §2.2，额外有 `OnTopologyChange` 置 `needFullSync` 后 `Sync()`。

---

## 6. 全链路时序（一次 Endpoint 变化，iptables 模式）

```text
EndpointSlice 变化
└── informer → proxier.OnEndpointSliceUpdate            iptables/proxier.go · proxier.OnEndpointSliceUpdate
     └── endpointsChanges.EndpointSliceUpdate(...)       仅改内存缓存
          └── 变化生效 → proxier.Sync()                  iptables/proxier.go · proxier.Sync
               └── syncRunner.Run()                      runner/bounded_frequency_runner.go · Run（容量1 channel 合并）
                    └── (限频后) syncProxyRules           iptables/proxier.go · proxier.syncProxyRules
                         ├── endpointsMap.Update          刷 endpoint 缓存
                         ├── 重建 KUBE-SERVICES/KUBE-SVC-*/KUBE-SEP-* 全部链
                         ├── writeServiceToEndpointRules  生成 DNAT 规则
                         └── iptables-restore 原子下发
                              └── 内核按新规则 DNAT 流量到 Pod
```

nftables 模式同形，只是最后一步换成 `knftables.Transaction` 提交（且可能只做部分同步，见 §5.2）。

---

## 7. 验证与边界

### 7.1 代码回核

本专题锚点均在当前工作区复核（sr_tools.py 的 offset 子命令 + codegraph 双向确认）。抽样：

- iptables：`Proxier`、`NewProxier`、`proxier.Sync`、`proxier.SyncLoop`、`proxier.syncProxyRules`、`proxier.writeServiceToEndpointRules`、`portProtoHash`、`serviceFirewallChainName`、`servicePortEndpointChainName`、`computeProbability`、`proxier.probability`、各 `On*` 回调。
- nftables：`Proxier`、`NewProxier`、`proxier.Sync`、`proxier.SyncLoop`、`proxier.syncProxyRules`、`proxier.writeServiceToEndpointDNATs`、`proxier.writeServiceToEndpointJumps`、`nftElementStorage`、`newNFTElementStorage`、各 `On*` 回调。
- ipvs：`Proxier`、`NewProxier`、`proxier.Sync`、`proxier.SyncLoop`、`proxier.syncProxyRules`。
- runner：`BoundedFrequencyRunner`、`NewBoundedFrequencyRunner`、`Loop`、`Run`（均在 `pkg/proxy/runner/`，非 client-go）。
- meta / types：`metaProxier`、`Provider`。

### 7.2 生产实测

**未实测** `[待运行验证]`。三级链实际结构、概率/`numgen` 分发实际分布、restore/事务原子性、Sync 限频实际间隔、`numgen random mod N` 的真实均匀度均为代码推断，未在真实节点 `iptables-save`/`ipvsadm`/`nft list ruleset` 验证。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| iptables `syncProxyRules` 全文 | 未逐行读 | 各 Service 类型（ClusterIP/ExternalIP/NodePort/LB）分支未逐行展开 |
| sessionAffinity | 未深挖 | iptables `KUBE-SEP` recent 模块粘性规则未读 |
| conntrack 清理 | 未深挖 | `conntrack/` 连接跟踪清理未展开 |
| healthcheck nodePort | 未深挖 | `healthcheck/` 健康检查 nodePort 未读 |
| ipvs 规则生成 | 未深挖 | VS/RS 增删与调度器选择未展开 |
| nftables 部分同步细节 | 未逐行读 | `runTransaction`/`flushRules` 等提交辅助函数、`nftablesBaseChains`/`nftablesJumpChains`/`servicesChain` 常量成员 `[源码未确认]` |
| 双栈 `metaProxier` | 未深挖 | 仅定位 `metaProxier`，双栈分发细节未展开 |

### 7.4 修改联动

- 改链结构 → `proxier.syncProxyRules` / `proxier.writeServiceToEndpointRules`，同步本文 §3。
- 改负载均衡 → iptables `computeProbability`/`proxier.probability`，或 nftables `proxier.writeServiceToEndpointDNATs`/`proxier.writeServiceToEndpointJumps`，同步 §3.3 / §5.3。
- 改限频 → `BoundedFrequencyRunner`（`pkg/proxy/runner/`），同步 §2.3。
- 改 nftables 全量/部分同步策略 → `proxier.syncProxyRules` 的 `doFullSync` 判定与 `needFullSync`，同步 §5.2。

**关联文档**：[16-kube-proxy网络代理](../sourceReader/16-kube-proxy网络代理.md)（kube-proxy 全貌）、[11-informer与workqueue机制](../sourceReader/11-informer与workqueue机制.md)（informer 事件接入）、[workqueue机制](workqueue机制.md)（背压范式）、[conntrack与ServiceCIDR](conntrack与ServiceCIDR.md)（连接跟踪与 ServiceCIDR）。

[总目录](../sourceReader/README.md)
