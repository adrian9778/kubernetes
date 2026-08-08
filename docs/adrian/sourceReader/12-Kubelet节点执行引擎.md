# 12. Kubelet：节点执行、Pod 状态机与 CRI

## 12.1 配置与依赖

Kubelet 启动输入分为 KubeletFlags、版本化 KubeletConfiguration、KubeletServer 和 kubelet.Dependencies。配置可来自 `--config` 及 drop-in 目录，再恢复命令行优先级。Dependencies 注入 clients、CRI、cAdvisor、mounter、container manager、recorder、OS abstraction 等，便于平台适配和测试。

## 12.2 NewMainKubelet

[kubelet.go](../../../pkg/kubelet/kubelet.go) 的 `NewMainKubelet` 创建主对象，字段涵盖 podManager、podWorkers、runtime、statusManager、probeManager、volumeManager、containerManager、evictionManager、pleg、secret/configmap managers、certificate managers 等。构造阶段应建立依赖，不应启动无法停止的 goroutine。

## 12.3 Pod 来源和 PodConfig

Kubelet 接收 API Server 中绑定本节点的 Pod和静态 file/HTTP source。PodConfig 将多个 source 的 ADD/UPDATE/REMOVE/RECONCILE/DELETE/SET 归并。静态 Pod 由本地 manifest 驱动，并创建 mirror Pod 供控制面观察。

PodManager 以 UID 管理生命周期。名字可重用，UID 才能区分同名新旧 Pod。

## 12.4 syncLoop

syncLoop 多路监听：Pod updates、PLEG/runtime events、sync ticker、housekeeping、liveness/readiness/startup probe results。事件最终触发 Pod worker 或节点级维护。

周期同步是安全网，不是唯一正确性来源。runtime event 降低容器退出发现延迟，probe update 改变 restart/readiness，housekeeping 清理孤儿资源。

## 12.5 podWorkers

[pod_workers.go](../../../pkg/kubelet/pod_workers.go) 为每个 Pod UID 建立串行 worker 状态。不同 Pod 可并行，同 Pod 的创建、更新和删除不能并发。处理期间的新 update 被记录，完成后再次同步，不会丢最后状态。

概念状态：

```text
SyncPod
 -> SyncTerminatingPod
 -> SyncTerminatingRuntimePod
 -> SyncTerminatedPod
 -> remove worker state
```

终止状态区分 API Pod 仍在、本地只剩 runtime sandbox、最终清理等边界。`completeWork` 根据结果和时间安排重试。

## 12.6 syncPod 主干

一次同步通常包括：生成当前 API status；admission/active deadline/eviction 检查；创建数据目录和 cgroup；等待 volume mount；获取 pull secrets；调用 runtime SyncPod；注册 probe；记录 event 与下一重试。

任何阶段都可能部分成功。下一轮必须通过观察磁盘、runtime 和 manager state 继续收敛，而不是假设前轮原子完成。

## 12.7 CRI Runtime Manager

CRI API 位于 `staging/src/k8s.io/cri-api`，client 在 `cri-client`。RuntimeService 管理 sandbox/container/status/exec，ImageService 管理镜像。Kubelet 的 `pkg/kubelet/kuberuntime` 把 PodSpec 转成 CRI 请求。

`kubeGenericRuntimeManager.SyncPod` 比较期望 containers 和 runtime PodStatus，计算 PodActions：是否重建 sandbox、保留/杀死哪些 containers、启动 init/app/ephemeral containers。Sandbox network namespace 变化通常要求重建其中 containers。

RestartPolicy、init/sidecar、container restart rules、backoff、hooks 和 probe 共同决定动作。CrashLoopBackOff 是本地重启退避状态，不是 Scheduler 失败。

## 12.8 PLEG

PLEG 观察 runtime 生命周期，产生 ContainerStarted、ContainerDied、ContainerRemoved、PodSync 等事件。GenericPLEG 周期 relist 并比较前后状态；事件化路径可减少轮询。PLEG 长期不健康会影响 Kubelet Ready，因为节点无法可靠观察容器现实。

## 12.9 Probe

Startup probe 成功前保护慢启动应用；liveness 失败触发重启；readiness 失败只使容器/Pod 不 Ready，从 EndpointSlice 移除。Probe worker 结果必须绑定 container ID，防止旧实例结果污染重启后的新容器。

## 12.10 Status Manager

Status Manager 缓存并写回 PodStatus，合并 runtime、probe、volume、Pod conditions 和 IP。写回要核对 UID、处理 mirror Pod、API conflict 和终态。API 写失败不改变本地容器，下轮继续。

## 12.11 Node Status 与 Lease

NodeStatus 包含 addresses、capacity/allocatable、conditions、images、runtime handlers 和 daemon endpoints。Lease 提供高频轻量 heartbeat，完整 NodeStatus 较低频更新。Controller Manager 综合 Lease 和 status 判断健康。

## 12.12 终止

Pod 删除后执行 preStop、TERM、等待 grace period、KILL，停止 sandbox，卸载 volume，更新 terminal status 并清理目录。更短的新期限可以收紧旧期限。Kubelet 重启和 runtime timeout 下终止仍须可重入。

## 12.13 镜像和容器 GC

Image Manager 拉取、验证凭证和并发策略；Image GC 根据磁盘阈值与最后使用时间清理；Container GC 清理死亡容器和日志。GC 必须避开正在使用对象，并与 eviction 的 disk pressure 信号协作。

## 12.14 节点安全

Kubelet secure server 使用认证、authorization webhook 和节点身份；证书 bootstrap/rotation 维护 client/server certificates。只读端口和匿名访问是高风险兼容项。Pod exec/attach/port-forward 走 streaming server 边界。

## 12.15 重建顺序

先用 FakeRuntime 实现按 UID 串行 worker和 desired/actual diff；加入 restart backoff、status、heartbeat；再实现 volume readiness、probe、graceful termination和 checkpoint；最后接真实 CRI、认证和资源管理。

