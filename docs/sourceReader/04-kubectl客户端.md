[上一篇](03-主链路逐步拆解.md) · [总目录](README.md) · [下一篇](05-apiserver启动与路由.md)

# kubectl 客户端
> **层级**：第四层——函数级源码解析。
> **范围**：进程启动、命令树装配、run 命令执行、REST 请求的最终发出。
> **代码基准**：`master@8893923938107170c86eb4b8cc2b7225b4e44e9b`（2026-11-18，`cmd/`、`staging/`、`pkg/` 工作区干净，仅 `docs/` 有改动）；Go 1.27.4（本机实测 `go version` 报 `go1.27.1`，基准声明与实测存在小版本差）。
> **锚点记法**：`<符号>+<偏移>`，函数定义行 = `+0`；仅认证插件导入一处按证据要求保留绝对锚点。

## 0. 生产视角

### 0.1 对外角色
`kubectl` 是 Kubernetes 的命令行客户端，**生产入口是二进制**：用户安装 `kubectl` 后，在 shell 中直接调用 `kubectl run NAME --image=IMAGE [flags]`。它不做 daemon、不常驻，是一次性进程：解析 argv → 装配命令树 → 执行一个子命令 → 打印结果并退出。与集群的交互全部经 kubeconfig 与 API server 的 REST 接口。

### 0.2 命令用法（以 run 为例）
```text
kubectl run NAME --image=IMAGE [--env=KEY=VALUE] [--port=PORT]
    [--restart=Always|OnFailure|Never] [--dry-run=server|client]
    [--overrides=INLINE-JSON] [--command] -- [COMMAND] [args...]
```
- `--restart` 默认 `Always`：映射为 Deployment（本场景以 `--restart=Never` → 裸 Pod 为主）。
- `--image` 必填（`MarkFlagRequired`），缺了直接报错。
- `--field-manager` 默认 `kubectl-run`，写入 server 端字段管理（SIG 字段所有权）。

### 0.3 可见结果
成功时终端打印 `pod/nginx created`（默认打印器，带 `created` 前缀）；失败时打印 `error` 与可操作的提示，退出码非零。创建成功后 Pod 的 `UID`、`ResourceVersion` 由 server 分配并写回对象。

---

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口模块 | `cmd/kubectl`（包 `main`），仅做启动引导 |
| 命令实现 | `[staging] k8s.io/kubectl/pkg/cmd`（命令树）、`pkg/cmd/run`（run 命令）、`pkg/cmd/util`（Factory 等） |
| 通用运行时 | `[staging] k8s.io/cli-runtime/pkg/genericclioptions`（ConfigFlags）、`.../resource`（Helper、builder） |
| 依赖 | `[staging] k8s.io/client-go`（RESTClient）、`k8s.io/apimachinery`（scheme、序列化） |
| 对外接口 | 二进制 CLI；进程内通过 `cmdutil.Factory` 向各命令暴露 client/mapper/config |
| 数据流 | argv → flags/Options → 资源对象 → RESTClient → HTTP |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `RunOptions` | `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go` | run 命令全部 flags 与运行参数的载体（Image、Restart 策略、DryRun、PrintFlags 等） |
| `RunObject` | `run/run.go` | 包装已创建对象及其 `RESTMapping` |
| `Factory`（interface） | `kubectl/pkg/cmd/util/factory.go` | 命令与集群之间的工厂：`ToRESTConfig`、`ToRESTMapper`、`ClientForMapping` |
| `ConfigFlags` | `cli-runtime/pkg/genericclioptions/config_flags.go` | 持有 kubeconfig 上下文、namespace 等用户配置，能产出 RESTConfig 与各类 client |
| `Helper` | `cli-runtime/pkg/resource/helper.go` | 对 RESTClient 的薄封装，提供 Create/Patch/Get/Replace 等动词 |

## 3. 函数级

### 3.1 函数：main
**源码：** `cmd/kubectl/kubectl.go`
**所属：** `package main`
**职责：** 预解析日志级别、构造根命令、执行并处理错误。
**参数：** 无（读 `os.Args`）。
**返回值：** 无；错误经 `util.CheckErr` 漂亮打印并以非零码退出。
**执行流程：**
1. 从 argv 预解析 `-v`，`logs.GlogSetter(...)`（`main+7`）——在正常 flag 解析前先设 klog 级别，保证命令构造（含插件、kuberc）期间就能打日志。
2. `cmd.NewDefaultKubectlCommand()` 构造完整命令树（`main+8`）。
3. `cli.RunNoErrOutput(command)` 执行（`main+9`，内部完成 flag 解析与 RunE 调度）。
4. 有错走 `util.CheckErr`（`main+11`）。
**调用关系：** 调用 `NewDefaultKubectlCommand`、`cli.RunNoErrOutput`、`util.CheckErr`；被系统 `runtime` 调用（Go 程序入口）。
**错误处理：** 自身不返回错误；`RunNoErrOutput` 的 error 交给 `CheckErr` 打印并 `os.Exit(1)`。
**副作用：** 进程退出码；认证插件经空导入注册（见下）。

**认证插件导入（关键修正 1）：**
`cmd/kubectl/kubectl.go` **文件行 28**：`_ "k8s.io/client-go/plugin/pkg/client/auth"`。这是**绝对锚点**，不在 `main` 函数体内（相对 `main` 为 `-3`，在函数之前），旧文以 `+25~+26` 记法无法一致复现，已弃用。其作用是空导入注册所有 client-go 认证插件（gcp/oidc/azure 等），使 `run` 等命令能按 kubeconfig 选择认证方式。

### 3.2 函数：NewDefaultKubectlCommand / NewKubectlCommand
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/cmd.go`
**所属：** 包 `cmd`
**职责：** 装配 cobra 根命令：构造 `Factory`、注册全部子命令（含 run）、处理插件与 kuberc。
**参数/返回值：** `NewDefaultKubectlCommand() *cobra.Command`；`NewKubectlCommand(o KubectlOptions) *cobra.Command`。
**执行流程：**
1. `NewDefaultKubectlCommand`（自身 `+0`）：构造 `IOStreams`，组装 `KubectlOptions`（插件处理器、argv、ConfigFlags、IOStreams），转调 `NewDefaultKubectlCommandWithArgs`（`+2`）。
2. `NewDefaultKubectlCommandWithArgs`（自身 `+0`）：先 `NewKubectlCommand`（`+1`）；若配置了插件处理器且有参数，尝试 `cmd.Find`，按 `help`/`__complete` 等特殊命令与子命令是否存在，决定是否 `HandlePluginCommand`（外部插件扩展）。
3. `NewKubectlCommand`（自身 `+0`）：构造根 `cobra.Command`（`Use: "kubectl"`、`PersistentPreRunE`/`PersistentPostRunE` 做 warning handler 与 profiling）；注册全局 flags；处理 kuberc 偏好；`cmdutil.NewFactory(matchVersionKubeConfigFlags)`（`+73`）构造工厂；随后注册全部子命令（create/get/debug/run/expose 等）与插件命令。
**调用关系：** 链 `NewDefaultKubectlCommand` → `NewDefaultKubectlCommandWithArgs` → `NewKubectlCommand`；后者调 `cmdutil.NewFactory`。**被调用：** `main`。
**错误处理：** 插件处理失败直接 `os.Exit(1)`（构造期错误不返回）。

### 3.3 函数：NewCmdRun
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go`
**所属：** 包 `run`
**职责：** 构造 `run` 子命令，注册 flags 与 Run 回调。
**参数：** `f cmdutil.Factory`（集群工厂）、`streams genericiooptions.IOStreams`。
**返回值：** `*cobra.Command`。
**执行流程：**
1. `NewRunOptions(streams)` 建默认选项（`NewCmdRun+1`）。
2. 定义 cobra.Command，Run 回调依次执行 `Complete` → `Validate` → `Run`（`+9`，体 `+10~+12`），每步用 `cmdutil.CheckErr` 包装。
3. 添加通用 flags（Delete/Print/Record，`+16~+18`）。
4. `addRunFlags` 注册业务 flags（`+23`）：
   - `--image`：`StringVar` `+33`、`MarkFlagRequired` `+34`（旧 `+30`，已漂移 `+3~+4`）。
   - `--restart`：默认 `Always`，`+44`。
   - `--field-manager`：默认 `kubectl-run`，`AddFieldManagerFlagVar` `+50`（旧 `+52`，已漂移 `-2`）。
   - `--attach`/`-i`/`-t`/`--expose`/`--port`/`--rm`/`--overrides` 等。
**调用方：** 命令树装配逻辑（`NewKubectlCommand` 注册子命令处）。

### 3.4 函数：(*RunOptions).Complete
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go`（定义行 `Complete+0`）
**所属：** `RunOptions`
**职责：** flags 解析后的补全：recorder、namespace、dry-run 策略、restart 策略转译。
**参数：** `f cmdutil.Factory`、`cmd *cobra.Command`。**返回值：** `error`。
**关键逻辑：** `--restart` 经 `getRestartPolicy(cmdutil.GetFlagString(cmd,"restart"), o.Interactive)`（`Complete+21`）映射为 Pod 的 `restartPolicy`：`Always`/`OnFailure`/`Never` 直映；interactive 且空串 → `OnFailure`，否则空串 → `Always`。`--restart=Always`（默认）映射为生成 Deployment，`--restart=Never` → 裸 Pod（本场景）。其余：`RecordFlags` 补全出 recorder、解析 annotations/labels/env、`GetDryRunStrategy`、`GetPodRunningTimeoutFlag`、attach 默认（`-i` 未显式置 false 时 attach=true）。
**后置条件：** `o.DryRunStrategy`、namespace、打印器均就绪。

### 3.5 函数：(*RunOptions).Run
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go`（定义行 `Run+0`）
**所属：** `RunOptions`
**职责：** 生成目标对象并创建；可选 expose/attach。
**参数：** `f cmdutil.Factory`、`cmd *cobra.Command`、`args []string`（`args[0]` 为名称）。
**返回值：** `error`。
**执行流程：**
```text
Run (+0)
├── 1. 取 name=args[0] (+1)，arguments=args[1:] (+2~+4)
├── 2. 默认 label run=name (+6~+11)
├── 3. createPod 构造 Pod (+14)
├── 4. createGeneratedObject 创建 (+15)，err 则返回 (+16~+18)
├── 5.（可选 Expose）createService (+23) → createGeneratedObject (+24)，错误进 allErrs (+25~+30)
└── 6.（可选 Attach）组装 AttachOptions (+32~)，等 Pod 运行并 attach
```
**被调用：** `NewCmdRun` 的 Run 回调。内部调用：`createPod`、`createGeneratedObject`、条件性的 `createService`。
**主路径状态变化：** 无；HTTP 成功后终端输出由打印器处理（默认打印 `pod/nginx created`）。
**错误处理：** 创建失败立即返回；expose 的错误收集进 `allErrs`（attach 场景特殊处理）。

### 3.6 函数：(*RunOptions).createPod
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go`（定义行 `createPod+0`）
**职责：** 按 flags 构造 `*corev1.Pod`：单容器（name 默认取镜像名）、image、env、port、命令/参数、拉取策略、restartPolicy。
**参数：** `name string`、`labels map[string]string`、`args []string`（容器额外命令）。
**返回值：** `*corev1.Pod`。
**关键数据结构：** `corev1.Container`、`corev1.PodSpec`。该函数是纯构造，无 IO、无错误返回。容器字段：`Name`、`Image`、`Env`、`Stdin`/`StdinOnce`（interactive/leave-stdin-open）、`TTY`、`ImagePullPolicy`、`SecurityContext`（privileged）；PodSpec：`DNSPolicy: ClusterFirst`、`RestartPolicy`。`--command` 时写 `Command` 字段，否则写 `Args` 字段；`--port` 解析为 `ContainerPort`。

### 3.7 函数：(*RunOptions).createGeneratedObject
**源码：** `staging/src/k8s.io/kubectl/pkg/cmd/run/run.go`（定义行 `createGeneratedObject+0`）
**所属：** `RunOptions`
**职责：** 解析映射、应用 overrides、实际发起创建。
**参数：** `f cmdutil.Factory`、`cmd`、`obj runtime.Object`（待创建对象）、`overrider *cmdutil.Overrider`。
**返回值：** `*RunObject, error`。
**前置条件：** Complete 已完成，namespace 已知。
**执行流程：**
1. `f.ToRESTMapper()`（`+1`）。
2. `scheme.Scheme.ObjectKinds(obj)` 得 GVK（`+6`），`mapper.RESTMapping(...)` 得资源映射（`+10`）。
3. overrider 非空则 `overrider.Apply(obj)` 合并 `--overrides` JSON（`+16`）。
4. recorder 记录命令 `o.Recorder.Record(obj)`（`+22`，失败仅降级日志）。
5. 非 client-dry-run：写注解 `util.CreateOrUpdateAnnotation(...)`（`+28`）→ `f.ClientForMapping(mapping)`（`+31`）→ `resource.NewHelper(client, mapping)`（`+36`）`.DryRun(...)`（`+37`）`.WithFieldManager(o.fieldManager)`（`+38`）`.Create(o.Namespace, false, obj)`（`+39`）。
6. client-dry-run：仅设 namespace 不发请求（`meta.Accessor` `+44`、`SetNamespace` `+45`）。
**被调用函数：** RESTMapper、ObjectKinds、ClientForMapping、`Helper.Create`。
**数据流：** Pod →（按 mapping 版本序列化）→ HTTP body → 响应对象。
**错误处理：** 每步错误原样返回；HTTP 层错误（apiserver 返回的状态错误）在此首次具体化为 `*apierrors.StatusError`。

### 3.8 函数：(*Helper).Create / CreateWithOptions
**源码：** `staging/src/k8s.io/cli-runtime/pkg/resource/helper.go`（定义行 `Create+0` / `CreateWithOptions+0`）
**所属：** `Helper`
**职责：** 补全 CreateOptions（dryRun/fieldManager/fieldValidation），按需清空 ResourceVersion，转入实际请求。
**参数：** `namespace string`、`modify bool`（kubectl run 传 false）、`obj`、`options`。
**返回值：** `runtime.Object, error`。
**执行流程：**
1. `Create`（`+0`）直接转调 `CreateWithOptions(namespace, modify, obj, nil)`（`+1`）。
2. `CreateWithOptions`（`+0`）：`options==nil` 时初始化为 `&metav1.CreateOptions{}`（`+1~+3`）；`m.ServerDryRun` 设 `DryRun=[]string{DryRunAll}`（`+4~+6`）；`m.FieldManager`、`m.FieldValidation` 非空时写入 options（`+7~+12`）。
3. **关键分支（关键修正 2）：** `if modify {` 为 `CreateWithOptions+13`。为真时：`metadataAccessor.ResourceVersion(obj)` 取版本（`+14~+15`），取不到则原样转发（`+16~+18`）；版本非空则 `SetResourceVersion(obj,"")` 清空（`+19~+23`）。**kubectl run 传 `modify=false`，本分支不执行，直接创建。**
4. 末尾 `return m.createResource(...)`（`+27`）。
**关键分支（modify==true 的版本清空）：** 旧文「`+22~+33`」超出本函数总长（27 行），旧锚点不可复现，已弃用；现记 `+14~+22`（含取版本与清空逻辑）。本场景 `modify=false` 直接创建。

### 3.9 函数：(*Helper).createResource
**源码：** `staging/src/k8s.io/cli-runtime/pkg/resource/helper.go`（定义行 `createResource+0`）
**职责：** 组装并执行 POST 请求——客户端链路的最末端。
**执行流程（链式 builder，各步为请求设置字段）：**
1. `c.Post()`（`+1`）：取 RESTClient 的 POST verb builder。
2. `NamespaceIfScoped(namespace, m.NamespaceScoped)`（`+2`）：拼 `/namespaces/default`。
3. `Resource(resource)`（`+3`）：拼 `/pods`。
4. `VersionedParams(options, metav1.ParameterCodec)`（`+4`）：query 参数。
5. `Body(obj)`（`+5`）：按 mapping 的组版本序列化对象为 body。
6. `Do(context.TODO())`（`+6`）：实际发起 HTTP，处理状态码与重试。
7. `Get()`（`+7`）：把响应 decode 为 runtime.Object。
**返回值：** 存回的 Pod（含服务端分配的 UID、ResourceVersion）。
**错误处理：** `Do` 把非 2xx 响应转为 StatusError；网络错误直接返回。
**并发：** 同步阻塞；context 为 `context.TODO()`，超时由 http client 与上层控制。

#### Factory → RESTClient 链深度拆解

`createGeneratedObject` 从 `f.ToRESTMapper()` 到 `Helper.Create` 之间，靠 Factory 把「一个 GVK/资源映射」翻译成「可发 HTTP 的 RESTClient」。这一步是 kubectl 客户端链路最关键的适配层。

**Factory 的三个客户端入口（`kubectl/pkg/cmd/util/factory_client_access.go`）：**
| 方法 | 偏移 | 职责 |
|---|---|---|
| `ToRESTConfig` | `factoryImpl.ToRESTConfig+0`（:68） | 从 `clientGetter` 取 `restclient.Config`（集群地址、凭据、TLS） |
| `ToRESTMapper` | `factoryImpl.ToRESTMapper+0`（:72） | 取 GVK→resource 的 REST 映射器（同样委托 `clientGetter`） |
| `ClientForMapping` | `factoryImpl.ClientForMapping+0`（:114） | **按 mapping 的 GVK 决定 API 路径并建 RESTClient** |
| `UnstructuredClientForMapping` | `factoryImpl.UnstructuredClientForMapping+0`（:137） | 同上但走 unstructured（无 Go 类型）内容配置 |

**ClientForMapping 的关键分支（`ClientForMapping+0`，:114）：**
1. `clientGetter.ToRESTConfig()` 取基础配置（+2 ～ +4）。
2. `setKubernetesDefaults(cfg)` 补默认（+6 ～ +8）。
3. **核心 API 路径分流**（+10 ～ +16）：`mapping.GroupVersionKind.Group` 为 `corev1.GroupName`（即核心组 `""`）时 `cfg.APIPath = "/api"`；否则 `cfg.APIPath = "/apis"`。这就是 `kubectl run` 的 Pod（核心组）最终打到 `/api/v1/namespaces/default/pods` 而非 `/apis/...` 的根因。
4. `cfg.GroupVersion = &gv` 写入组版本（+18）→ `restclient.RESTClientFor(cfg)` 产出 RESTClient（+20）。

**UnstructuredClientForMapping 的差异（+137）：** 先 `restclient.SetKubernetesDefaults`（非 `setKubernetesDefaults`），且固定 `ContentConfig = resource.UnstructuredPlusDefaultContentConfig()`——用于没有 Go 类型定义的资源（如 CRD），其余 API 路径分流逻辑相同（核心组 `/api`、其余 `/apis`）。

**Helper 的装配链（createGeneratedObject 内，偏移见 §3.7）：**
```text
f.ClientForMapping(mapping)  → RESTClient（已定 API 路径 + 组版本）
  └─ resource.NewHelper(client, mapping)  → Helper（绑定 RESTClient + resource + NamespaceScoped）
       ├─ .DryRun(...)                    → 设 ServerDryRun
       ├─ .WithFieldManager(...)          → 设 FieldManager
       └─ .Create(namespace, false, obj)  → CreateWithOptions → createResource
            └─ RESTClient.Post()...Do().Get()  → HTTP → apiserver
```

**设计要点（源码可证）：**
1. **核心组 vs 扩展组的路径分流在客户端完成**：`ClientForMapping` 按 GVK.Group 切 `/api` 与 `/apis`，与 apiserver 侧 director/路由的分组完全对应（见篇 05 路由生成）。
2. **Factory 是「配置→客户端」的唯一适配点**：命令本身不拼 URL、不碰 restclient.Config，全部经 Factory 的 `ToRESTConfig/ToRESTMapper/ClientForMapping` 三步取得就绪的客户端。
3. **Helper 是 RESTClient 的薄封装**：`NewHelper(client, mapping)` 绑定 client/resource/namespaceScope，`Create/Get/Patch` 等动词都只是「设 options → 拼 builder → Do().Get()」，无业务逻辑。
4. **modify 语义**：`Helper.CreateWithOptions` 的 `modify=true` 才清空 ResourceVersion（供 apply/update 复用对象），`kubectl run` 传 `modify=false`，直接创建。

### 3.10 kubeconfig 加载与 RESTConfig 构造（第二层补充视角）

kubectl 发请求前必须先知道「连哪个集群、用什么凭据」。这来自 kubeconfig 文件（默认 `~/.kube/config`）。本节走一遍 kubeconfig 的加载与 RESTConfig 构造——这是 client-go 客户端链路的起点（先于篇 04 的 Factory→RESTClient 链）。

**kubeconfig 加载（`ClientConfigLoadingRules.Load`，loader.go:197）：**
```text
Load()
├── 1. Migrate()（迁移旧格式）
├── 2. 确定 kubeConfigFiles：
│      ExplicitPath 非空 → 只用该文件（不存在即报错）
│      否则 → Precedence 列表（默认 ~/.kube/config）
├── 3. 逐文件 LoadFromFile，跳过不存在的（记 missingList 产 warning）
└── 4. 合并所有 kubeconfigs（按 Precedence 顺序，后者覆盖前者）
```

**RESTConfig 构造（`DirectClientConfig.ClientConfig`，client_config.go:172）：**
```text
ClientConfig()
├── 1. getAuthInfo → AuthInfo（用户凭据：token/cert/client-key/exec/oidc）
├── 2. getContext → Context（当前上下文：cluster + user + namespace）
├── 3. getCluster → Cluster（集群：server、CA、insecure）
├── 4. ConfirmUsable（:432）校验三者齐备
└── 5. 构造 restclient.Config{Host, TLS, BearerToken, ...}
```

**kubeconfig 的三层结构（`clientcmdapi.Config`）：**
| 层 | 字段 | 作用 |
|---|---|---|
| Clusters | server / certificate-authority / insecure-skip-tls-verify | 集群地址与 CA |
| AuthInfos (users) | token / client-certificate / client-key / username-password / exec | 用户凭据 |
| Contexts | cluster + user + namespace | 把集群与用户绑成当前上下文 |
| current-context | 上下文名 | 默认使用哪个上下文 |

**上下文切换（`kubectl config use-context`）：** 改 `current-context`，使后续请求用不同的 cluster/user。

**认证插件的注册（篇 04 §3.1 提到的空导入）：** `cmd/kubectl/kubectl.go:28` 空导入 `k8s.io/client-go/plugin/pkg/client/auth`，注册所有认证插件（gcp/oidc/azure 等），使 kubeconfig 的 `exec`/`auth-provider` 凭据能被解析。

**RESTConfig 的关键字段（`DirectClientConfig.ClientConfig`）：**
| 字段 | 来源 | 作用 |
|---|---|---|
| `Host` | Cluster.Server | apiserver 地址 |
| `TLSClientConfig` | Cluster.CA + AuthInfo.cert/key | TLS 凭据 |
| `BearerToken` | AuthInfo.Token | Bearer token（写入 Authorization 头） |
| `ExecProvider` | AuthInfo.Exec | exec 凭据（动态获取 token） |
| `ProxyURL` | Cluster.ProxyURL | 代理地址 |

**设计要点（源码可证）：**
1. **kubeconfig 是分层的**：Clusters/AuthInfos/Contexts 三层解耦，一个 cluster 可配多个 user，一个 user 可访问多个 cluster——通过 context 切换。
2. **凭据多样**：token/cert/exec/oidc 等多种认证方式，由认证插件（篇 04 §3.1 提到的空导入）解析。
3. **Precedence 顺序**：多 kubeconfig 文件按 Precedence 顺序合并，后者覆盖前者——允许用户有默认配置 + 特定覆盖。
4. **ConfirmUsable 校验**：构造 RESTConfig 前先校验三者齐备，避免运行时才发现配置不全。

## 4. 调用关系总览
```text
main
 ↓
NewDefaultKubectlCommand → NewDefaultKubectlCommandWithArgs → NewKubectlCommand
    （cmdutil.NewFactory 构造工厂；注册 run 等全部子命令）
 ↓ cobra 调度
NewCmdRun 的 Run 回调
 ├── Complete
 ├── Validate
 └── RunOptions.Run
     ├── createPod
     └── createGeneratedObject
         ├── ToRESTMapper / RESTMapping
         ├── ClientForMapping
         └── Helper.Create → CreateWithOptions → createResource
             └── RESTClient.Post()...Do().Get()  → HTTP → apiserver
```

## 5. 设计意图（源码可证部分）
- **Options + Complete/Validate/Run 三段式**把 flag 解析、补全、执行分离，全部子命令共享同一模式，便于复用与测试。
- **Factory 接口**隔离命令与具体 client 构造，便于 fake 与测试。
- **`Helper.createResource` 用链式 builder 拼装请求**，资源映射（GVK→resource/version）集中在 `createGeneratedObject`，命令本身不拼 URL，REST 细节封装在 cli-runtime。
- **`modify` 标志**区分「创建全新对象」与「基于现有对象改写再创建」两种语义：新建对象无 ResourceVersion，`modify=false` 直接 POST；改写对象需先清空 ResourceVersion 避免 server 版本冲突。

## 6. 验证
- 锚点全部以 `grep -n` 绝对行号复核（HEAD `8893923`，工作区 `cmd/`、`staging/`、`pkg/` 干净），偏移 = 绝对行号 − 函数定义行。
- **与证据/旧版的差异**（源码为准）：
  - 认证插件导入改为绝对锚点 `cmd/kubectl/kubectl.go:28`（旧 `+25~+26` 不可复现，弃用）。
  - `Helper.CreateWithOptions` 的 `if modify {` 为 `+13`，版本清空 `+14~+22`（旧 `+22~+33` 超出函数长度，弃用）。
  - `kubectl.go` 漂移：`NewDefaultKubectlCommand +8`（旧 `+9`）、`RunNoErrOutput +9`（旧 `+10`）、`CheckErr +11`（旧 `+10~+12` 范围内）；`cmd.go` `NewFactory` 绝对行 `238`（旧 `240`，漂移 `-2`）。
  - `NewCmdRun`：`--image +33/+34`（旧 `+30`）、`--field-manager +50`（旧 `+52`）；`Complete` 定义 `+0`（绝对 `215`，旧 `214`，漂移 `+1`）。
  - `Run` 内部：`createPod` 调用 `+14`、`createGeneratedObject` 调用 `+15`（证据记 `+12/+13`，经源码复核为 `+14/+15`，已按源码修正；证据把空行/中间行误当调用行）。
  - `createGeneratedObject` 内部以源码为准：`ObjectKinds +6`、`RESTMapping +10`、`CreateOrUpdateAnnotation +28`、`ClientForMapping +31`、`NewHelper +36`、`Create +39`（证据分别记 `+5/+9/+26/+29/+31/+35`，偏小 1~4 行，已按源码修正）。
- **未确认项：** `[源码未确认]`——Go 基准版本（声明 `1.27.4` vs 本机实测 `go1.27.1`）待主代理确认；其余锚点均已源码确认，无未确认项。

[上一篇](03-主链路逐步拆解.md) · [总目录](README.md) · [下一篇](05-apiserver启动与路由.md)
