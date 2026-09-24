[上一篇](03-主链路逐步拆解.md) · [总目录](README.md) · [下一篇](05-apiserver启动与路由.md)

# kubectl 客户端
> **层级**：第四层——函数级源码解析。
> **范围**：进程启动、命令树装配、run 命令执行、REST 请求的最终发出。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 入口模块 | `cmd/kubectl`（包 main），仅做启动引导 |
| 命令实现 | `[staging] k8s.io/kubectl/pkg/cmd`（命令树）、`pkg/cmd/run`（run 命令）、`pkg/cmd/util`（Factory 等） |
| 通用运行时 | `[staging] k8s.io/cli-runtime/pkg/genericclioptions`（ConfigFlags）、`.../resource`（Helper、builder） |
| 依赖 | `[staging] k8s.io/client-go`（RESTClient）、`k8s.io/apimachinery`（scheme、序列化） |
| 对外接口 | 二进制 CLI；进程内通过 `cmdutil.Factory` 向各命令暴露 client/mapper/config |
| 数据流 | argv → flags/Options → 资源对象 → RESTClient → HTTP |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `RunOptions` | `kubectl/pkg/cmd/run/run.go` | run 命令全部 flags 与运行参数的载体（Image、Restart 策略、DryRun、PrintFlags 等） |
| `Factory`（interface） | `kubectl/pkg/cmd/util/factory.go` | 命令与集群之间的工厂：`ToRESTConfig`、`ToRESTMapper`、`ClientForMapping` |
| `ConfigFlags` | `cli-runtime/pkg/genericclioptions/config_flags.go` | 持有 kubeconfig 上下文、namespace 等用户配置，能产出 RESTConfig 与各类 client |
| `Helper` | `cli-runtime/pkg/resource/helper.go` | 对 RESTClient 的薄封装，提供 Create/Patch/Get/Replace 等动词 |
| `RunObject` | `kubectl/pkg/cmd/run/run.go` | 包装已创建对象及其 RESTMapping |

## 3. 函数级

### 函数：main
**源码：** `cmd/kubectl/kubectl.go`
**所属：** `package main`
**职责：** 设置日志级别、构造根命令、执行并处理错误。
**参数：** 无（读 `os.Args`）。
**返回值：** 无；错误经 `util.CheckErr` 漂亮打印并以非零码退出。
**执行流程：**
1. 从 argv 预解析 `-v` 设置 klog 级别（+7 ～ +8）
2. `cmd.NewDefaultKubectlCommand()` 构造完整命令树（+9）
3. `cli.RunNoErrOutput(command)` 执行（+10，内部完成 flag 解析与 RunE 调度）
4. 有错走 `util.CheckErr`（+10 ～ +12）
**副作用：** 进程退出码；导入 `client-go/plugin/pkg/client/auth` 注册所有认证插件（+25 ～ +26）。

### 函数：NewDefaultKubectlCommand / NewKubectlCommand
**源码：** `[staging] kubectl/pkg/cmd/cmd.go`
**职责：** 装配 cobra 根命令：构造 `Factory`、注册全部子命令（含 run）、处理插件与 kuberc。
**调用关系：** `NewDefaultKubectlCommand`（cmd.go:96）→ `NewDefaultKubectlCommandWithArgs`（:107）→ `NewKubectlCommand(o KubectlOptions)`（:165）；后者在 :240 调 `cmdutil.NewFactory(...)`。
**被调用：** `main`。

### 函数：NewCmdRun
**源码：** `[staging] kubectl/pkg/cmd/run/run.go`
**所属：** 包 run
**职责：** 构造 `run` 子命令，注册 flags 与 Run 回调。
**参数：** `f cmdutil.Factory`（集群工厂）、`streams genericiooptions.IOStreams`。
**返回值：** `*cobra.Command`。
**执行流程：**
1. `NewRunOptions(streams)` 建默认选项（+1）
2. 定义 cobra.Command，Run 回调依次执行 `Complete` → `Validate` → `Run`（+8 ～ +14）
3. 添加通用 flags（Delete/Print/Record，+16 ～ +18）
4. `addRunFlags` 注册业务 flags（+23）：`--image`（+30，标记必填）、`--restart`（默认 `Always`，+44）、`--attach/-i/-t` 等；`--field-manager` 默认 `kubectl-run`（+52）
**调用方：** 命令树装配逻辑。

### 函数：(*RunOptions).Complete
**源码：** `[staging] kubectl/pkg/cmd/run/run.go`（:214）
**职责：** flags 解析后的补全：recorder、namespace、dry-run 策略、restart 策略转译。
**关键逻辑：** `--restart=Never/OnFailure` 映射为 Pod 的 `restartPolicy` 并决定生成裸 Pod；`--restart=Always`（默认）在新版本中映射为生成 Deployment（本场景为 Never → 裸 Pod）。
**后置条件：** `o.DryRunStrategy`、namespace、打印器均就绪。

### 函数：(*RunOptions).Run
**源码：** `[staging] kubectl/pkg/cmd/run/run.go:326`
**所属：** `RunOptions`
**职责：** 生成目标对象并创建；可选 expose/attach。
**参数：** `f cmdutil.Factory`、`cmd *cobra.Command`、`args []string`（`args[0]` 为名称）。
**返回值：** `error`。
**执行流程：**
```text
Run (+0)
├── 1. 取 name=nginx，arguments (+1 ～ +4)
├── 2. 默认 label run=nginx (+5 ～ +9)
├── 3. createPod 构造 Pod (+14)
├── 4. createGeneratedObject 创建 (+15 ～ +17)
├── 5.（可选 Expose）创建 Service (+23 ～ +30)
└── 6.（可选 Attach）等待 Pod 运行并 attach (+32 ～ +end)
```
**被调用：** `validate_user` 等不存在；实际被调：`createPod`、`createGeneratedObject`、条件性的 `createService`。
**主路径状态变化：** 无；HTTP 成功后终端输出由打印器处理（默认打印 `pod/nginx created`）。
**错误处理：** 创建失败立即返回；expose 的错误收集进 `allErrs`（attach 场景特殊处理）。

### 函数：(*RunOptions).createPod
**源码：** `[staging] kubectl/pkg/cmd/run/run.go:605`
**职责：** 按 flags 构造 `*corev1.Pod`：单容器（name 默认取镜像名）、image、env、port、命令/参数、拉取策略、restartPolicy。
**参数：** `name string`、`labels map[string]string`、`args []string`（容器额外命令）。
**返回值：** `*corev1.Pod`。
**关键数据结构：** `corev1.Container`、`corev1.PodSpec`。该函数是纯构造，无 IO、无错误返回。

### 函数：(*RunOptions).createGeneratedObject
**源码：** `[staging] kubectl/pkg/cmd/run/run.go:679`
**所属：** `RunOptions`
**职责：** 解析映射、应用 overrides、实际发起创建。
**参数：** `f cmdutil.Factory`、`cmd`、`obj runtime.Object`（待创建对象）、`overrider *cmdutil.Overrider`。
**返回值：** `*RunObject, error`。
**前置条件：** Complete 已完成，namespace 已知。
**执行流程：**
1. `f.ToRESTMapper()`（+1）
2. `scheme.Scheme.ObjectKinds(obj)` 得 GVK（+6），`mapper.RESTMapping` 得资源映射（+10 ～ +11）
3. overrider 非空则 `Apply` 合并 `--overrides` JSON（+14 ～ +18）
4. recorder 记录命令（+21 ～ +23，失败仅降级日志）
5. 非 client-dry-run：写注解（+27 ～ +29）→ `f.ClientForMapping(mapping)`（+31）→ `resource.NewHelper(...).DryRun(...).WithFieldManager(...).Create(...)`（+34 ～ +38）
6. client-dry-run：仅设置 namespace 不发请求（+41 ～ +44）
**被调用函数：** RESTMapper、ObjectKinds、ClientForMapping、`Helper.Create`。
**数据流：** Pod →（按 mapping 版本序列化）→ HTTP body → 响应对象。
**错误处理：** 每步错误原样返回；HTTP 层错误（apiserver 返回的状态错误）在此首次具体化为 `*apierrors.StatusError`。

### 函数：(*Helper).Create / CreateWithOptions
**源码：** `[staging] cli-runtime/pkg/resource/helper.go:206 / :210`
**所属：** `Helper`
**职责：** 补全 CreateOptions（dryRun/fieldManager/fieldValidation），按需清空 ResourceVersion，转入实际请求。
**参数：** `namespace string`、`modify bool`（kubectl run 传 false）、`obj`、`options`。
**返回值：** `runtime.Object, error`。
**关键分支：** `modify==true` 且对象带 ResourceVersion 时清空（+22 ～ +33）；本场景 `modify=false` 直接创建。

### 函数：(*Helper).createResource
**源码：** `[staging] cli-runtime/pkg/resource/helper.go:240`
**职责：** 组装并执行 POST 请求——客户端链路的最末端。
**执行流程（链式 builder，各步为请求设置字段）：**
1. `c.Post()`：取 RESTClient 的 POST verb builder
2. `NamespaceIfScoped(namespace, m.NamespaceScoped)`：拼 `/namespaces/default`
3. `Resource(resource)`：拼 `/pods`
4. `VersionedParams(options, metav1.ParameterCodec)`：query 参数
5. `Body(obj)`：按 mapping 的组版本序列化对象为 body
6. `Do(context.TODO())`：实际发起 HTTP，处理状态码与重试
7. `Get()`：把响应 decode 为 runtime.Object
**返回值：** 存回的 Pod（含服务端分配的 UID、ResourceVersion）。
**错误处理：** `Do` 把非 2xx 响应转为 StatusError；网络错误直接返回。
**并发：** 同步阻塞；context 为 `context.TODO()`，超时由 http client 与上层控制。

## 4. 调用关系总览
```text
main
 ↓
NewDefaultKubectlCommand → NewKubectlCommand（Factory + 子命令注册）
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
- Options + Complete/Validate/Run 三段式把 flag 解析、补全、执行分离，全部子命令共享同一模式。
- Factory 接口隔离命令与具体 client 构造，便于 fake 与测试。
- `Helper.createResource` 用链式 builder 拼装请求，资源映射（GVK→resource/version）集中在 createGeneratedObject，命令本身不拼 URL。

[上一篇](03-主链路逐步拆解.md) · [总目录](README.md) · [下一篇](05-apiserver启动与路由.md)
