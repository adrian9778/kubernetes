[上一篇](18-cloud-controller-manager.md) · [总目录](README.md) · [下一篇](20-CSI存储插件.md)

# kubeadm 集群引导

> **层级**：第四层——函数级源码解析。
> **范围**：kubeadm 命令组织、init 阶段工作流、静态 Pod 控制面落盘、join 与 bootstrap token。
> **基准**：adrian@`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，工作区干净），Go 1.27.4。

---

## 1. 生产视角：kubeadm 对外行为与可见结果

kubeadm 是**引导**（bootstrap）而不是**管理**集群的工具：它把「一台裸机 + 一个容器运行时」变成「一个可运行的 Kubernetes 控制面 + 可加入的工作节点」，并留下可直接交给 `kubectl` 使用的 kubeconfig。它不做编排、不维护组件运行，控制面组件由 kubelet 以**静态 Pod** 方式拉起。

### 1.1 两条主命令，两种角色

| 命令 | 节点角色 | 做了什么 | 可见结果 |
|---|---|---|---|
| `kubeadm init` | 控制面节点 | 生成 PKI/证书、写 kubeconfig、落静态 Pod manifests、启动 kubelet、装 addons、打印 join 命令 | `/etc/kubernetes/` 下的 pki/、manifests/、*.conf；`kubectl get nodes` 出现 control-plane 节点 |
| `kubeadm join` | 工作节点（可带 `--control-plane` 提升为控制面） | 用 bootstrap token 向集群注册、拉取集群配置、启动 kubelet、申请并轮转证书 | 节点被自动批准 CSR 并 Ready；control-plane 模式额外加入 etcd 与静态 Pod |

### 1.2 引导顺序的关键语义

1. **先有 kubelet，再有控制面**：kubeadm 先把控制面组件的 manifests 写到 `/etc/kubernetes/manifests/`，再启动 kubelet。kubelet 的 file source 监控该目录，见到文件即通过 CRI 启动对应组件——控制面是 kubelet「带」出来的，不是 kubeadm 直接 spawn 的。
2. **证书引导先于正式证书**：kubelet 先用 bootstrap token 向 apiserver 发起 CSR 申请客户端证书，预置的 RBAC（`AutoApproveNodeBootstrapTokens`）自动批准；之后走 `AutoApproveNodeCertificateRotation` 轮转，kubelet 拿到正式证书即脱离 bootstrap 身份。
3. **init 是确定性的**：同一份 `InitConfiguration` + 同一环境，产出可复现的证书目录与 manifests，便于审计与重放。

---

## 2. 开发视角

### 2.1 模块级

| 项 | 内容 |
|---|---|
| 入口 | `cmd/kubeadm/kubeadm.go#main+0`（:24）→ `app/kubeadm.go#Run+0`（:32） |
| 命令 | `cmd/kubeadm/app/cmd/`（cmd.go、init.go、join.go、reset.go、upgrade/、alpha/） |
| 阶段实现 | `cmd/kubeadm/app/cmd/phases/`（init/、join/、workflow/）与 `cmd/kubeadm/app/phases/`（certs、kubeconfig、controlplane、etcd、kubelet、bootstraptoken、addons、markcontrolplane、uploadconfig、copycerts、upgrade） |
| 数据流（init） | InitConfiguration + 集群环境 → 分阶段执行 → 写 PKI/kubeconfig/manifests → 启动 kubelet → 等控制面就绪 → 装 addons |

> **修正说明（相对旧版）**：kubeadm 实现代码**未迁移**到 `staging/src/k8s.io/kubeadm/`。`staging/src/k8s.io/kubeadm/` 不存在，`go.mod` 无任何 kubeadm 模块引用，kubeadm 仍属主模块 `k8s.io/kubernetes`，完整实现（含 `app/`、`apis/`、`discovery/`、`util/`、`constants/`）都在 `cmd/kubeadm/` 下，并非只剩 main 引导。

### 2.2 类型级

| 类型 | 文件 | 职责 |
|---|---|---|
| `initOptions` / `initData` | `cmd/kubeadm/app/cmd/init.go` | init 全部参数与运行数据（cfg、clientset、cert dir、dry-run） |
| `joinOptions` / `joinData` | `cmd/kubeadm/app/cmd/join.go` | join 参数（token、discovery、control-plane 模式） |
| workflow `Runner` | `cmd/kubeadm/app/cmd/phases/workflow/runner.go#Runner+0`（:48） | 按 `AppendPhase`（:113）的顺序执行阶段，支持 phases 子命令单跑；`NewRunner`（:106）、`SetDataInitializer`（:174）、`InitData`（:181）、`Run`（:193）、`BindToCommand`（:317） |
| `Phase` | `cmd/kubeadm/app/cmd/phases/workflow/phase.go` | 阶段接口：Name/Short/Example/Init/Run/PostRun |

> **修正说明（相对旧版）**：workflow 包实际路径为 `cmd/kubeadm/app/cmd/phases/workflow/`（旧文写 `app/cmd/workflow/`），Runner 定义在 `Runner+0`。

### 2.3 命令组织函数级

#### 函数：Run / NewKubeadmCommand
**源码：** `cmd/kubeadm/app/kubeadm.go#Run+0`（:32，:47 调 `cmd.NewKubeadmCommand`）/ `cmd/kubeadm/app/cmd/cmd.go#NewKubeadmCommand+0`（:32）
**子命令注册（`NewKubeadmCommand+51~+62`，`NewKubeadmCommand` 内）：** certs（:83）、completion（:84）、config（:85）、init（:86）、join（:87）、reset（:88）、version（:89）、token（:90）、upgrade（`upgrade.NewCmdUpgrade`，:91）、alpha（:92）、kubeconfig（:94）。

> **修正说明（相对旧版）**：子命令注册起始由旧文「:82 起」变为 **:83 起**（:83～:94，+1）；旧文把范围写成「:82 ～ +95」，现按逐条 AddCommand 精确列位。

#### 命令构造函数

| 命令 | 函数 | 锚点 |
|---|---|---|
| init | `newCmdInit` | `cmd/kubeadm/app/cmd/init.go#newCmdInit+0`（:112） |
| join | `newCmdJoin` | `cmd/kubeadm/app/cmd/join.go#newCmdJoin+0`（:165） |
| reset | `newCmdReset` | `cmd/kubeadm/app/cmd/reset.go#newCmdReset+0`（:214） |
| upgrade | `NewCmdUpgrade` | `cmd/kubeadm/app/cmd/upgrade/upgrade.go#NewCmdUpgrade+0`（:44） |

### 2.4 init 主流程函数级

#### 函数：newCmdInit 的 RunE
**源码：** `cmd/kubeadm/app/cmd/init.go#newCmdInit+0`（:112）
**执行流程：** `workflow.NewRunner()` → `initRunner.SetDataInitializer`（`newInitData`，:307：加载配置、生成证书材料、准备 clientset）→ `initRunner.Run` 按阶段顺序执行；阶段构造器在 `cmd/kubeadm/app/cmd/phases/init/`。

#### init 阶段顺序（`newCmdInit+45~+58`，`newCmdInit` 内 AppendPhase）
```text
NewPreflightPhase        # 环境检查（端口、内核、容器运行时、swap）
NewCertsPhase            # 证书与 CA
NewKubeConfigPhase       # admin/controller-manager/scheduler/kubelet kubeconfig
NewEtcdPhase             # 本地 etcd 静态 Pod 配置
NewControlPlanePhase     # 控制面静态 Pod manifests
NewKubeletStartPhase     # 写 kubelet config 并尝试启动
NewWaitControlPlanePhase # 等 apiserver /healthz
NewUploadConfigPhase     # 把 ClusterConfiguration 存 kube-system ConfigMap
NewUploadCertsPhase      # （可选）上传加密证书
NewMarkControlPlanePhase # 给 Node 打 control-plane label/taint
NewBootstrapTokenPhase   # 创建 bootstrap token 与 RBAC
NewKubeletFinalizePhase
NewAddonPhase            # CoreDNS + kube-proxy
NewShowJoinCommandPhase  # 打印 join 命令
```

#### newInitData 深度拆解（数据初始化，先于任何阶段执行）

`newInitData`（`init.go#newInitData+0`，:307）在 workflow Runner 跑阶段**之前**执行，把命令行 flags + 配置文件归一成一份内部 `InitConfiguration`，并做全套前置校验。它是「配置解析 + 校验 + dry-run/外部 CA 准备」的汇聚点。

**执行流程（`newInitData+0`，:307 起）：**
1. **公共 API 默认化**：`kubeadmscheme.Scheme.Default(externalInitCfg)` / `.Default(externalClusterCfg)`（+3 ～ +5）——只填未被 flag 暴露/未设置的值。
2. **FeatureGate 解析**：`features.NewFeatureGate(&InitFeatureGates, featureGatesString)`（+11 ～ +14）。
3. **互斥参数校验**：`validation.ValidateMixedArguments(cmd.Flags())`（+17 ～ +19）。
4. **bootstrap token 应用**：`initOptions.bto.ApplyTo(externalInitCfg)`（+21 ～ +23）。
5. **配置加载或默认化**：`configutil.LoadOrDefaultInitConfiguration(cfgPath, externalInitCfg, externalClusterCfg, {SkipCRIDetect})`（+26 ～ +33）——**有配置文件用文件，否则把公共 API 转内部 InitConfiguration**。
6. **忽略的 preflight 错误归并**：`ValidateIgnorePreflightErrors`（+35 ～ +38）+ 把并集写回 `cfg.NodeRegistration.IgnorePreflightErrors`（+41 ～ +43，保证运行时配置视图一致）。
7. **命令行覆盖节点名**：`externalInitCfg.NodeRegistration.Name != ""` 时覆盖 `cfg.NodeRegistration.Name`（+46 ～ +49）。
8. **API Server 绑定地址校验**：`VerifyAPIServerBindAddress(AdvertiseAddress)`（+51 ～ +53）。
9. **版本与特性门校验**：`features.ValidateVersion(InitFeatureGates, cfg.FeatureGates, KubernetesVersion)`（+55 ～ +57）。
10. **dry-run 临时目录**：`initOptions.dryRun || cfg.DryRun` 时经 `GetDryRunDir(EnvVarInitDryRunDir, "kubeadm-init-dryrun")`（+60 ～ +66）建临时目录，所有生成文件落此处而非真实路径。
11. **外部 CA 检测**：`certsphase.UsingExternalCA(&cfg.ClusterConfiguration)`（+69）——**CA 证书在而 CA Key 不在**即为外部 CA；此时 `ValidateKubeconfigsForExternalCA`（+80）校验所需 kubeconfig 已存在且有效（kubeadm 无 CA Key 无法重新生成）。
12. **外部 Front-Proxy CA 检测**（+90 起，同逻辑）。

**关键分支：外部 CA 与 dry-run（源码可证）：**
| 场景 | 判定 | 行为 |
|---|---|---|
| 外部 CA | CA Cert 在、CA Key 不在 | 不重新签发证书；校验 kubeconfig 已存在有效 |
| dry-run | `initOptions.dryRun \|\| cfg.DryRun` | 建临时目录，产物不落真实 `/etc/kubernetes` |
| 命令行节点名覆盖 | `externalInitCfg.NodeRegistration.Name != ""` | 覆盖配置文件中的节点名 |

**设计要点（源码可证）：**
1. **校验前置、阶段纯执行**：所有配置归一与校验都在 `newInitData` 完成，workflow 各阶段只消费已校验的 `initData`——阶段内部不再解析 flag。
2. **默认值分层**：`Scheme.Default` 只补「未被 flag 暴露的值」，flag 显式值不被覆盖；命令行节点名最后再覆盖一次，优先级最高。
3. **dry-run 隔离**：通过 `GetDryRunDir` 把产物重定向到临时目录，使 `--dry-run` 可安全演练而不污染节点。
4. **外部 CA 不可逆**：一旦检测到外部 CA，kubeadm 放弃证书生成，转而校验用户提供的 kubeconfig——因为无 CA Key 无法重签。

#### phases 入口函数

| 阶段 | 函数 | 锚点 |
|---|---|---|
| certs | `CreatePKIAssets(cfg *kubeadmapi.InitConfiguration)` | `cmd/kubeadm/app/phases/certs/certs.go#CreatePKIAssets+0`（:45） |
| kubeconfig | `CreateKubeConfigFile(kubeConfigFileName, outDir, cfg)` | `cmd/kubeadm/app/phases/kubeconfig/kubeconfig.go#CreateKubeConfigFile+0`（:110） |
| controlplane | `CreateInitStaticPodManifestFiles(manifestDir, patchesDir, cfg, isDryRun)` | `cmd/kubeadm/app/phases/controlplane/manifests.go#CreateInitStaticPodManifestFiles+0`（:44） |
| etcd | `CreateLocalEtcdStaticPodManifestFile(manifestDir, patchesDir, nodeName, cfg, endpoint, isDryRun)` | `cmd/kubeadm/app/phases/etcd/local.go#CreateLocalEtcdStaticPodManifestFile+0`（:57） |
| kubelet | `TryStartKubelet()` | `cmd/kubeadm/app/phases/kubelet/kubelet.go#TryStartKubelet+0`（:29） |
| token | `UpdateOrCreateTokens` | `cmd/kubeadm/app/phases/bootstraptoken/node/token.go#UpdateOrCreateTokens+0`（:40） |
| tlsbootstrap | `AutoApproveNodeBootstrapTokens` | `cmd/kubeadm/app/phases/bootstraptoken/node/tlsbootstrap.go#AutoApproveNodeBootstrapTokens+0`（:90） |
| addons | `EnsureDNSAddon` / `EnsureProxyAddon` | `cmd/kubeadm/app/phases/addons/dns/dns.go#EnsureDNSAddon+0`（:90）、`cmd/kubeadm/app/phases/addons/proxy/proxy.go#EnsureProxyAddon+0`（:54） |

### 2.5 静态 Pod 控制面落盘函数级

#### 函数：CreateInitStaticPodManifestFiles
**源码：** `cmd/kubeadm/app/phases/controlplane/manifests.go#CreateInitStaticPodManifestFiles+0`（:44）
**调用链：**
1. `CreateStaticPodFiles`（manifests.go#CreateStaticPodFiles+0，:123）
2. `GetStaticPodSpecs`（manifests.go#GetStaticPodSpecs+0，:51）：用 `staticpodutil.ComponentPod`（`cmd/kubeadm/app/util/staticpod/utils.go#ComponentPod+0`，:63）组装 kube-apiserver、kube-controller-manager、kube-scheduler 三个 `v1.Pod`（含镜像、command、hostNetwork、挂载证书目录）
3. 每组件 patch 后 `staticpodutil.WriteStaticPodToDisk(componentName, manifestDir, spec)`（`cmd/kubeadm/app/util/staticpod/utils.go#WriteStaticPodToDisk+0`，**:191**）
4. 路径经 `kubeadmconstants.GetStaticPodFilepath`（`cmd/kubeadm/app/constants/constants.go#GetStaticPodFilepath+0`，:603）：默认 `/etc/kubernetes/manifests/<name>.yaml`
**衔接 kubelet：** manifests 目录正是 kubelet file source 的静态 Pod 目录（见 09 篇 PodConfig），kubelet 见到文件即经 CRI 启动控制面组件，形成"先有 kubelet、静态 Pod 带出控制面"的引导顺序。etcd 同由 `GetEtcdPodSpec` + 同一路径落盘。

> **修正说明（相对旧版）**：`WriteStaticPodToDisk` 定义由旧文「:175」移至 **:191**（+16）；`ComponentPod`、`GetStaticPodFilepath` 锚点零漂移（:63 / :603）。

### 2.6 join 与 bootstrap token 函数级

#### join 阶段（`newCmdJoin+57~+64`，`newCmdJoin` 内 AppendPhase）
```text
Preflight → ControlPlanePrepare（仅 control-plane 节点）→ CheckEtcd
→ KubeletStart → EtcdJoin → KubeletWaitBootstrap
→ ControlPlaneJoin → WaitControlPlane
```

#### 关键函数

| 函数 | 锚点 | 职责 |
|---|---|---|
| `token.BuildInsecureBootstrapKubeConfig` | 定义 `cmd/kubeadm/app/discovery/token/token.go#BuildInsecureBootstrapKubeConfig+0`（:155）；调用点 `cmd/kubeadm/app/cmd/join.go#joinData.Client+16`（:598，`joinData.Client` 内） | 用 token 建临时 bootstrap kubeconfig |
| `configutil.FetchInitConfigurationFromCluster` | 定义 `cmd/kubeadm/app/util/config/cluster.go#FetchInitConfigurationFromCluster+0`（:58）；调用点 `cmd/kubeadm/app/cmd/join.go#fetchInitConfiguration+4`（:722） | 拉集群配置（API bind 端口、版本） |
| `CreateBootstrapConfigMapIfNotExists` | `cmd/kubeadm/app/phases/bootstraptoken/clusterinfo/clusterinfo.go#CreateBootstrapConfigMapIfNotExists+0`（:42） | 建 `cluster-info` ConfigMap |
| `NewKubeletStartPhase` | `cmd/kubeadm/app/cmd/phases/join/kubelet.go#NewKubeletStartPhase+0`（:50） | TryStopKubelet → WriteInstanceConfigToDisk/WriteConfigToDisk → kubeletphase.TryStartKubelet（`NewKubeletStartPhase+180`，调用点） |

> **修正说明（相对旧版）**：`BuildInsecureBootstrapKubeConfig` 与 `FetchInitConfigurationFromCluster` 的**定义**均不在 join.go 内——分别迁移至 `discovery/token/token.go#BuildInsecureBootstrapKubeConfig+0` 与 `util/config/cluster.go#FetchInitConfigurationFromCluster+0`，join.go 的调用点分别为 `joinData.Client+16`（:598）与 `fetchInitConfiguration+4`（:722）——均不在 `newCmdJoin` 体内（旧文按「join.go 内定义」且归到 `newCmdJoin` 偏移需更正）。

**证书引导语义：** kubelet 先用 bootstrap token 向 apiserver 申请客户端证书（CSR），`AutoApproveNodeBootstrapTokens` 等预置 RBAC 自动批准；后续证书轮转走 `AutoApproveNodeCertificateRotation`。kubelet 取得正式证书后脱离 bootstrap 身份。

### 2.7 upgrade / reset 定位
- `upgrade apply`：从 ClusterConfiguration 比对版本，更新静态 Pod manifests（控制面组件滚动重建）与 kubelet 配置；`upgrade node` 处理节点侧。入口 `cmd/kubeadm/app/cmd/upgrade/upgrade.go#NewCmdUpgrade+0`（:44），子命令 apply/plan/diff/node。
- `reset`：清 kubelet 本地状态、证书与 etcd 数据目录，回滚 init 对节点文件系统的改动。入口 `cmd/kubeadm/app/cmd/reset.go#newCmdReset+0`（:214）。

### 2.8 调用关系总览
```text
main（`main+0`）→ Run（`Run+0`）→ NewKubeadmCommand（`NewKubeadmCommand+0`）
 └── init RunE（`newCmdInit+0`）: newInitData（`newInitData+0`）→ workflow.Runner.Run
      ├── CreatePKIAssets / CreateKubeConfigFile
      ├── CreateLocalEtcdStaticPodManifestFile
      ├── CreateInitStaticPodManifestFiles
      │    └── CreateStaticPodFiles → GetStaticPodSpecs → ComponentPod → WriteStaticPodToDisk
      ├── TryStartKubelet（bootstrap token + CSR）
      ├── WaitControlPlane
      ├── UpdateOrCreateTokens / AutoApproveNodeBootstrapTokens
      └── EnsureDNSAddon / EnsureProxyAddon
 join（`newCmdJoin+0`）: BuildInsecureBootstrapKubeConfig（定义 `BuildInsecureBootstrapKubeConfig+0`）
      → CreateBootstrapConfigMapIfNotExists → TryStartKubelet →（control-plane）ControlPlaneJoin
```

---

## 3. 设计意图

kubeadm 的设计把「引导」与「运行」彻底分离：它只负责把集群**装配出来**，装配完成后把控制面组件交给 kubelet 的静态 Pod 机制，自己退出。这样做的收益：

1. **可重放**：静态 Pod manifests 是幂等的——同一份配置重跑 `init` 得到相同文件，方便审计与灾备。
2. **符合 Kubernetes 组件模型**：控制面组件仍是普通 kubelet 管理的 Pod，而不是 kubeadm 私有的进程，`kubectl` 与监控体系无需特殊对待。
3. **阶段可裁剪**：workflow Runner 的 `AppendPhase` + `--skip-phases` 让用户能精确控制「做到哪一步」，适合分阶段排障或自定义拓扑。
4. **bootstrap token 让加入零预配**：新节点凭一次性 token 自动注册、自动批 CSR，不需要管理员预先为每个节点签发证书。

## 4. 验证

本稿按当前代码基准核实：所有源码锚点以「符号+偏移」（函数定义行=+0）标注，关键定义行已逐一 grep 复核；kubeadm 未迁移 staging、定义文件迁移（`BuildInsecureBootstrapKubeConfig+0`、`FetchInitConfigurationFromCluster+0`）与 `WriteStaticPodToDisk` 定位（`WriteStaticPodToDisk+0`）均按当前值照抄。无消失符号。

| 核对项 | 结果 |
|---|---|
| kubeadm 是否在 staging | 否（`staging/src/k8s.io/kubeadm/` 不存在，go.mod 无引用） |
| `cmd/kubeadm/` 是否只剩 main | 否（仍含完整 `app/`、`apis/`、`discovery/`、`util/`、`constants/`） |
| `WriteStaticPodToDisk` 定义 | `WriteStaticPodToDisk+0`（旧 :175，+16） |
| `BuildInsecureBootstrapKubeConfig` 定义 | `BuildInsecureBootstrapKubeConfig+0`（调用点 `joinData.Client+16`） |
| `FetchInitConfigurationFromCluster` 定义 | `FetchInitConfigurationFromCluster+0`（调用点 `fetchInitConfiguration+4`） |
| workflow Runner 路径 | `cmd/kubeadm/app/cmd/phases/workflow/runner.go#Runner+0`（旧文 `app/cmd/workflow/` 需更正） |
| 其余锚点 | 零漂移（main :24、Run :32、NewKubeadmCommand :32、newCmdInit :112、newInitData :307、newCmdJoin :165、newCmdReset :214、NewCmdUpgrade :44、phases 全部、ComponentPod :63、GetStaticPodFilepath :603、NewKubeletStartPhase :50、TryStartKubelet 调用点 :230、CreateBootstrapConfigMapIfNotExists :42） |

---

[上一篇](18-cloud-controller-manager.md) · [总目录](README.md) · [下一篇](20-CSI存储插件.md)
