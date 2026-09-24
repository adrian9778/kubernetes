# 14. Service、EndpointSlice 与 Kube Proxy

## 14.1 Kube Proxy 不是数据包代理进程

它观察 API 并编译节点数据面规则，实际包转发由 nftables、iptables、IPVS 或 Windows HNS 完成。Service 提供稳定虚拟 IP，EndpointSlice 提供后端集合。

## 14.2 输入控制器

`pkg/proxy/config` 把 Service、EndpointSlice、Node informer 事件转换为 handler callbacks。ServiceChangeTracker 和 EndpointsChangeTracker 合并短时间更新，构建 ServicePortMap 和 EndpointsMap。

EndpointSliceCache 需要合并同一 Service 的多个 slices，处理 addressType、ports、ready/serving/terminating conditions、zone/hints 和删除。

## 14.3 同步链

```text
Informer events
 -> ChangeTracker.Update
 -> Proxier.Sync
 -> BoundedFrequencyRunner
 -> syncProxyRules
 -> apply pending changes to maps
 -> render desired rules
 -> atomic restore / nft transaction / IPVS reconcile
 -> metrics + health timestamp
```

限频 runner 在高频变更下合并更新，又通过最大间隔保证最终校正。Informer synced 与 dataplane successfully synced 是两个不同状态。

## 14.4 nftables

当前源码中 nftables 是正式后端路径。它使用 table/chains/sets/maps 和事务更新，适合大量 Service 的增量表达。排障工具和规则结构与 iptables 不同，不应套用旧 chain 名教程。

## 14.5 iptables

iptables proxier 为 Service 入口和 endpoint 建 chains，匹配 ClusterIP、ExternalIP、LoadBalancerIP、NodePort 并 DNAT。iptables-restore 减少逐条调用和中间状态。SNAT/MASQUERADE 由流量来源、目标和策略决定。

## 14.6 IPVS

IPVS proxier 把 Service 编为 virtual server，endpoint 编为 real server，并使用 ipset/iptables 处理额外流量分类。需要同步 dummy interface 地址、IPVS state 和辅助规则。

## 14.7 流量策略

externalTrafficPolicy Local 保留客户端源 IP并只选本地可用 endpoint；internalTrafficPolicy Local 限制集群内流量；sessionAffinity 使用 client IP 粘性；trafficDistribution/topology hints 倾向本 zone/region；terminating endpoint 在特定回退条件下仍可能服务。

## 14.8 双栈

Service ClusterIPs/ipFamilies、EndpointSlice addressType 和 proxier address family 必须一致。MetaProxier 可组合 IPv4/IPv6 实现。单栈正常不表示另一地址族配置正确。

## 14.9 conntrack

规则改变不总能立即影响已有 conntrack entry。UDP、删除 endpoint、NodePort 和 NAT 排障常需检查 conntrack 清理。Proxier 在特定变更后触发 stale entry 清理。

## 14.10 Windows

winkernel proxier 使用 HNS/HCN policy，不走 Linux netfilter。通用 Service/Endpoint 模型相同，平台能力、DSR 和网络实现不同。阅读时确认 `_windows.go`。

## 14.11 NetworkPolicy 边界

核心 kube-proxy 实现 Service 转发，不实现完整 NetworkPolicy。NetworkPolicy 通常由 CNI/network policy engine 实现。Service 可达但策略拒绝时，不能把问题归到 kube-proxy。

## 14.12 教学实现

先写纯内存编译器：Service+EndpointSlice -> RuleSet；按 readiness、family、local policy 过滤；fake dataplane 事务 apply；注入部分失败后下一轮 full sync 恢复。最后才接真实 nftables，并仅操作自有 table。

