# Kubernetes v1.37 节点与数据面源码导读

本文档基于当前工作树 `b882c60b402`，范围严格限定为 kubelet 与 kube-proxy 的生产源码。目标不是列目录，而是回答四个问题：数据从哪里来，状态由谁拥有，哪个 goroutine 推动状态前进，失败后如何重新收敛。

## 阅读地图

| 文档 | 主问题 | 最重要的源码入口 |
|---|---|---|
| [01-kubelet启动与总控制环.md](01-kubelet启动与总控制环.md) | kubelet 如何完成配置、依赖装配并启动所有循环 | `cmd/kubelet/app/server.go`、`pkg/kubelet/kubelet.go` |
| [02-podWorkers-PLEG-CRI生命周期.md](02-podWorkers-PLEG-CRI生命周期.md) | 一个 Pod 如何创建、重试、终止并与容器运行时收敛 | `pod_workers.go`、`pleg/`、`kuberuntime/` |
| [03-探针状态卷与优雅终止.md](03-探针状态卷与优雅终止.md) | 异步探针、PodStatus、卷挂载和删除安全如何协作 | `prober/`、`status/`、`volumemanager/` |
| [04-资源管理驱逐与checkpoint.md](04-资源管理驱逐与checkpoint.md) | CPU、内存、设备、NUMA 和压力驱逐如何保持一致 | `cm/`、`eviction/`、`checkpointmanager/` |
| [05-kube-proxy公共模型与流量语义.md](05-kube-proxy公共模型与流量语义.md) | Service/EndpointSlice 如何变成统一的转发表模型 | `pkg/proxy/`、`cmd/kube-proxy/app/` |
| [06-iptables-ipvs-nftables-winkernel.md](06-iptables-ipvs-nftables-winkernel.md) | 四种后端如何实现相同 Service 语义 | 各后端 `proxier.go` |
| [07-新手重建路线.md](07-新手重建路线.md) | 如何从零实现一个功能等价的最小节点代理 | 全部上述模块 |

## 先建立三个正确心智模型

1. kubelet 不是“一次创建 Pod”的命令执行器，而是一组通过缓存和事件连接起来的控制环。API Pod 是期望，CRI PodStatus、挂载点、cgroup 和设备分配是现实，所有循环都必须可重复执行。
2. `podWorkers` 是单 Pod 生命周期串行化中心，但不是所有状态的唯一拥有者。运行时状态由 CRI/PLEG 观察，API 状态由 status manager 缓存，卷状态由 DSW/ASW 保存，资源分配由各 manager 的 checkpoint 保存。
3. kube-proxy 不转发用户数据包。它观察 Service 和 EndpointSlice，然后把目标网络状态编译为 iptables、IPVS、nftables 或 Windows HNS/HCN 对象，真正的数据包由内核处理。

## 建议阅读顺序

先顺着一个 Pod 的正常启动读 `01 → 02 → 03`，再读异常路径 `04`。理解 kubelet 后再读 `05 → 06`。最后按 `07` 自己实现。不要从某个 2000 行的 `syncProxyRules` 中间开始，那里每一行都认识，整体仍然会迷路，算是大型项目的传统节目。

## 当前 v1.37 工作树值得特别留意的变化

以下不是发布公告摘要，而是本次源码阅读中直接可见的实现变化：

- `kuberuntime_manager.go` 已把 Pod 级资源原地调整纳入 `podActions`，包含 `UpdatePodLevelResources`、CRI `UpdatePodSandboxResources`、回滚和 actuated resource checkpoint。
- 内存管理器除 `none/static` 外出现 `best-effort` 策略，并与 Topology Manager 的 authoritative store 配合。
- cgroup v1 在 container manager 中明确标记为弃用；重建实现应以 cgroup v2 为主。
- probe manager 对 kubelet 重启后的 readiness/startup 状态有显式恢复逻辑，restartable init container 也参与探针管理。
- nftables 后端维护 ServiceCIDR、未分配 ClusterIP、非法端口、localhost NodePort 等独立集合/映射，并可启用本地 NodePort userspace 辅助代理。
- Windows winkernel 后端包含 LB 原地更新失败指标、陈旧 LB 延迟清理、终止 endpoint 清理和更完整的双栈能力探测。

