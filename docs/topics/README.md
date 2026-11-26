# Kubernetes 源码走读 · 机制纵深专题索引

> 本目录（`docs/topics/`）是与 [`sourceReader/`](../sourceReader/README.md) 同级的**独立专题库**，单独汇总索引。
> 每篇按「代码基准 + 结论速览 → 生产视角 → 开发视角 → 验证」组织，源码引用统一用**符号+偏移**锚点（不写绝对行号，抗漂移）。
> 适合在读完主线篇后，针对某个子系统做机制纵深深挖。
>
> 代码基准：adrian@`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。

## 调度

| 专题 | 主题 | 一句话定位 |
|---|---|---|
| [调度器抢占 preemption](调度器抢占-preemption.md) | kube-scheduler 抢占 | PostFilter→Preempt→DryRun→SelectCandidate→Actuate 全链：选受害者、PDB tie-break、nominated node、异步驱逐 |
| [scheduler 过滤打分框架](scheduler过滤打分框架.md) | kube-scheduler 调度周期 | SchedulePod 两阶段：PreFilter→Filter（并行逐节点）→PostFilter、PreScore→Score（分×权重选节点）；10 个扩展点接口 |
| [v1.37 PodGroup 与组合调度](v1.37-PodGroup与组合调度.md) | 组调度（GenericWorkload） | PodGroup 入队→组调度周期→minCount→placement 生成/评分→CompositePodGroup 父子层级→失败 condition 与回队 |
| [DRA 动态资源分配](DRA动态资源分配.md) | Dynamic Resource Allocation | ResourceSlice/Claim/DeviceClass 四类对象→调度器 dynamicresources 插件各扩展点→resourceclaim controller→kubelet 准备/取消准备→devicetainteviction 设备级驱逐 |

## apiserver / storage

| 专题 | 主题 | 一句话定位 |
|---|---|---|
| [API Server 全景](api-server.md) | kube-apiserver 全链路 | 三层委托链（aggregator→kubeAPIServer→apiextensions）装配、路由注册、40+ 层中间件链、CRUD handlers、etcd3 storage/加密、watch cache、聚合代理、discovery |
| [etcd3 写入路径](etcd3写入路径.md) | apiserver storage 层 | Create/Update→OptimisticPut 乐观事务：key 准备、编码、加密变换、lease、冲突重试循环 |
| [etcd watch 链路](etcd-watch链路.md) | 服务端 watch 实时推送 | etcd3 watcher.Watch→watchChan（sync 先全量后 startWatching 增量、processEvents 背压不丢事件）→Cacher 共享内存 buffer→apiserver watch handler 流式推客户端 |
| [优先级与公平性流控](优先级与公平性流控.md) | apiserver APF 流控 | WithPriorityAndFairness→Handle→startRequest：FlowSchema 分类、PriorityLevel 排队、并发席位、429 拒绝 |
| [admission webhook](admission-webhook.md) | admission webhook 扩展点 | mutating（Admit→Dispatch→callAttrMutatingHook 应用 JSON Patch）+ validating（Validate→Dispatch allow/deny）；两种触发（内联 Admit / storage 写内 AdmissionToValidateObjectFunc）；fail-open/closed |
| [aggregator 代理转发](aggregator代理转发.md) | kube-aggregator 代理转发 | proxyHandler.ServeHTTP 五个早退守卫→ResolveEndpoint 后端解析→NewAuthProxyRoundTripper 身份透传→NewUpgradeAwareHandler 转发；transport 复用 |
| [CRD 建立与懒分发](CRD建立与懒分发.md) | CRD 动态注册与懒分发 | crdHandler 注册 NonGoRestfulMux /apis→ServeHTTP 分发→getOrCreateServingInfoFor 懒安装（atomic.Value 缓存）；establish 控制器置 Established；CRD 清理 finalizer |
| [CRD conversion webhook](CRD-conversion-webhook.md) | CRD 多版本转换 | 转换器工厂按策略分派（nopConverter/webhookConverter）→webhookConverter.Convert（batch 打包→webhook /convert→validateConvertedObject 校验）→safeConverterWrapper 防御性拷贝 |
| [apiserver discovery 与 OpenAPI](apiserver-discovery与OpenAPI.md) | discovery 与 OpenAPI 生成 | discovery 三层 handler（root `/apis`→group→version）+aggregated discovery 聚合一次返回；OpenAPI v2/v3；CRD/聚合 API 动态并入 |
| [apiserver 操作使用](apiserver操作使用.md) | 操作 apiserver 的实操视角 | 操作方式（kubectl/curl/informer）、凭证生成与查看、URL 拼接规则、建 Pod/Service、监听集群变化、高频报错对照 |

## kubelet

| 专题 | 主题 | 一句话定位 |
|---|---|---|
| [kubelet 同步循环与 PLEG](kubelet同步循环与PLEG.md) | kubelet 主循环与状态探测 | 三源 PodConfig→syncLoop 事件分派→podWorkers per-Pod 串行→SyncPod；GenericPLEG 周期 Relist 生成容器事件驱动自愈 |
| [kubelet 探针机制](kubelet探针机制.md) | kubelet 探针 | prober_manager→worker→doProbe→prober：HTTP/TCP/exec/gRPC 探测、阈值判定、容器重启与 readiness |
| [kubelet image pull 与背压](kubelet-image-pull背压.md) | kubelet 镜像拉取与背压 | EnsureImageExists 拉取决策→pull backoff（指数退避）+QPS 节流+parallel/serial puller 并发控制；image GC 按磁盘水位清理 |
| [kubelet volume manager](kubelet-volume-manager.md) | kubelet 卷挂载调谐 | 双状态模型（desired/actual）→reconciler 调谐循环→operationexecutor 两阶段挂载（attach→mount）；populator 异步填充 desired |
| [设备插件注册与 Allocate](设备插件注册与Allocate.md) | 设备管理器与设备插件 gRPC | 三条 gRPC 通道（Register / ListAndWatch 流式 / Allocate 按需）→GetDeviceRunContainerOptions→makeDevices 进 CRI config；kubelet 不直接调 CNI |

## 控制器 / 网络 / client-go

| 专题 | 主题 | 一句话定位 |
|---|---|---|
| [ReplicaSet 副本控制](replicaset副本控制.md) | ReplicaSet 副本控制（基础控制器范式） | informer→queue→worker→syncReplicaSet→manageReplicas：expectations 抑制、slowStartBatch 指数批量创建、getPodsToDelete 删除优先级、calculateStatus 回写 |
| [Deployment 滚动更新](deployment滚动更新.md) | Deployment 滚动更新 | syncDeployment 策略分派→rolloutRolling 先扩后缩（maxSurge/maxUnavailable）、比例伸缩、回滚=模板回退 |
| [DaemonSet 控制器](daemonset控制器.md) | DaemonSet 节点-Pod 一一对应 | syncDaemonSet→manage→podsShouldBeOnNode/nodeShouldRunDaemonPod 三层准入；ReplaceDaemonSetPodNodeNameNodeAffinity 注入 hostname 亲和；rollingUpdate |
| [Job 与 CronJob 周期调度](job与cronjob周期调度.md) | Job/CronJob 周期调度 | Job：syncJob+tracking finalizer 精确计数+manageJob+backoff；CronJob：syncCronJob 周期调度+并发策略 Allow/Forbid/Replace |
| [garbage-collector 依赖图与级联删除](garbage-collector依赖图与级联删除.md) | GC 依赖图与级联删除 | GraphBuilder 依赖图装配→isDangling 判定→attemptToDeleteItem 按 finalizer 选策略；Foreground/Background/Orphan 三路径 |
| [kube-proxy 规则生成](kube-proxy规则生成.md) | kube-proxy 规则生成 | 事件回调→changes 缓存→BoundedFrequencyRunner 限频 Sync→syncProxyRules 全量重建 iptables 三级链概率负载均衡；ipvs/nftables 同构定位 |
| [conntrack 与 ServiceCIDR](conntrack与ServiceCIDR.md) | conntrack 清理与 ServiceCIDR | CleanStaleEntries（筛陈旧→删死路径）+ flowFilter 去重；ServiceCIDRConfig 独立 informer |
| [informer resync 机制](informer-resync机制.md) | client-go informer | resyncPeriod 确定→shouldResync 判定→distribute(sync=true) 仅投需 resync 的 handler→OnUpdate(old==new) 自愈 |
| [workqueue 机制](workqueue机制.md) | client-go workqueue | 基础队列（Add/Get/Done 去重+脏标记）+ delaying（AddAfter 延迟）+ rate-limiting（指数退避重试）三层包装 |
| [leader election 机制](leader-election机制.md) | client-go leader election | Run→acquire→renew→tryAcquireOrRenew：Lease 竞争、快慢路径、写门、Recovery 恢复 |
| [PodCertsSigner 服务签名](podcertssigner服务签名.md) | 服务证书签名控制器 | Pod 通过 PodCertificateRequest 为 Service webhook/APIService 申请 TLS 证书：模板映射→准入判定→签发→状态回写 |

## 运维 / 元

| 专题 | 主题 | 一句话定位 |
|---|---|---|
| [Docker Desktop 内置 Kubernetes 使用](docker中集群的使用.md) | Docker Desktop 自带单节点集群（kubeadm/kind 两形态） | 两形态判别、启用与跑通、常用操作速查、YAML 写法、curl 访问 apiserver 四种方式、网络配置、本环境特有坑 |
| [测试与质量体系](测试与质量体系.md) | 四维质量保障 | 单元（fake clientset + reactor）/集成（真实 testserver）/e2e（Ginkgo）/verify-update 静态门禁四层，选层原则与替身 |
| [构建发布与代码生成](构建发布与代码生成.md) | 构建/生成/发布 | `make update`（`*-gen` 再生代码）/`make verify`（只读门禁）/`make test`/`build/release*.sh`；`+k8s:` 标注驱动的生成机制 |

## 约定

- **新增专题**：在本目录下建 `中文名.md`，按单篇专题骨架（先总结、再生产、再开发、末验证）写，并在上方对应分类表追加一行。
- **源码引用**：统一「符号+偏移」记法（`` `pkg/path/file.go` · `Symbol` `+0` ``），不写绝对行号；行号由读者用 `sr_tools offset` 实时获取。
- **主线阅读**：系统骨架→主链路的循序渐进路径见 [`sourceReader/README.md`](../sourceReader/README.md)，本目录是其机制纵深补充。
