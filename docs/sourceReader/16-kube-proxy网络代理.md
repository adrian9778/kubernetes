[上一篇](15-garbage-collector垃圾回收.md) · [总目录](README.md) · [下一篇](17-CRD扩展与聚合层.md)

# kube-proxy 网络代理
> **层级**：第四层——函数级源码解析。
> **范围**：Service/EndpointSlice 事件接入、iptables 规则生成（KUBE-SERVICES/SVC/SEP 三级链）、ipvs 模式定位。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/kube-proxy/proxy.go#main` → `cmd/kube-proxy/app`（NewProxyCommand、Options、ProxyServer.Run） |
| 实现位置 | **主模块 `pkg/proxy/`**（本分支差异：staging 的 `k8s.io/kube-proxy` 只含配置 API，无 proxy 实现） |
| 子模块 | `config`（Service/EndpointSlice 事件）、`iptables`、`ipvs`、`nftables`、`metaproxier`、`runner` |
| Proxier 创建 | `cmd/kube-proxy/app/server_linux.go`：iptables.NewProxier:165 / ipvs.NewProxier:211 / nftables.NewProxier:248 |
| 数据流 | Service/EndpointSlice informer → config handler（直接函数回调）→ proxier 内部 changes 缓存 → Sync → syncProxyRules 全量生成规则 → iptables-restore |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `Provider`（interface） | `pkg/proxy/types.go:28` | 组合 Service/EndpointSlice/NodeTopology/ServiceCIDR handler 接口加 `Sync()`/`SyncLoop()`（无 provider.go 文件） |
| `metaProxier` | `metaproxier/meta_proxier.go` | 双栈包装：持有 ipv4/ipv6 两个 Provider，按 EndpointSlice 地址类型分发 |
| `ServiceConfig` | `config/config.go:167` | Service 事件入口，持有 eventHandlers |
| `EndpointSliceConfig` | `config/config.go:73` | EndpointSlice 事件入口（本分支无 EndpointsConfig） |
| iptables `Proxier` | `iptables/proxier.go:127` | 持有 serviceChanges、endpointsChanges、svcPortMap、endpointsMap、syncRunner、nat/filter LineBuffer |
| ipvs `Proxier` | `ipvs/proxier.go:148` | ipvs 模式实现（结构同构） |

## 3. 启动与事件接入函数级

### 函数：NewProxyCommand / Options / ProxyServer.Run
**源码：** `cmd/kube-proxy/app/server.go:100`、`options.go:52/195`、`server.go:526`
**装配（:608 ～ +639）：**
1. `NewServiceConfig` → `serviceConfig.RegisterEventHandler(s.Proxier)` → `go serviceConfig.Run(...)`
2. EndpointSlice 同构
3. `go s.Proxier.SyncLoop()`

### 函数：config ServiceConfig / EndpointSliceConfig
**源码：** `config/config.go`
**关键特征：** informer 事件经 `handleAdd/Update/DeleteService`（:212/224/241）与 `handleAdd/Update/DeleteEndpointSlice`（:118/130/147）**同步遍历** eventHandlers 直接调用 proxier 回调；事件不入中间 channel。Run（:105/:199）在 WaitForCacheSync 后回调 OnXxxSynced。

### 函数：iptables Proxier 的事件回调
**源码：** `iptables/proxier.go`
- `OnServiceUpdate`（:454）：写入 `serviceChanges`，initialized 后调 `Sync()`
- EndpointSlice 回调：:481/489/497，写入 endpointsChanges
**设计效果：** 事件只做增量记账；规则生成是定时/触发的全量重建，避免增量改规则的中间不一致。

### 函数：Sync / SyncLoop
**源码：** `iptables/proxier.go:413 / :422`
**机制：** Sync 触发 `BoundedFrequencyRunner.Run`（最小间隔/突发参数）；SyncLoop 让 runner 常驻循环。runner 实现见 `pkg/proxy/runner/bounded_frequency_runner.go`，把高频事件合并为有限频率的规则同步。

## 4. 规则生成函数级

### 函数：syncProxyRules
**源码：** `iptables/proxier.go:625`
**职责：** 从当前全部 ServicePort 与 endpoint 全量生成 iiptables 规则，一次性 restore。
**执行流程：**
```text
syncProxyRules (+0)
├── 1. initialized 门控：等 Service 与 EndpointSlice 都 synced (+5)
├── 2. svcPortMap.Update(serviceChanges)、endpointsMap.Update(endpointsChanges) (+27 ～ +28)
├── 3. 全量场景 EnsureChain/EnsureRule 建 PREROUTING→KUBE-SERVICES jump (+61 ～ +74)
├── 4. 写顶层链：KUBE-SERVICES、KUBE-NODEPORTS、KUBE-POSTROUTING、KUBE-MARK-MASQ (+105 ～ +111)
├── 5. for svcName, svc := range svcPortMap 逐 ServicePort (+188):
│      ├─ clusterPolicyChain：KUBE-SVC-<portProtoHash>（:540，+205）
│      ├─ internalTrafficChain（:851）
│      └─ 外部流量链 KUBE-FW-/KUBE-XLB-
├── 6. ClusterIP 捕获：-A KUBE-SERVICES -d <clusterIP> --dport -j internalChain (+300 ～ +307)
├── 7. writeServiceToEndpointRules（:1430）:
│      KUBE-SVC-XXXX 内按 --mode random --probability 写概率跳 KUBE-SEP-XXXX (+579 ～ +587)
├── 8. 逐 endpoint 写 KUBE-SEP-XXXX：hairpin SNAT + DNAT --to-destination (+593)
├── 9. 与现存规则 diff，清理不再使用的旧链
└── 10. iptables.RestoreAll 一次性 iptables-restore（NoFlushTables）(+759)
```

### 三级链结构
```text
KUBE-SERVICES（按 ClusterIP/NodePort 匹配，一跳）
  ↓
KUBE-SVC-XXXX（一个 ServicePort：按概率在多 endpoint 间负载均衡）
  ↓
KUBE-SEP-XXXX（一个 endpoint：MARK-MASQ 判断 + DNAT 到 Pod IP:Port）
```

### 函数：writeServiceToEndpointRules
**源码：** `iptables/proxier.go:1430`
**概率算法：** n 个 endpoint 时每条规则概率 `1/剩余数`（stateless 的随机选择，最后一条不带 probability 作兜底）。

## 5. ipvs 模式定位
| 函数 | 文件行 | 职责 |
|---|---|---|
| `NewProxier` | `ipvs/proxier.go:242` | 建 ipvs Proxier（同样用 syncRunner） |
| `Sync/SyncLoop` | :549/:558 | 与 iptables 同构 |
| `syncProxyRules` | :658 | 逐 ServicePort 调 `ipvs.AddVirtualServer`（:1540），逐 endpoint `AddRealServer/UpdateRealServer`（:1659/1679） |
| 事件回调 | :581、:610 | OnServiceAdd、OnEndpointSliceAdd |

ipvs 模式把负载均衡下发给内核 IPVS（哈希表查找），kube-proxy 只维护 virtual/real server 集合；Service ClusterIP 的捕获仍配合少量 iptables 规则。

## 6. 调用关系总览
```text
Service/EndpointSlice informer
 ↓ 同步回调
Proxier.OnServiceUpdate / OnEndpointSliceUpdate
 └── serviceChanges/endpointsChanges 记账 → Sync
      ↓ BoundedFrequencyRunner
syncProxyRules
 ├── svcPortMap/endpointsMap 更新
 ├── KUBE-SERVICES 顶层链
 ├── 逐 ServicePort：KUBE-SVC 概率链 → KUBE-SEP DNAT 链
 └── RestoreAll（ipvs：AddVirtualServer/RealServer）
```

## 7. 设计意图（源码可证）
- 事件与规则生成分离：informer 回调只增量记账，规则全量重建后原子 restore，规则表始终一致。
- 三级链按"匹配 Service → 选择 endpoint → DNAT"分层，endpoint 增减只改对应 SVC/SEP 链。
- 双栈经 metaProxier 在入口分流，ipv4/ipv6 各自一套独立规则。

[上一篇](15-garbage-collector垃圾回收.md) · [总目录](README.md) · [下一篇](17-CRD扩展与聚合层.md)
