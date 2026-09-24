# 03. cmd：进程入口、配置装配和生命周期

## 3.1 通用入口

多数主组件的 `main` 很薄：创建 Cobra command，交给 component-base CLI runner，错误转换为退出码。真正装配位于 `cmd/<component>/app`。

```text
main -> NewXCommand -> RunE
     -> logging/feature/version
     -> Options.Complete/Validate
     -> Config/CompletedConfig
     -> Run(ctx,...)
```

## 3.2 kube-apiserver

关键文件：[入口](../../../cmd/kube-apiserver/apiserver.go)、[server.go](../../../cmd/kube-apiserver/app/server.go)、[options](../../../cmd/kube-apiserver/app/options)、[config.go](../../../cmd/kube-apiserver/app/config.go)。

当前主链：`NewAPIServerCommand -> Run -> NewConfig -> CreateServerChain -> PrepareRun -> RunWithContext`。`CreateServerChain` 组装 API Extensions、Kube API Server 和 Aggregator delegation chain。Options 包含 secure serving、etcd、audit、authentication、authorization、admission、egress selector、service network 等。

## 3.3 kube-controller-manager

关键文件：[入口](../../../cmd/kube-controller-manager/controller-manager.go)、[controllermanager.go](../../../cmd/kube-controller-manager/app/controllermanager.go)、[controller_descriptor.go](../../../cmd/kube-controller-manager/app/controller_descriptor.go)。

主链：`NewControllerManagerCommand -> Config -> Run -> leader election -> CreateControllerContext -> BuildControllers -> RunControllers`。ControllerContext 持有 shared informers、metadata informer、RESTMapper、discovery、client builders 和 GC 等公共依赖。ControllerDescriptor 描述名称、别名、初始化器和 feature 条件。

## 3.4 kube-scheduler

关键文件：[入口](../../../cmd/kube-scheduler/scheduler.go)、[server.go](../../../cmd/kube-scheduler/app/server.go)、[options](../../../cmd/kube-scheduler/app/options)。

主链：`NewSchedulerCommand -> runCommand -> Setup -> scheduler.New -> Run`。Setup 构建 client、static/dynamic informers、event broadcaster、leader election 和插件 registry/profile。当前版本还会按 feature gate 安装 PodGroup、CompositePodGroup 和 DRA 相关 informer/插件。

## 3.5 kubelet

关键文件：[入口](../../../cmd/kubelet/kubelet.go)、[server.go](../../../cmd/kubelet/app/server.go)、[options](../../../cmd/kubelet/app/options)。

主链：`NewKubeletCommand -> Run -> run -> RunKubelet -> NewMainKubelet -> startKubelet`。启动前处理 bootstrap kubeconfig、客户端证书轮换、CRI endpoint、cgroup、静态 Pod、认证授权和 Dependencies 注入。Kubelet 通常不做 leader election，因为每个节点需要自己的唯一执行代理。

## 3.6 kube-proxy

关键文件：[入口](../../../cmd/kube-proxy/proxy.go)、[server.go](../../../cmd/kube-proxy/app/server.go)。`NewProxyCommand` 读取并转换 KubeProxyConfiguration，创建 clients/informers，选择 nftables、iptables、IPVS 或 Windows proxier，启动 config controllers 和 sync loop。

## 3.7 kubectl 与 kubeadm

kubectl 的命令实现权威路径主要在 `staging/src/k8s.io/kubectl/pkg/cmd`，`cmd/kubectl` 负责二进制装配。Factory/RESTClientGetter 延迟创建 discovery、RESTMapper、dynamic/typed clients，resource.Builder 把文件、stdin、label selector 和 resource args 统一为访问链。

kubeadm 位于 `cmd/kubeadm/app`，围绕 init/join/reset/upgrade 构建 workflow phases。它生成证书、kubeconfig、静态 Pod manifests 和 bootstrap token，但不是长期控制面进程。

## 3.8 信号和优雅停止

顶层 context 收到 SIGTERM 后，组件应停止接收新工作、关闭队列、等待 worker、执行 pre-shutdown hooks、排空 HTTP 非长期请求、终止 watch，并 flush audit/log。API Server 的停机最复杂；Controller/Scheduler 失去领导权通常退出进程，避免旧 goroutine 与新 leader 并行写。

## 3.9 健康端点

- livez：进程是否仍有能力运行；失败可能触发重启。
- readyz：是否可以接流量，常依赖 post-start hooks、storage 和 informer sync。
- healthz：历史兼容入口，各组件语义不同。

健康检查不能做昂贵外部操作，也不能把短暂可恢复延迟轻易升级为重启风暴。

## 3.10 教学版进程骨架

```go
func Run(ctx context.Context, cfg CompletedConfig) error {
    server := newHealthServer(cfg)
    go server.Run(ctx)
    factory := newInformerFactory(cfg.Client)
    controller := NewController(factory.Objects(), cfg.Client)
    factory.Start(ctx.Done())
    if !cache.WaitForCacheSync(ctx.Done(), controller.HasSynced) {
        return errors.New("initial cache sync failed")
    }
    return controller.Run(ctx, cfg.Workers)
}
```

生产版本还需要日志、metrics、trace、feature gates、认证、leader election、config versioning 和有界停机。

