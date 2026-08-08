# iptables、IPVS、nftables 与 Windows Kernel 后端

## 1. 共同骨架

四个 Proxier 都实现 `proxy.Provider`：事件进入 change tracker；`SyncLoop` 由 bounded runner 调 `syncProxyRules`；同步先 checkout Service/Endpoint changes，再从完整内存图生成目标内核状态；最后更新 healthcheck、metrics、conntrack 和陈旧对象。

后端不应只做增量。事件可能合并、丢失，管理员也可能手工改内核规则。周期 full sync 必须能从完整 `svcPortMap + endpointsMap` 重建正确状态。

## 2. iptables

[`pkg/proxy/iptables/proxier.go`](../../../pkg/proxy/iptables/proxier.go) 的核心链：`KUBE-SERVICES` 接 ClusterIP/外部 VIP，`KUBE-NODEPORTS` 接 NodePort，`KUBE-POSTROUTING` 和 `KUBE-MARK-MASQ` 做 SNAT 标记，`KUBE-FORWARD` 放行转发，`KUBE-PROXY-FIREWALL` 处理 LB source ranges。每个 Service 有 cluster/local/external/firewall chain，每个 endpoint 有 `KUBE-SEP-*` chain。

`syncProxyRules` 先 `iptables-save` 读取现状，构造 filter/nat 的 chain 和 rule buffer，再用 restore 一次提交，避免逐条命令的高开销和大窗口半成品。Service chain 用 statistic random 逐级选择 endpoint；endpoint chain 做 hairpin/source masquerade 和 DNAT。

ClusterIP、ExternalIP、LB VIP、NodePort 各自跳入合适 policy chain。外部 Local policy 需要区分本机源流量和真正外部流量，以兼顾源 IP 保留与本机访问短路。无 endpoint 与无 local endpoint 分别生成 REJECT/DROP。UDP Service 或 endpoint 删除后清理 conntrack，避免旧 flow 继续命中已不存在 backend。

`ipt.Monitor` 通过 canary chain 发现规则被外部 flush，调用 `forceSyncProxyRules`。[`cleanup.go`](../../../pkg/proxy/iptables/cleanup.go) 用 save/restore 删除 kube-proxy 自有 chain，不清空整个系统 table。

## 3. IPVS

[`pkg/proxy/ipvs/proxier.go`](../../../pkg/proxy/ipvs/proxier.go) 将 VIP:port 建为 `VirtualServer`，endpoint 建为 `RealServer`。ClusterIP/LB/ExternalIP 通常绑定到 dummy interface `kube-ipvs0`；NodePort 使用真实 node IP。scheduler 来自配置，ClientIP affinity 映射为 IPVS persistence timeout。

IPVS 仍依赖 ipset + iptables：ipset 对 ClusterIP、NodePort、LB、source range、Local policy 等分类，iptables 做流量捕获、masquerade、filter 和 healthcheck。同步流程：构造 active ipset entries；创建/更新 VS；同步 RS；提交 ipset/iptables；删除陈旧 VS 和 dummy 地址；清 UDP conntrack。

[`graceful_termination.go`](../../../pkg/proxy/ipvs/graceful_termination.go) 删除 RS 时先把 weight 置 0 并放入 termination list，等待连接消退后删除。若 endpoint 又出现，则移出队列并恢复，避免滚动更新切断现有连接。

## 4. nftables

[`pkg/proxy/nftables/proxier.go`](../../../pkg/proxy/nftables/proxier.go) 使用单独 `kube-proxy` table。base chains 挂在 prerouting/output/input/forward/postrouting hooks；普通 chains 完成 services、endpoint checks、firewall、masquerade、ClusterIP 合法性检查。

nftables 的优势是原生 map/set/transaction：`service-ips` 和 `service-nodeports` 可把 tuple verdict-map 到 per-service chain；`cluster-ips` 检查非法端口；`no-endpoint-*` 决定 reject/drop；`hairpin-connections` 决定 masquerade；affinity set 保存 client→endpoint 选择。一次 transaction 原子提交目标变化。

`nftElementStorage` 追踪 active/previous element，`ensureElem` 加入目标，`cleanupLeftoverKeys` 删除陈旧 key。per-service/per-endpoint chain 不立即删除，而进入 `staleChains map[chain]time`，延迟到下一阶段清理，避免事务中仍存在引用。

当前工作树还处理 ServiceCIDR：对已分配 ClusterIP 的非法端口 reject，对 ServiceCIDR 中未分配地址 drop。localhost NodePort 根据 feature gate、地址过滤和协议，可由 local userspace proxy 服务或明确 reject 不支持协议。

[`README.md`](../../../pkg/proxy/nftables/README.md) 是理解 hook 优先级的首选说明，特别是 LB source range 必须在 DNAT 前检查，而某些 endpoint reject 只能放在支持 reject verdict 的 filter hook。

## 5. winkernel

[`pkg/proxy/winkernel/proxier.go`](../../../pkg/proxy/winkernel/proxier.go) 把目标状态编译为 HNS/HCN endpoint 和 load balancer policy。`serviceInfo` 保存 ClusterIP、NodePort、ExternalIP、LB ingress 对应 HNS ID；远端 `endpointInfo` 有共享 refCount，防止多个 Service 引用时被提前删除。

`syncProxyRules` 先查询当前网络中的全部 endpoint 和 LB，随后为 Service VIP 创建 endpoint、为后端查找/创建 HNS endpoint，再为 ClusterIP、NodePort、ExternalIP、LB ingress、healthcheck 分别创建或更新 LB。Overlay 网络根据 endpoint 是否远端节点/公网选择 source VIP；L2Bridge 通常使用 node IP。

Local policy 与 preserve destination IP 通过 DSR/local routed VIP/MUX 等 flags 实现。session affinity 仅在平台能力支持时启用。所有 endpoint terminating 时不新建 LB；远端 endpoint 仅在 refCount 为 0 后删除。

LB 删除失败会放入 `mapStaleLoadbalancers`，后续 `cleanupStaleLoadbalancers` 重试。当前实现还区分 create/update 失败指标和 LB 类型，便于定位 HNS 资源泄漏。

## 6. 后端对照

| 维度 | iptables | IPVS | nftables | winkernel |
|---|---|---|---|---|
| 负载选择 | 规则概率链 | 内核 scheduler | verdict map/numgen | HNS LB policy |
| 原子更新 | restore 批量 | 多对象 reconcile | transaction | HNS/HCN API 多对象 |
| VIP 承载 | NAT 规则 | `kube-ipvs0` | nft NAT | HNS endpoint/LB |
| 会话保持 | recent/check 规则 | persistence | dynamic affinity set | HNS affinity flag |
| 优雅删后端 | conntrack 自然消退 | weight=0 队列 | 陈旧 chain 延迟删 | refcount + stale LB |
| 双栈 | 两个单栈 proxier | 两个单栈 proxier | 两个 family table | HNS 能力探测后双实例 |

