# kube-proxy 公共模型、Service 与 EndpointSlice

## 1. 启动链

入口 [`cmd/kube-proxy/proxy.go`](../../../cmd/kube-proxy/proxy.go) 调 [`app/server.go`](../../../cmd/kube-proxy/app/server.go) 的 `NewProxyCommand`。`newProxyServer` 创建 client、event recorder、health server、node manager，探测主 IP family 和平台能力，再由平台 `createProxier` 选择 iptables/IPVS/nftables/winkernel。

`ProxyServer.Run` 创建 shared informer：Service、EndpointSlice，必要时还有 Node、NodeTopology、ServiceCIDR。各 config 把 informer 事件转成 `OnService*`、`OnEndpointSlice*` 等 Provider 回调，最后启动 `Proxier.SyncLoop`。

Linux [`server_linux.go`](../../../cmd/kube-proxy/app/server_linux.go) 先设置 conntrack sysctl，并分别探测 IPv4/IPv6 后端能力。只要两族都受支持就创建 dual-stack proxier，实际实现通常是 meta proxier 包住两个独立单栈 proxier。Windows [`server_windows.go`](../../../cmd/kube-proxy/app/server_windows.go) 通过 HNS/HCN compat tester 判断双栈。

## 2. 事件不是立即写规则

所有后端都使用同一模式：事件回调只更新 change tracker 并调用 `Sync()`；[`runner/bounded_frequency_runner.go`](../../../pkg/proxy/runner/bounded_frequency_runner.go) 在 min interval、retry interval 和 full sync interval 之间合并触发。这样 EndpointSlice 风暴不会每次都完整重写规则，同时仍有周期 full sync 修复外部漂移。

`OnServiceSynced` 和 `OnEndpointSlicesSynced` 都到达前，proxier 不应进行首次完整编程，否则会把“informer 尚未 list 完”误判为“集群没有 endpoint”，短暂清空数据面。

## 3. Service 模型

[`serviceport.go`](../../../pkg/proxy/serviceport.go) 的 `ServicePort` 是后端无关接口，`BaseServicePortInfo` 保存 ClusterIP、port/protocol、NodePort、ExternalIP、LB VIP/source ranges、session affinity、healthCheckNodePort，以及 internal/external traffic policy。

`UsesClusterEndpoints()` 不等于 `internalTrafficPolicy=Cluster`：外部可访问 Service 即使 external policy 为 Local，仍可能需要 cluster endpoint 做短路等语义。`UsesLocalEndpoints()` 在 internal policy Local，或 external policy Local 且确实有外部入口时为真。

[`servicechangetracker.go`](../../../pkg/proxy/servicechangetracker.go) 以 `ServicePortName(namespace/name, port, protocol)` 为 key，把旧/新 Service 转成 `ServicePortMap`。`Update` 仅缓存 previous/current；规则同步时 `ServicePortMap.Update` 才 checkout、执行 callback、merge/unmerge，并清空 pending changes。

## 4. EndpointSlice 模型

[`endpoint.go`](../../../pkg/proxy/endpoint.go) 的 `Endpoint` 区分：

- `IsReady`：ready 且通常不 terminating；publishNotReadyAddresses 可改变语义。
- `IsServing`：是否仍能服务，不因 terminating 自动变 false。
- `IsTerminating`：正在优雅退出。
- `IsLocal`：endpoint 的 nodeName 与当前节点匹配。
- zone/node hints：用于 topology-aware routing。

[`endpointslicecache.go`](../../../pkg/proxy/endpointslicecache.go) 按 Service 和 Slice 保存数据，因为一个 Service 的 endpoint 分散在多个 Slice。Slice 更新先进入 cache，checkout 时为受影响 Service 计算 previous/current 聚合图。随后 [`endpointschangetracker.go`](../../../pkg/proxy/endpointschangetracker.go) 更新 `EndpointsMap[ServicePortName][]Endpoint`。

这个两级结构解决了常见错误：删除一个 Slice 不能删除整个 Service 的 endpoint；同 IP 不同端口是不同 backend；healthCheck 统计 local ready endpoint 时要按 IP 去重。

## 5. traffic policy 与终止回退

[`topology.go`](../../../pkg/proxy/topology.go) 的 `CategorizeEndpoints` 一次返回 cluster endpoints、local endpoints、所有本机可达 endpoints 和 hasAnyEndpoints。

选择规则：

1. Cluster 路径默认选所有 Ready endpoint。
2. 若启用 PreferSameNode/PreferSameZone 且 hints 完整，过滤到 node/zone；hints 不完整则安全回退为所有 Ready。
3. Local 路径优先 local Ready。
4. 若没有 local Ready，但有 local Serving+Terminating，则使用后者，减少滚动终止时连接中断。
5. Cluster 路径同样在完全没有 Ready 时可回退到 Serving+Terminating。
6. `hasAnyEndpoints=true` 但策略筛选后为空，通常应 DROP；整个 Service 根本无 endpoint，通常应 REJECT。两者对客户端重试语义不同。

`externalTrafficPolicy=Local` 的目标是保留源 IP 并仅转本地 endpoint；无本地 endpoint 的节点不能偷偷转远端。`internalTrafficPolicy=Local` 对 ClusterIP 生效。healthCheckNodePort 报告本地 ready endpoint 数，使外部 LB 不再向无本地后端节点送流量。

## 6. 双栈

公共 tracker 和后端实例都按 `ipFamily` 构造。Service 通过 `GetClusterIPByFamily` 取当前族 ClusterIP；EndpointSlice 的 addressType 也必须匹配。双栈不是一张同时混合 IPv4/IPv6 的规则图，而是两个共享 informer 输入、各自过滤和编程的单栈控制器。

重建时不要用“字符串含冒号”散落判断地址族。入口处解析并分类，内部对象携带明确 family，所有 VIP、endpoint、CIDR、node address 都只进入同族实例。

