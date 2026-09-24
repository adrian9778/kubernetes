# 15. kubectl、kubeadm、cloud-provider 与辅助组件

## 15.1 kubectl 根架构

入口 `cmd/kubectl` 很薄，主要源码在 `staging/src/k8s.io/kubectl/pkg/cmd` 和 `cli-runtime`。`ConfigFlags -> Factory -> command Options -> Complete -> Validate -> Run` 是标准链。

Factory 提供 RESTConfig、DiscoveryClient、RESTMapper、typed/dynamic client、Resource.Builder、validator、OpenAPI 和 printer。子命令不应自行重复解析 kubeconfig。

## 15.2 Resource Builder

Builder 统一 `TYPE NAME`、`TYPE/NAME`、文件、目录、URL、stdin、label/field selector，经过 mapper、decoder、visitor 产生 resource.Info。ContinueOnError、Flatten、Latest 等选项影响访问行为。

## 15.3 apply

Client-side apply 计算 last-applied diff；Server-Side Apply 发送 apply patch、fieldManager 和 force，由 API Server structured-merge-diff 处理字段所有权。kubectl apply 还负责 schema validation、prune、namespace 和输出。

## 15.4 打印

printers 将 runtime.Object 转 table、JSON/YAML、name、custom columns、jsonpath/go-template。Server-side Table 可减少客户端对类型知识依赖；wide columns来自 resource printer handlers/OpenAPI。

## 15.5 exec/logs/port-forward

这些命令不是普通 CRUD：先定位 Pod/container，再使用升级连接或流协议与 API Server/Kubelet 通信。context、终端 raw mode、resize stream、stdin/stdout 和错误流都需正确关闭。

## 15.6 kubectl 插件

未知命令可映射 PATH 中 `kubectl-<name>`，Unix 通过 exec 替换进程。当前根命令还包含 kuberc preferences、warnings-as-errors、command headers、OpenAPI v3 和插件 completion。插件无法向现有 Cobra 树任意注入内部对象，只通过进程协议扩展。

## 15.7 kubeadm

`cmd/kubeadm/app/cmd/phases` 将 init/join/upgrade/reset 拆为可组合 phase。Init 生成 CA/证书、kubeconfig、静态 Pod manifests、bootstrap token、RBAC 和 addons；Join 做 discovery、TLS bootstrap 和节点配置；Upgrade 处理版本/配置迁移。

Control plane 组件通常作为静态 Pod 由 Kubelet 管理。kubeadm 管理初始化制品，不替代长期 controllers。

## 15.8 cloud-controller-manager

可复用框架在 `staging/src/k8s.io/cloud-provider/app`。Cloud provider initializer 注入云接口；controller constructors 启动 Node、NodeLifecycle、Route、Service 等。现代云厂商应构建 out-of-tree CCM，不复制内树 cmd。

Leader Migration 支持控制器在 kube-controller-manager 与 cloud-controller-manager 之间平滑所有权迁移。

## 15.9 ServiceAccount 与证书

ServiceAccount controllers 创建默认资源和 token 相关状态；TokenRequest API 生成短期、受 audience 约束的 token。CertificateSigningRequest controllers 负责 approve/sign 等分工。外部 JWT signer 和证书插件通过明确 API 边界扩展。

## 15.10 Metrics 和 component-base

`component-base` 提供日志、metrics registry、featuregate、compatibility、tracing、version、configz 和 CLI。稳定 metrics 有生命周期规则，不能随意改名/label。FeatureGate 需注册版本、默认值和锁定状态，组件兼容版本通过 registry 固化。

## 15.11 辅助 cmd

`cmd/gendocs`、genfeaturegates、import-boss、dependencyverifier、preferredimports 等服务于仓库验证和生成，不是集群组件。阅读它们主要理解输入、规则文件和 verify 链，而非运行时架构。

