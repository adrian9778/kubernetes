[上一篇](18-cloud-controller-manager.md) · [总目录](README.md) · [下一篇](20-CSI存储插件.md)

# kubeadm 集群引导
> **层级**：第四层——函数级源码解析。
> **范围**：kubeadm 命令组织、init 阶段工作流、静态 Pod 控制面落盘、join 与 bootstrap token。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口 | `cmd/kubeadm/kubeadm.go#main` → `app/kubeadm.go#Run`（:32） |
| 命令 | `app/cmd/`（cmd.go、init.go、join.go、reset.go、upgrade/、alpha/） |
| 阶段实现 | `app/phases/`（certs、kubeconfig、controlplane、etcd、kubelet、bootstraptoken、addons） |
| 数据流（init） | InitConfiguration + 集群环境 → 分阶段执行 → 写 PKI/kubeconfig/manifests → 启动 kubelet → 等控制面就绪 → 装 addons |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `initOptions`/`InitData` | `app/cmd/init.go` | init 全部参数与运行数据（cfg、clientset、cert dir、dry-run） |
| workflow `Runner` | `app/cmd/workflow` | 按 AppendPhase 的顺序执行阶段，支持 phases 子命令单跑 |
| `joinOptions` | `app/cmd/join.go` | join 参数（token、discovery、control-plane 模式） |

## 3. 命令组织函数级

### 函数：Run / NewKubeadmCommand
**源码：** `app/kubeadm.go:32` / `app/cmd/cmd.go:32`
**子命令（cmd.go:82 ～ +95）：** init、join、reset、config、token、certs、kubeconfig、version、completion、upgrade（`upgrade.NewCmdUpgrade`）、alpha。

### 命令构造函数
| 命令 | 函数 | 行 |
|---|---|---|
| init | `newCmdInit` | `cmd/init.go:112` |
| join | `newCmdJoin` | `cmd/join.go:165` |
| reset | `newCmdReset` | `cmd/reset.go:214` |
| upgrade | `NewCmdUpgrade` | `cmd/upgrade/upgrade.go:44`（子命令 apply/plan/diff/node，:62 ～ +65） |

## 4. init 主流程函数级

### 函数：newCmdInit 的 RunE
**源码：** `cmd/init.go`
**执行流程：** `workflow.NewRunner()` → `initRunner.InitData`（`newInitData:307`：加载配置、生成证书材料、准备 clientset）→ `initRunner.Run` 按阶段执行。

### init 阶段顺序（init.go:157 ～ +169，构造器在 `cmd/phases/init/`）
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

### phases 入口函数
| 阶段 | 函数 | 行 |
|---|---|---|
| certs | `CreatePKIAssets(cfg *kubeadmapi.InitConfiguration)` | `phases/certs/certs.go:45` |
| kubeconfig | `CreateKubeConfigFile(...)` | `phases/kubeconfig/kubeconfig.go:110` |
| controlplane | `CreateInitStaticPodManifestFiles(manifestDir, patchesDir, cfg, isDryRun)` | `phases/controlplane/manifests.go:44` |
| etcd | `CreateLocalEtcdStaticPodManifestFile(...)`（堆叠式 :140） | `phases/etcd/local.go:57` |
| kubelet | `TryStartKubelet()` | `phases/kubelet/kubelet.go:29`；`WriteConfigToDisk`（config.go:46） |
| token | `UpdateOrCreateTokens` | `phases/bootstraptoken/node/token.go:40`；`AutoApproveNodeBootstrapTokens`（tlsbootstrap.go:90） |
| addons | `EnsureDNSAddon` / `EnsureProxyAddon` | `phases/addons/dns/dns.go:90`、`addons/proxy/proxy.go:54` |

## 5. 静态 Pod 控制面落盘函数级

### 函数：CreateInitStaticPodManifestFiles
**源码：** `phases/controlplane/manifests.go:44`
**调用链：**
1. `CreateStaticPodFiles`（:123）
2. `GetStaticPodSpecs`（:51）：用 `staticpodutil.ComponentPod`（`util/staticpod/utils.go:63`）组装 kube-apiserver、kube-controller-manager、kube-scheduler 三个 `v1.Pod`（含镜像、command、hostNetwork、挂载证书目录）
3. 每组件 patch 后 `staticpodutil.WriteStaticPodToDisk(componentName, manifestDir, spec)`（:175）
4. 路径经 `kubeadmconstants.GetStaticPodFilepath`：默认 `/etc/kubernetes/manifests/<name>.yaml`
**衔接 kubelet：** manifests 目录正是 kubelet file source 的静态 Pod 目录（见 09 篇 PodConfig），kubelet 见到文件即经 CRI 启动控制面组件，形成"先有 kubelet、静态 Pod 带出控制面"的引导顺序。etcd 同由 `GetEtcdPodSpec` + 同一路径落盘。

## 6. join 与 bootstrap token 函数级

### join 阶段（join.go:222 ～ +229）
```text
Preflight → ControlPlanePrepare（仅 control-plane 节点）→ CheckEtcd
→ KubeletStart → EtcdJoin → KubeletWaitBootstrap
→ ControlPlaneJoin → WaitControlPlane
```
### 关键函数
| 函数 | 行 | 职责 |
|---|---|---|
| `token.BuildInsecureBootstrapKubeConfig` | join.go:597 | 用 token 建临时 bootstrap kubeconfig |
| `configutil.FetchInitConfigurationFromCluster` | join.go:722 | 拉集群配置（API bind 端口、版本） |
| `CreateBootstrapConfigMapIfNotExists` | `phases/bootstraptoken/clusterinfo/clusterinfo.go:42` | 建 `cluster-info` ConfigMap；RBAC 规则 :83 |
| NewKubeletStartPhase | `cmd/phases/join/kubelet.go:50` | TryStopKubelet → WriteInstanceConfigToDisk/WriteConfigToDisk → kubeletphase.TryStartKubelet（:230） |

**证书引导语义：** kubelet 先用 bootstrap token 向 apiserver 申请客户端证书（CSR），`AutoApproveNodeBootstrapTokens` 等预置 RBAC 自动批准；后续证书轮转走 `AutoApproveNodeCertificateRotation`。kubelet 取得正式证书后脱离 bootstrap 身份。

## 7. upgrade / reset 定位
- `upgrade apply`：从 ClusterConfiguration 比对版本，更新静态 Pod manifests（控制面组件滚动重建）与 kubelet 配置；`upgrade node` 处理节点侧。
- `reset`：清 kubelet 本地状态、证书与 etcd 数据目录，回滚 init 对节点文件系统的改动。

## 8. 调用关系总览
```text
main → Run → NewKubeadmCommand
 └── init RunE: newInitData → Runner.Run
      ├── CreatePKIAssets / CreateKubeConfigFile
      ├── CreateLocalEtcdStaticPodManifestFile
      ├── CreateInitStaticPodManifestFiles
      │    └── GetStaticPodSpecs → ComponentPod → WriteStaticPodToDisk
      ├── TryStartKubelet（bootstrap token + CSR）
      ├── WaitControlPlane
      ├── UpdateOrCreateTokens
      └── EnsureDNSAddon / EnsureProxyAddon
 join: BuildInsecureBootstrapKubeConfig → CreateBootstrapConfigMapIfNotExists
       → TryStartKubelet →（control-plane）ControlPlaneJoin
```

[上一篇](18-cloud-controller-manager.md) · [总目录](README.md) · [下一篇](20-CSI存储插件.md)
