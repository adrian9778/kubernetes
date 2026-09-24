[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)

# kubectl run 全路径走读
> **层级**：第二层——一个真实、最小、可运行的成功场景，从入口追踪到最终结果，只看主成功路径。
> **场景**：`kubectl run nginx --image=nginx --restart=Never`

## 1. 场景说明
`--restart=Never` 让 kubectl 直接创建一个裸 Pod（不带 Deployment/ReplicaSet），主路径最短。命令结束后集群中存在一个 Pod：`spec.nodeName` 由调度器填入，容器进入 Running，状态经 kubelet 回报。

## 2. 全路径一览
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

## 3. 各跳真实定位

### 3.1 客户端：构造 Pod 并发出 POST
- `cmd/kubectl/kubectl.go#main`：调 `cmd.NewDefaultKubectlCommand()`（`[staging] k8s.io/kubectl/pkg/cmd/cmd.go`）后执行。
- `[staging] k8s.io/kubectl/pkg/cmd/run/run.go`
  - `NewCmdRun`（函数定义行 +0）：注册 run 子命令与 flags（`--image`、`--restart`）。
  - `(*RunOptions).Run`（+0 起）：取参数名 `nginx`，默认补 label `run=nginx`（+5 ～ +9）。
  - `(*RunOptions).createPod`（+0）：根据 image/restart 策略构造 `*corev1.Pod`；`--restart=Never` 下生成的就是 Pod 而非 Deployment。
  - `(*RunOptions).createGeneratedObject`（+0）：
    - `f.ToRESTMapper()`（+1）取得 GVK→REST 映射（+1 ～ +11），Pod 属于核心组 `v1`、资源名 `pods`；
    - `f.ClientForMapping(mapping)`（+31）取得 REST client；
    - `resource.NewHelper(client, mapping).Create(o.Namespace, false, obj)`（+34 ～ +38）发出 **POST** `/api/v1/namespaces/default/pods`。
- 脱敏后的请求体（主成功场景核心字段）：
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
- 进程入口：`cmd/kube-apiserver/apiserver.go#main` → `app.NewAPIServerCommand()`（`cmd/kube-apiserver/app/server.go`）→ `Run` → `CreateServerChain` 构造三个委托 server（APIExtensions、KubeAPIServer、Aggregator）。
- HTTP 链：`[staging] k8s.io/apiserver/pkg/server/config.go#DefaultBuildHandlerChain` 依次包裹认证（`genericapifilters.WithAuthentication`）、审计、impersonation、流控、鉴权（`WithAuthorization`）等；请求最终到 `[staging] server/handler.go` 的 director，按已注册的 go-restful webservice 分流。
- 路由来源：Pod 的 RESTStorage 由 `pkg/registry/core/rest/storage_core.go#(*legacyProvider).NewRESTStorage` 调 `pkg/registry/core/pod/storage#NewStorage` 装入 `VersionedResourcesStorageMap["pods"]`；`[staging] endpoints/installer.go#(*APIInstaller).registerResourceHandlers` 检测到 storage 实现了 `rest.Creater`，把 POST 绑定到 `restfulCreateResource`。
- 请求处理：`[staging] endpoints/handlers/create.go#CreateResource` → `createHandler`：decode body → 构造 `admission.Attributes`（Create）→ 执行 mutating admission → 调 storage 的 Create。
- 存储层：Pod 的 `storage.REST` 内嵌泛型 `[staging] registry/generic/registry/store.go#Store`，方法提升为 `(*REST).Create`。`Store.Create` → `Store.create`：经 `podStrategy`（`pkg/registry/core/pod/strategy.go`，默认化与名称生成）默认化、执行 validating admission 与声明式校验，然后 `e.Storage.Create(...)`。
- 持久化：`[staging] storage/etcd3/store.go#(*store).Create`：对象编码、加密变换、取 lease，调用 `s.client.Kubernetes.OptimisticPut(ctx, key, data, 0, PutOptions{...})`（本分支魔改 etcd client，见 00 篇）。key 形如 `/registry/pods/default/nginx`；key 已存在则返回冲突错误（本场景为新建，主路径成功）。
- apiserver 把存回的 Pod（此时 `spec.nodeName` 为空）编码为 201 响应返回 kubectl。

### 3.3 scheduler：watch 到 Pod，选节点并绑定
- 进程入口：`cmd/kube-scheduler/scheduler.go#main` → `app.NewSchedulerCommand`（`cmd/kube-scheduler/app/server.go`）→ `Run` → `scheduler.New`（`pkg/scheduler/scheduler.go`）。
- `pkg/scheduler/scheduler.go`：`Scheduler` 持有 `SchedulingQueue`（实际类型在 `pkg/scheduler/backend/queue#PriorityQueue`）；`Run` 启动队列后以 `ScheduleOne` 作为 worker。
- `pkg/scheduler/schedule_one.go#(*Scheduler).ScheduleOne`：从队首 Pop 出待调度 Pod；经 `scheduleOnePod`：
  - 节点过滤绑定在 `pkg/scheduler/algorithm.go` 中的 `RunFilterPluginsWithNominatedPods`，排除资源不足、端口冲突、污点不容忍、亲和不满足的节点；
  - `schedule_one.go` 中 `schedFramework.RunScorePlugins` 对候选节点打分选最高分；
  - 在调度缓存中 assume，随后异步执行 bind：通过 framework 的 bind 插件写 **Binding**（apiserver 侧由 Pod 的 `binding` 子资源处理），效果是 `spec.nodeName=<选中节点>` 持久化。

### 3.4 kubelet：收到属于本节点的 Pod，经 CRI 启动容器
- Pod 数据来源：`pkg/kubelet/config/apiserver.go#NewSourceApiserver` 用 `cache.NewListWatchFromClient` 只 watch `spec.nodeName=<本节点>` 的 Pod，事件汇入 `pkg/kubelet/config/config.go#PodConfig` 的更新通道。
- `pkg/kubelet/kubelet.go`
  - `Run` 启动 statusManager、PLEG 等后进入 `syncLoop`；
  - `syncLoopIteration` 收到 ADD → `HandlePodAdditions`：登记到 podManager，调 `podWorkers.UpdatePod`（`pkg/kubelet/pod_workers.go`，`UpdateType: SyncPodCreate`）。
- `pkg/kubelet/pod_workers.go#podWorkerLoop`：每个 Pod 一个 goroutine，串行执行，调 Kubelet 的 `SyncPod`。
- `pkg/kubelet/kubelet.go#(*Kubelet).SyncPod`：准备卷与 Pod 目录后调 `containerRuntime.SyncPod`。
- `pkg/kubelet/kuberuntime/kuberuntime_manager.go#(*kubeGenericRuntimeManager).SyncPod`：`computePodActions` 算出动作（新 Pod：建 sandbox + 启全部容器）：
  - `kuberuntime_sandbox.go#createPodSandbox` → `runtimeService.RunPodSandbox`；
  - `kuberuntime_container.go#startContainer`：先拉镜像，再 `runtimeService.CreateContainer`、`runtimeService.StartContainer`。
- gRPC 落点：`[staging] k8s.io/cri-client/pkg/remote_runtime.go#NewRemoteRuntimeService` 建立连接，方法调用经 `[staging] cri-api` 的 `RuntimeServiceClient` 发往容器运行时（containerd 等）。

### 3.5 状态回报
- `pkg/kubelet/pleg/generic.go#GenericPLEG` 周期 `Relist`，发现 sandbox/container 已启动，生成生命周期事件。
- `pkg/kubelet/status/status_manager.go#manager` 后台循环调 `syncPod`，经 `PatchPodStatus`（`pkg/util/pod/pod.go`）把 `phase=Running`、容器状态等 PATCH 回 apiserver，再入 etcd。

### 3.6 最终结果
`kubectl get pod nginx` 读到：

```text
NAME    READY   STATUS    RESTARTS   AGE
nginx   1/1     Running   0          <n>
```

## 4. 本层刻意不展开的内容
异常分支（调度失败、镜像拉取失败、etcd 冲突、准入拒绝）、边界情况、PLEG 健康检查细节、探针、非核心组件、控制器调谐，全部留待第三、四层。

[上一篇](01-系统骨架.md) · [总目录](README.md) · [下一篇](03-主链路逐步拆解.md)
