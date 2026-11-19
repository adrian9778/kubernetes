[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)

# kubectl run 全路径走读
> **层级**：第二层——一个真实、最小、可运行的成功场景，从入口追踪到最终结果，只看主成功路径。
> **场景**：`kubectl run nginx --image=nginx --restart=Never`
> **基准**：`k8s.io/kubernetes` commit `8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，master，工作区干净），Go 1.27.4。

## 1. 场景说明
`--restart=Never` 让 kubectl 直接创建一个裸 Pod（不带 Deployment/ReplicaSet），主路径最短。命令结束后集群中存在一个 Pod：`spec.nodeName` 由调度器填入，容器进入 Running，状态经 kubelet 回报。

生产视角的主链路（每跳给出「触发+输入 → 动作 → 结果/异常」）：

```text
kubectl 进程
  └─ 构造 Pod 对象 → POST /api/v1/namespaces/default/pods
       ↓
kube-apiserver
  └─ 认证 → 鉴权 → mutating 准入 → 校验 → validating 准入 → 写 etcd
       ↓
etcd：/registry/pods/default/nginx
       ↓ watch（两个订阅者）
kube-scheduler：过滤 + 打分 → PUT Pod/binding（spec.nodeName 确定）
       ↓
kubelet（目标节点）：同步循环 → 创建 sandbox → 拉镜像 → 创建/启动容器
       ↓
kubelet statusManager：PATCH Pod status → apiserver → etcd
       ↓
kubectl get pods：读到 Running
```

## 2. 全路径一览
上图为生产视角的流程骨架；本章把每一跳的**真实符号**列成清单，章节 3 再逐跳展开。

| 跳 | 生产动作 | 真实符号（当前基准） |
|---|---|---|
| 客户端 | 构造 Pod 并 POST | `cmd/kubectl/kubectl.go 函数：main 偏移：+0` → `staging/src/k8s.io/kubectl/pkg/cmd/cmd.go 函数：NewDefaultKubectlCommand 偏移：+0` → `[staging] k8s.io/kubectl/pkg/cmd/run/run.go 函数：RunOptions.Run 偏移：+0` → `run.go 函数：RunOptions.createGeneratedObject 偏移：+0` → `staging/src/k8s.io/cli-runtime/pkg/resource/helper.go 函数：Helper.Create 偏移：+0` |
| apiserver | HTTP 入口到 etcd | `cmd/kube-apiserver/apiserver.go 函数：main 偏移：+0` → `cmd/kube-apiserver/app/server.go 函数：NewAPIServerCommand 偏移：+0` → `server.go 函数：Run 偏移：+0` → `server.go 函数：CreateServerChain 偏移：+0` → `staging/src/k8s.io/apiserver/pkg/server/config.go 函数：DefaultBuildHandlerChain 偏移：+0` → `[staging] server/handler.go Struct：director` → `pkg/registry/core/rest/storage_core.go 函数：legacyProvider.NewRESTStorage 偏移：+0` → `pkg/registry/core/pod/storage/storage.go 函数：NewStorage 偏移：+0` → `[staging] endpoints/installer.go 函数：APIInstaller.registerResourceHandlers 偏移：+0` → `[staging] endpoints/handlers/create.go 函数：CreateResource 偏移：+0` → `create.go 函数：createHandler 偏移：+0` → `[staging] registry/generic/registry/store.go 函数：Store.Create 偏移：+0` → `store.go 函数：Store.create 偏移：+0` → `[staging] storage/etcd3/store.go 函数：store.Create 偏移：+0` → `vendor/go.etcd.io/etcd/client/v3/kubernetes/client.go 函数：Client.OptimisticPut 偏移：+0` |
| scheduler | watch 到 Pod，选节点并绑定 | `cmd/kube-scheduler/scheduler.go 函数：main 偏移：+0` → `pkg/scheduler/scheduler.go 函数：Scheduler.Run 偏移：+0` → `pkg/scheduler/schedule_one.go 函数：Scheduler.ScheduleOne 偏移：+0` → `schedule_one.go 函数：Scheduler.scheduleOnePod 偏移：+0` → `pkg/scheduler/backend/queue/scheduling_queue.go Struct：PriorityQueue` → `pkg/scheduler/algorithm.go 函数：SchedulingAlgorithm.SchedulePod 偏移：+0` → `algorithm.go 函数：SchedulingAlgorithm.findNodesThatFitPod 偏移：+0` → `pkg/scheduler/framework/runtime/framework.go 函数：frameworkImpl.RunFilterPluginsWithNominatedPods 偏移：+0` / `framework.go 函数：frameworkImpl.RunScorePlugins 偏移：+0` |
| kubelet | 收到本节点 Pod，经 CRI 启动容器 | `pkg/kubelet/config/apiserver.go 函数：NewSourceApiserver 偏移：+0` → `pkg/kubelet/config/config.go Struct：PodConfig` → `pkg/kubelet/kubelet.go 函数：Kubelet.Run 偏移：+0` → `kubelet.go 函数：Kubelet.syncLoop 偏移：+0` → `kubelet.go 函数：Kubelet.syncLoopIteration 偏移：+0` → `kubelet.go 函数：Kubelet.HandlePodAdditions 偏移：+0` → `pkg/kubelet/pod_workers.go 函数：podWorkers.UpdatePod 偏移：+0` → `pod_workers.go 函数：podWorkers.podWorkerLoop 偏移：+0` → `kubelet.go 函数：Kubelet.SyncPod 偏移：+0` → `pkg/kubelet/kuberuntime/kuberuntime_manager.go 函数：kubeGenericRuntimeManager.SyncPod 偏移：+0` → `kuberuntime_manager.go 函数：kubeGenericRuntimeManager.computePodActions 偏移：+0` → `pkg/kubelet/kuberuntime/kuberuntime_sandbox.go 函数：kubeGenericRuntimeManager.createPodSandbox 偏移：+0` → `pkg/kubelet/kuberuntime/kuberuntime_container.go 函数：kubeGenericRuntimeManager.startContainer 偏移：+0` → `staging/src/k8s.io/cri-client/pkg/remote_runtime.go 函数：NewRemoteRuntimeService 偏移：+0` |
| 状态回报 | PLEG 事件 → PATCH status | `pkg/kubelet/pleg/generic.go Struct：GenericPLEG` → `generic.go 函数：GenericPLEG.Relist 偏移：+0` → `pkg/kubelet/status/status_manager.go Struct：manager` → `status_manager.go 函数：manager.syncPod 偏移：+0` → `pkg/util/pod/pod.go 函数：PatchPodStatus 偏移：+0` |

## 3. 各跳真实定位

### 3.1 客户端：构造 Pod 并发出 POST
**触发+输入**：`kubectl run nginx --image=nginx --restart=Never`。

**动作**：
- 进程入口 `cmd/kubectl/kubectl.go 函数：main 偏移：+0` 调 `cmd.NewDefaultKubectlCommand()`（`staging/src/k8s.io/kubectl/pkg/cmd/cmd.go 函数：NewDefaultKubectlCommand 偏移：+0`）后执行，路由到 run 子命令。
- `[staging] k8s.io/kubectl/pkg/cmd/run/run.go 函数：NewCmdRun 偏移：+0`：注册 run 子命令与 flags（`--image`、`--restart`）。
- `run.go 函数：RunOptions.Run 偏移：+0`：取参数名 `nginx`，默认补 label `run=nginx`（偏移：+8～+10）。
- `run.go 函数：RunOptions.createPod 偏移：+0`：根据 image/restart 策略构造 `*corev1.Pod`；`--restart=Never` 下生成的就是 Pod 而非 Deployment。
- `run.go 函数：RunOptions.createGeneratedObject 偏移：+0`：
  - `f.ToRESTMapper()`（偏移：+1）取得 GVK→REST 映射，Pod 属于核心组 `v1`、资源名 `pods`；
  - `f.ClientForMapping(mapping)`（偏移：+28）取得 REST client；
  - `resource.NewHelper(client, mapping).Create(o.Namespace, false, obj)`（偏移：+33～+37）发出 **POST** `/api/v1/namespaces/default/pods`。

**结果/异常**：POST 成功返回 201 + 存入 etcd 后的 Pod 对象（此时 `spec.nodeName` 为空）；若同名 Pod 已存在则返回冲突错误（本场景为主路径，不展开）。

脱敏后的请求体（主成功场景核心字段）：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx
  namespace: default
  labels:
    run: nginx
spec:
  containers:
  - name: nginx
    image: nginx
  restartPolicy: Never
```

### 3.2 apiserver：从 HTTP 入口到 etcd
**触发+输入**：kubectl 的 POST 请求进入 kube-apiserver 端口。

**动作**：
- 进程入口 `cmd/kube-apiserver/apiserver.go 函数：main 偏移：+0` → `cmd/kube-apiserver/app/server.go 函数：NewAPIServerCommand 偏移：+0` → `server.go 函数：Run 偏移：+0` → `server.go 函数：CreateServerChain 偏移：+0` 构造三个委托 server（APIExtensions、KubeAPIServer、Aggregator）。
- HTTP 链：`staging/src/k8s.io/apiserver/pkg/server/config.go 函数：DefaultBuildHandlerChain 偏移：+0` 依次包裹认证（`genericapifilters.WithAuthentication`）、审计、impersonation、流控、鉴权（`WithAuthorization`）等；请求最终到 `[staging] server/handler.go Struct：director`（`func (d director) ServeHTTP`），按已注册的 go-restful webservice 分流。
- 路由来源：Pod 的 RESTStorage 由 `pkg/registry/core/rest/storage_core.go 函数：legacyProvider.NewRESTStorage 偏移：+0` 调 `pkg/registry/core/pod/storage/storage.go 函数：NewStorage 偏移：+0` 装入 `VersionedResourcesStorageMap["pods"]`；`[staging] endpoints/installer.go 函数：APIInstaller.registerResourceHandlers 偏移：+0` 检测到 storage 实现了 `rest.Creater`，把 POST 绑定到 `restfulCreateResource`（installer.go，`rest.Creater` 分支偏移：+640，`restfulCreateResource` 定义偏移：+1019）。
- 请求处理：`[staging] endpoints/handlers/create.go 函数：CreateResource 偏移：+0` → `create.go 函数：createHandler 偏移：+0`：decode body → 构造 `admission.Attributes`（Create）→ 执行 mutating admission → 调 storage 的 Create。
- 存储层：Pod 的 `storage.REST` 内嵌泛型 `[staging] registry/generic/registry/store.go 函数：Store.Create 偏移：+0`（经 `createWithGenerateNameRetry` 中转，偏移：+21）→ `store.go 函数：Store.create 偏移：+0`：经 `podStrategy`（`pkg/registry/core/pod/strategy.go Struct：podStrategy`，`var Strategy` 偏移：+7，默认化与名称生成）默认化、执行 validating admission 与声明式校验，然后 `e.Storage.Create(...)`。
- 持久化：`[staging] storage/etcd3/store.go 函数：store.Create 偏移：+0`：对象编码、加密变换、取 lease，调用 `s.client.Kubernetes.OptimisticPut(ctx, key, data, 0, PutOptions{...})`（`vendor/go.etcd.io/etcd/client/v3/kubernetes/client.go 函数：Client.OptimisticPut 偏移：+0`；本分支魔改 etcd client，见 00 篇）。key 形如 `/registry/pods/default/nginx`；key 已存在则返回冲突错误（本场景为新建，主路径成功）。

**结果/异常**：apiserver 把存回的 Pod（`spec.nodeName` 为空）编码为 201 响应返回 kubectl。

### 3.3 scheduler：watch 到 Pod，选节点并绑定
**触发+输入**：apiserver 写 etcd 后，scheduler 经 informer 收到 Pod ADD 事件（待调度队列）。

**动作**：
- 进程入口 `cmd/kube-scheduler/scheduler.go 函数：main 偏移：+0` → `app.NewSchedulerCommand`（`cmd/kube-scheduler/app/server.go`）→ `Run` → `pkg/scheduler/scheduler.go 函数：Scheduler.Run 偏移：+0`。
- `Scheduler.Run`（偏移：+0）：启动 `SchedulingQueue`（实际类型 `pkg/scheduler/backend/queue/scheduling_queue.go Struct：PriorityQueue`，偏移：+0），随后以 `Scheduler.ScheduleOne` 作为 worker（偏移：+14）。
- `pkg/scheduler/schedule_one.go 函数：Scheduler.ScheduleOne 偏移：+0`：从队首 Pop 出待调度 Pod；经 `schedule_one.go 函数：Scheduler.scheduleOnePod 偏移：+0`（`scheduleOnePod` 在 `ScheduleOne` 内调用，偏移：+19）：
  - 单 Pod 调度走 `sched.SchedulePod` → `pkg/scheduler/algorithm.go 函数：SchedulingAlgorithm.SchedulePod 偏移：+0`；
  - 节点过滤在 `algorithm.go 函数：SchedulingAlgorithm.findNodesThatFitPod 偏移：+0` 内调 `schedFramework.RunFilterPluginsWithNominatedPods(...)`（调用点偏移：+155），排除资源不足、端口冲突、污点不容忍、亲和不满足的节点；
  - 打分在 `pkg/scheduler/schedule_one.go 函数：prioritizeNodes 偏移：+0` 内调 `schedFramework.RunScorePlugins(ctx, state, pod, nodes)`（调用点偏移：+49），对候选节点打分选最高分；`prioritizeNodes` 由 `algorithm.go 函数：SchedulingAlgorithm.SchedulePod 偏移：+0` 的评分段调用（algorithm.go 调用点偏移：+42）。
- **过滤/打分插件执行**：`RunFilterPluginsWithNominatedPods` 与 `RunScorePlugins` 的定义在 `pkg/scheduler/framework/runtime/framework.go`（frameworkImpl 方法）：`framework.go 函数：frameworkImpl.RunFilterPluginsWithNominatedPods 偏移：+0`、`framework.go 函数：frameworkImpl.RunScorePlugins 偏移：+0`。**二者不在 `pkg/scheduler/algorithm.go`**；algorithm.go 中的是 `SchedulingAlgorithm.SchedulePod` 与 `SchedulingAlgorithm.findNodesThatFitPod`（是调度算法的两个不同符号，不要混淆——见章节 5）。
- 在调度缓存中 assume，随后异步执行 bind：通过 framework 的 bind 插件写 **Binding**（apiserver 侧由 Pod 的 `binding` 子资源处理），效果是 `spec.nodeName=<选中节点>` 持久化。

**结果/异常**：调度成功返回 `ScheduleResult{SuggestedHost: <节点>}`，绑定写回 apiserver/etcd，Pod 的 `spec.nodeName` 被填入。

### 3.4 kubelet：收到属于本节点的 Pod，经 CRI 启动容器
**触发+输入**：Pod `spec.nodeName` 被写入后，目标节点 kubelet 的 List/Watch 收到该 Pod。

**动作**：
- Pod 数据来源：`pkg/kubelet/config/apiserver.go 函数：NewSourceApiserver 偏移：+0` 用 `cache.NewListWatchFromClient` 只 watch `spec.nodeName=<本节点>` 的 Pod，事件汇入 `pkg/kubelet/config/config.go Struct：PodConfig`（`NewPodConfig` 偏移：+0）的更新通道。
- `pkg/kubelet/kubelet.go 函数：Kubelet.Run 偏移：+0` 启动 statusManager、PLEG 等后进入 `kubelet.go 函数：Kubelet.syncLoop 偏移：+0`；
- `kubelet.go 函数：Kubelet.syncLoopIteration 偏移：+0` 收到 ADD → `kubelet.go 函数：Kubelet.HandlePodAdditions 偏移：+0`：登记到 podManager，调 `pkg/kubelet/pod_workers.go 函数：podWorkers.UpdatePod 偏移：+0`（`UpdateType: SyncPodCreate`）。
- `pod_workers.go 函数：podWorkers.podWorkerLoop 偏移：+0`：每个 Pod 一个 goroutine，串行执行，调 Kubelet 的 `pkg/kubelet/kubelet.go 函数：Kubelet.SyncPod 偏移：+0`。
- `Kubelet.SyncPod`：准备卷与 Pod 目录后调 `containerRuntime.SyncPod`。
- `pkg/kubelet/kuberuntime/kuberuntime_manager.go 函数：kubeGenericRuntimeManager.SyncPod 偏移：+0`：`kuberuntime_manager.go 函数：kubeGenericRuntimeManager.computePodActions 偏移：+0` 算出动作（新 Pod：建 sandbox + 启全部容器）：
  - `pkg/kubelet/kuberuntime/kuberuntime_sandbox.go 函数：kubeGenericRuntimeManager.createPodSandbox 偏移：+0` → `runtimeService.RunPodSandbox`；
  - `pkg/kubelet/kuberuntime/kuberuntime_container.go 函数：kubeGenericRuntimeManager.startContainer 偏移：+0`：先拉镜像，再 `runtimeService.CreateContainer`、`runtimeService.StartContainer`。
- gRPC 落点：`staging/src/k8s.io/cri-client/pkg/remote_runtime.go 函数：NewRemoteRuntimeService 偏移：+0` 建立连接，方法调用经 `[staging] cri-api` 的 `RuntimeServiceClient` 发往容器运行时（containerd 等）。

**结果/异常**：sandbox 与容器均创建并启动成功；若镜像拉取或容器启动失败则进入对应异常分支（本场景为成功，不展开）。

### 3.5 状态回报
**触发+输入**：容器运行时侧 sandbox/container 已启动。

**动作**：
- `pkg/kubelet/pleg/generic.go Struct：GenericPLEG` 周期 `generic.go 函数：GenericPLEG.Relist 偏移：+0`，发现 sandbox/container 已启动，生成生命周期事件。
- `pkg/kubelet/status/status_manager.go Struct：manager` 后台循环调 `status_manager.go 函数：manager.syncPod 偏移：+0`，经 `pkg/util/pod/pod.go 函数：PatchPodStatus 偏移：+0` 把 `phase=Running`、容器状态等 PATCH 回 apiserver，再入 etcd。

**结果/异常**：PATCH 成功，apiserver/etcd 中 Pod 状态更新为 Running。

### 3.6 最终结果
`kubectl get pod nginx` 读到：

```text
NAME    READY   STATUS    RESTARTS   AGE
nginx   1/1     Running   0          <n>
```

## 4. 本层刻意不展开的内容
异常分支（调度失败、镜像拉取失败、etcd 冲突、准入拒绝）、边界情况、PLEG 健康检查细节、探针、非核心组件、控制器调谐，全部留待第三、四层。

## 5. 与旧版的定位差异（本次修订）
- **关键修正**：`RunFilterPluginsWithNominatedPods` 与 `RunScorePlugins` 的定义在 `pkg/scheduler/framework/runtime/framework.go`（frameworkImpl 方法，`:1467`/`:1598`），**不在 `pkg/scheduler/algorithm.go`**。旧文档把这两个符号写在 algorithm.go 属文档定位错误——algorithm.go 内的是调用点（`findNodesThatFitPod` 调 `RunFilterPluginsWithNominatedPods`，调用点偏移：+155），定义从未在 algorithm.go。
- **不要混淆**：`pkg/scheduler/algorithm.go` 确实另有 `SchedulingAlgorithm.SchedulePod`（偏移：+0）与 `SchedulingAlgorithm.findNodesThatFitPod`（偏移：+0），二者是调度算法自身的不同符号，与 frameworkImpl 的过滤/打分方法不是一回事。
- **零源码移动**：本场景其余 44 个符号在基准 commit 均存在且位置与证据索引一致；仅上述两处符号为文档定位修正，非代码移动造成。

[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)
