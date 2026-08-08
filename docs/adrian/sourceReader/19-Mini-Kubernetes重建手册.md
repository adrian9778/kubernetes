# 19. 从零实现 Mini Kubernetes

## 19.1 目标

实现机制相同的教学系统：版本化对象、API/存储/watch、Informer/队列、控制器、调度器、节点代理、Service 规则和测试。它不追求生产安全、Kubernetes wire compatibility 或真实多租户。

## 19.2 项目结构

```text
cmd/{apiserver,controller-manager,scheduler,node-agent,proxyctl}
pkg/{runtime,api,storage,watch,client,cache,workqueue}
pkg/controllers/replica
pkg/scheduler
pkg/node
pkg/proxy
test/integration
```

## 19.3 阶段一：对象系统

实现 TypeMeta/ObjectMeta、GVK/GVR、Scheme、JSON codec、DeepCopy、default/conversion。定义 Node、Workload、ReplicaSet、Service、EndpointSlice。

验收：未知 GVK 明确错误；版本 round-trip；DeepCopy 隔离；metadata 保留。

## 19.4 阶段二：存储和 API

内存 Store 支持 CRUD/List/Watch，单调 revision，resourceVersion CAS，namespace/name key。HTTP 安装 discovery 和资源 routes。

验收：AlreadyExists、Conflict、NotFound；watch 从版本续接；慢 watcher 不阻塞全部写。

## 19.5 阶段三：Reflector/Informer

REST client、ListWatcher、Reflector List+Watch、DeltaFIFO、Indexer、listeners、Lister、HasSynced。

验收：watch 断线 relist；版本过期恢复；tombstone；多个 handler 共享 watch。

## 19.6 阶段四：Workqueue 和 Replica Controller

实现 dirty/processing、delaying、rate limiter。Controller 观察 parent/child，以 key reconcile，owner UID、expectations、status。

验收：0->3、3->1；处理中再 Add；缓存延迟不重复创建；进程重启恢复。

## 19.7 阶段五：Scheduler

active/backoff/unschedulable queue；Node cache/snapshot；Filter/Score；Assume；异步 Bind；FailureHandler。

验收：资源不足等待；Node 增加激活；Bind 慢不超分配；Bind 失败回滚。

## 19.8 阶段六：Node Agent

Watch 本节点 Workload；UID 串行 worker；FakeRuntime Inspect/Apply；restart backoff；status/heartbeat；graceful termination。

验收：绑定启动；失败退避；删除优雅停止；agent 重启从 runtime 恢复。

## 19.9 阶段七：Volume 与 Probe

实现 DSW/ASW、operation executor、fake mount、readiness/liveness/startup probe。容器只在 volume ready 后启动。

验收：mount 失败重试；旧 probe 结果不污染新 container ID；unmount 幂等。

## 19.10 阶段八：Service Proxy

Endpoint controller 只发布 Ready workload；proxy 编译 ServicePort->Backends RuleSet；fake dataplane事务 apply。

验收：readiness 移除；selector 更新；apply 失败后 full sync；删除清理。

## 19.11 阶段九：安全与 HA

加入 mTLS/token authn、rule authorizer、mutating/validating admission、audit、leader election、health/ready、metrics、graceful shutdown。

## 19.12 阶段十：PodGroup/DRA

先实现 minCount PodGroup和组级 placement/rollback；再实现 Device inventory、Claim、Allocation、Prepare/Unprepare。不要一开始做组合组和真实设备插件。

## 19.13 故障注入

每阶段注入：API 409/429/500、watch 断线、cache delay、leader loss、Bind fail、runtime timeout、mount fail、dataplane partial fail。系统必须最终收敛且没有 goroutine 泄漏。

## 19.14 最终故事

启动 API、Controller Manager、Scheduler、两个 Node Agent、Proxy。创建 replicas=3 和 Service；自动创建、调度、运行、Ready 和 endpoints。随机重启任一进程、断一次 watch、制造一次 conflict/Bind/runtime/dataplane 失败后仍收敛；删除 parent 后级联清理全部资源。

