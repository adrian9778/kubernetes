[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)

# kubectl run 全路径走读
> **层级**：第二层——一个真实、最小、可运行的成功场景，从入口追踪到最终结果，只看主成功路径。
> **场景**：`kubectl run nginx --image=nginx --restart=Never`
> **基准**：adrian @ `6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，Go 1.27.4）。

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

### 3.7 各跳成功判据 / 失败点 / 可观察判据速查

本节是第二层的「排障速查表」：每跳给出一句话的成功判据、最主要的失败点、以及**不进函数体就能观察到的信号**（事件 reason、Pod 字段/condition、可见结果）。函数级细节留待第三、四层。

| 跳 | 成功判据 | 最主要失败点 | 可观察判据（事件 reason / 字段 / condition） |
|---|---|---|---|
| 客户端 POST | apiserver 返回 201 + Pod 对象 | 同名 Pod 已存在（409 冲突）、认证/鉴权失败（401/403） | `kubectl run` 终端打印 `pod/nginx created`；无即未成功 |
| apiserver 写 etcd | Pod 落 etcd，`spec.nodeName` 为空 | 准入拒绝、声明式校验失败（422 Invalid）、etcd 冲突 | `kubectl get pod` 能看到 `STATUS=Pending`（已持久化、未调度） |
| scheduler 选节点 + bind | `spec.nodeName` 被填入 | 无可行节点（资源/亲和/污点）、抢占也失败 | 事件 `Scheduled`（Normal，reason=Binding，sched.finishBinding+9）；失败为 `FailedScheduling`（Warning，:976） |
| kubelet 创建 sandbox + 容器 | sandbox 与全部容器 Running | 镜像拉取失败、PreCreate/PreStart 钩子失败、资源不足被 kubelet 拒绝 | 事件 `Pulling`/`Pulled`/`Failed`（拉镜像，`pkg/kubelet/events/event.go:47-49`）；`Started`/`Failed`（建/启容器，:22-23） |
| 状态回报 | Pod status 回写 apiserver/etcd | PATCH 冲突（重读重试）、Pod 被删（丢弃更新） | Pod condition `PodScheduled`、`ContainersReady`、`Ready` 依次为 True（m.updateStatusInternal+36、:659-660）；`STATUS=Running` |

**用这张表排障的顺序：**
1. 先看 `kubectl get pod` 的 `STATUS`：`Pending` 说明还没到 kubelet（卡在前两跳）；`Running` 说明前四跳全通。
2. `STATUS=Pending` 时看 `kubectl describe pod` 的 Events：有 `FailedScheduling` → 卡在调度跳；无任何事件 → 卡在 apiserver 写入或 watch 未送达。
3. `STATUS` 非 Running 但已调度（`spec.nodeName` 非空）→ 卡在 kubelet 跳，看节点上 `Pulling`/`Failed`/`Started` 事件定位是镜像还是容器启动问题。
4. condition 的 `LastTransitionTime`（见第三层 03 篇）能给出每跳的**切换时刻**，用于判断卡在哪个跳、卡了多久。

**边界（本层不展开）：** 每个失败点内部的函数级分支（如镜像 GC 触发、抢占、退避重试）属第三、四层，见 03/07/09/10 篇。

### 3.8 完整 HTTP 报文解剖（第二层补充视角）

本节把 3.1 的 POST 展开成**线上的完整 HTTP 报文**：请求行、请求头、请求体、响应行、响应体。所有头部与编码都来自 client-go 的 `rest.Request` 构造链（`createResource` → `Post/NamespaceIfScoped/Resource/VersionedParams/Body/Do/Get`）。

#### 请求行与 URL 构造

**源码：** `staging/src/k8s.io/client-go/rest/request.go` · `Request.URL` `+0`、`staging/src/k8s.io/cli-runtime/pkg/resource/helper.go` · `Helper.createResource` `+0`

```text
POST /api/v1/namespaces/default/pods HTTP/1.1
```
URL 由 `Request.URL`（:556）拼装：
- `pathPrefix` = `/api/v1`（核心组，见篇 04 `ClientForMapping` 的 `/api` vs `/apis` 分流）
- `namespaceSet && namespace` → 追加 `/namespaces/default`（`NamespaceIfScoped`，m.createResource+2）
- `resource` → 追加 `/pods`（m.createResource+4）
- name 为空（Create 不带 name，由服务端/对象内 name 定）
- query：`VersionedParams(options, ParameterCodec)`（m.createResource+6）追加 `dryRun`/`fieldManager`/`fieldValidation`（本场景均空）

#### 请求头

| 头 | 值（本场景） | 来源锚点 |
|---|---|---|
| `Authorization` | `Bearer <token>` | `rt.RoundTrip+12`（`SetHeader("Authorization", "Bearer "+token)`，`NewBearerAuthRoundTripper` :285） |
| `Accept` | `application/vnd.kubernetes.protobuf,application/json` | `r.UseProtobufAsDefault+2`（设 `AcceptContentTypes`，默认 protobuf 优先） |
| `Content-Type` | `application/json` | `r.Body+36`（`Body` 内 `SetHeader("Content-Type", ContentType)`）；未设时按 `ContentConfig+7` 注释回落 `application/json` |
| `User-Agent` | `kubectl/vX.Y.Z (os/arch) kubernetes/<commit>` | `buildUserAgent+0` 格式串 / `DefaultKubernetesUserAgent+0` |
| `Content-Length` | body 字节数 | `http` 传输层 |

`Accept` 用 protobuf 优先：client-go 默认 `AcceptContentTypes = "application/vnd.kubernetes.protobuf,application/json"`（r.UseProtobufAsDefault+2）——能接受 protobuf 就用（省带宽），否则回落 JSON。`Content-Type` 则由 `Body` 内 `Negotiator.Encoder(ContentType)`（:530）按对象序列化后设置。

#### 请求体

**源码：** `request.go 函数：Body 偏移：+0`（:510）

`Body(obj)` 对 `runtime.Object` 走 `Negotiator.Encoder(ContentType)` → `runtime.Encode`（:530 ～ :539）序列化，并设 `Content-Type`。本场景序列化后的 body 即 §3.1 的 Pod 清单（`apiVersion: v1` / `kind: Pod` / `metadata.name: nginx` / `spec.containers[0].image: nginx` / `restartPolicy: Never`），以 `application/json` 编码为 JSON 字节。

#### 响应行与响应体

**源码：** `request.go 函数：transformResponse 偏移：+0`（:1186）、`函数：Do 偏移：+0`（:1142）

```text
HTTP/1.1 201 Created
Content-Type: application/json
```
- `Do`（:1142）调 `request`（:1043）发 HTTP，回调 `transformResponse`（:1186）。
- `transformResponse`：读 body → 取 `Content-Type`（空则回落 `ContentType`）→ `Negotiator.Decoder` 解码器 → 按 status code 分流。
- **2xx**（:1242 附近，`<200` 或 `>206` 为错误）：decode body 为 `runtime.Object`，`Result{obj, body}`。
- **非 2xx**：`transformUnstructuredResponseError` 把错误体转 `*apierrors.StatusError`（409 冲突 / 422 Invalid / 403 等）。
- body 再经 `Helper.createResource` 的 `.Get()`（m.Patch+0）decode 成存回的 Pod（含服务端分配的 `UID`、`ResourceVersion`）。

#### 请求/响应报文对照

| 维度 | 请求（kubectl → apiserver） | 响应（apiserver → kubectl） |
|---|---|---|
| 行 | `POST /api/v1/namespaces/default/pods HTTP/1.1` | `HTTP/1.1 201 Created` |
| 身份 | `Authorization: Bearer <token>` | — |
| 内容协商 | `Accept: ...protobuf,application/json` | `Content-Type: application/json` |
| 体 | Pod 清单（JSON） | 存回的 Pod（含 UID/RV，`spec.nodeName=""`） |
| 失败形态 | — | 409 AlreadyExists / 422 Invalid / 403 Forbidden |

**设计要点（源码可证）：**
1. **内容协商双向**：`Accept`（client 能收什么，protobuf 优先）与 `Content-Type`（client 发什么，JSON）由 `Negotiator` 分别决定——读写可用不同编码。
2. **身份用 Bearer token，凭证用后由 apiserver 侧认证中间件校验**（见篇 05 认证 filter）；client-go 只负责把 token 放进 `Authorization` 头。
3. **URL 由 builder 链拼装**：`Post/NamespaceIfScoped/Resource/VersionedParams/Body` 各设一段，最终 `URL`（:556）拼成完整路径——命令本身不拼 URL。
4. **非 2xx 统一转 StatusError**：`transformResponse` 把错误体 decode 成 `StatusError`，使上层能按 `Reason`（AlreadyExists/Invalid/Forbidden）分支。

## 4. 本层刻意不展开的内容
异常分支（调度失败、镜像拉取失败、etcd 冲突、准入拒绝）、边界情况、PLEG 健康检查细节、探针、非核心组件、控制器调谐，全部留待第三、四层。

## 5. 与旧版的定位差异（本次修订）
- **关键修正**：`RunFilterPluginsWithNominatedPods` 与 `RunScorePlugins` 的定义在 `pkg/scheduler/framework/runtime/framework.go`（frameworkImpl 方法，`:1467`/`:1598`），**不在 `pkg/scheduler/algorithm.go`**。旧文档把这两个符号写在 algorithm.go 属文档定位错误——algorithm.go 内的是调用点（`findNodesThatFitPod` 调 `RunFilterPluginsWithNominatedPods`，调用点偏移：+155），定义从未在 algorithm.go。
- **不要混淆**：`pkg/scheduler/algorithm.go` 确实另有 `SchedulingAlgorithm.SchedulePod`（偏移：+0）与 `SchedulingAlgorithm.findNodesThatFitPod`（偏移：+0），二者是调度算法自身的不同符号，与 frameworkImpl 的过滤/打分方法不是一回事。
- **零源码移动**：本场景其余 44 个符号在基准 commit 均存在且位置与证据索引一致；仅上述两处符号为文档定位修正，非代码移动造成。

[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)
