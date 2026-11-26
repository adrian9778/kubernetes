[上一篇](05-apiserver启动与路由.md) · [总目录](README.md) · [下一篇](07-scheduler调度与绑定.md)

# apiserver 请求处理与 etcd
> **层级**：第四层——函数级源码解析。
> **范围**：POST 创建 Pod 的 handler 内部全流程、Pod strategy 默认化与校验、泛型 Store.create、etcd3 写入与冲突处理。
> **代码基准**：adrian@`6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26），Go 1.27.4。

## 0. 生产视角：一次 POST 创建 Pod 的请求怎么进来、怎么结束

一个 `kubectl apply -f pod.yaml` 或 `kubectl run nginx --image=nginx`，最终都收敛为对 apiserver 的一条 HTTP POST：

```text
POST /api/v1/namespaces/default/pods        # 集合创建（name 来自对象体）
POST /api/v1/namespaces/default/pods/nginx  # 命名创建（name 来自 URL 路径）
```

请求进入后依次经历：**认证/鉴权 filter**（见第 05 篇）→ **WithTimeout filter**（把处理放入 goroutine 并给一个 34s 上限 context）→ 分发到本资源 REST handler（`CreateResource` → `createHandler` 闭包）。

**成功表现**：对象被写入 etcd（key `/registry/pods/default/nginx`），响应 `201 Created`，body 是**解码回 out 的对象**（含服务端填充的 `resourceVersion`、`uid`、`creationTimestamp` 与默认化的 `status.phase=Pending`、`spec` 默认字段、`metadata.managedFields`）。watch 广播在存储层成功后被触发，后续监听者收到 ADDED 事件。

**失败表现**（按阶段，全部经 `scope.err` 转为问题细节响应，不 panic）：

| 阶段 | 状态码 | 形态 |
|---|---|---|
| body 读取/限量 | 400 | `BadRequest` |
| body decode 失败 / GV 不匹配 | 400 | `BadRequest` |
| 对象 namespace 与请求不一致 | 400 | `BadRequest` |
| strategy 声明式校验失败 | 422 | `Invalid`（field.ErrorList 聚合） |
| mutating/validating admission 拒绝 | 403 或 admission 自带状态 | 上抛 |
| etcd key 已存在 | 409 | `AlreadyExists` |
| 对象过大（序列化超限） | 413 | 清 managedFields 重试一次；仍大则上抛 |
| 编码/加密/网络/etcd 故障 | 500 | `InternalError` 包装 |

## 1. 模块级
| 项 | 内容 |
|---|---|
| 协议层 | `[staging] apiserver/pkg/endpoints/handlers/create.go`：decode、admission、调存储、写响应 |
| 资源策略 | `pkg/registry/core/pod/strategy.go`：podStrategy（默认化、声明式校验） |
| 泛型存储 | `[staging] apiserver/pkg/registry/generic/registry/store.go`：Store.Create/create |
| 底层存储 | `[staging] apiserver/pkg/storage/etcd3/store.go`：编码、加密变换、lease、OptimisticPut |
| 数据流 | JSON body → Pod(internal) → strategy 默认化/校验 → 编码/加密 → etcd KV → decode 回 out → JSON 响应 |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `podStrategy` | `pkg/registry/core/pod/strategy.go` | Pod 的 RESTCreateStrategy（`podStrategy+0`，:61）；单例 `Strategy+0`（:68），组合 `rest.DeclarativeValidation` 与 SimpleNameGenerator |
| `Store` | `[staging] generic/registry/store.go` | 泛型资源存储，持有 CreateStrategy、KeyFunc、Storage（storage.Interface） |
| `store`（etcd3） | `[staging] storage/etcd3/store.go` | storage.Interface 的 etcd 实现，持有 codec、versioner、transformer、leaseManager、client |
| `ValidateObjectFunc` | `[staging] registry/rest/types.go` | validating admission 回调的函数类型 |

## 3. 函数级

> 偏移记法：`文件:符号+偏移` 表示符号当前行号与内部语句的相对位置；`create.go` 的 createHandler 闭包基线 = 闭包开括号行 `createHandler+1`（:54，函数定义 `createHandler+0` :53）；其余函数基线 = 函数体首个语句行。本节所有行号均已按基准 HEAD 复核。

### 函数：CreateResource
**源码：** `[staging] endpoints/handlers/create.go CreateResource+0`（:245）
**职责：** 入口适配，直接返回 `createHandler(r, scope, admission, false)` 构造的 handler 闭包。
**参数：** `r rest.Creater`、`scope *RequestScope`、`admission admission.Interface`。
**调用方：** 资源 REST 安装（`InstallREST` 为每个 resource 注册该 HandlerFunc）。

### 函数：createHandler
**源码：** `[staging] endpoints/handlers/create.go createHandler+0`（:53）
**所属：** 包 handlers
**职责：** Pod 创建的完整请求处理。
**参数（外层）：** `r rest.NamedCreater`、`scope *RequestScope`、`admit admission.Interface`、`includeName bool`（集合创建为 false）。
**返回（闭包）：** 无返回值；经 `ResponseWriter` 输出。
**前置条件：** 请求已通过全部 filter（认证/鉴权等），context 中带 user 与 RequestInfo。
**后置条件：** 成功时对象已持久化，201 已写出。
**执行流程（偏移相对闭包开括号行 `createHandler+1`（:54）计 +0）：**
```text
闭包入口 (+0)
├── 1. tracing span "Create" (+1 ～ +5)
├── 2. Namer.Name 取 namespace/name (+7 ～ +22)
│      name 缺失时回退取 namespace（本场景 name=nginx 来自对象，+97 ～ +99）
├── 3. context 34s 超时 (+25 ～ +26)
├── 4. 协商输出媒体类型 (+27 ～ +33)
├── 5. 协商输入序列化器 (+34 ～ +39)
├── 6. limitedReadBody 限量读 body (+40 ～ +46)
├── 7. 解析 CreateOptions（query）并校验 (+48 ～ +60)
├── 8. decode body → Pod（strict 视 FieldValidation）(+73 ～ +87)
├── 9. 校验对象 GV 与 scope 匹配 (+90 ～ +93)
├── 10. name 为空则取对象 name (+98 ～ +99)
├── 11. 写 namespace 入 ctx (+101)
├── 12. admission.WithAudit 装饰 + 审计记录对象 (+106 ～ +107)
├── 13. 擦除 ObjectMeta 系统字段 (+116 ～ +118)
├── 14. 保证对象 namespace 与请求一致 (+121 ～ +124)
├── 15. 构造 admissionAttributes（Create）(+128)
├── 16. requestFunc 闭包 → r.Create (+129 ～ +137)
├── 17. FinishRequest：
│      ├─ FieldManager.UpdateNoErrors（managed fields）(+145)
│      ├─ mutatingAdmission.Admit（+149 ～ +151）
│      ├─ dedup owner refs（+154）
│      └─ requestFunc；过大则清 managedFields 重试一次（+158 ～ +163）
├── 18. 错误检查 (+166 ～ +170)
└── 19. transformResponseObject 写 201 (+181 ～ +219)
```
**调用方：** go-restful RouteFunction（经 restfulCreateResource）。
**被调用：** negotiation、decoder.Decode、`FieldManager.UpdateNoErrors`、`mutatingAdmission.Admit`、`r.Create`（Pod storage.REST）。
**错误处理：** 前半段错误经 `scope.err` 做问题细节化输出（400/404/409 等）；admission 错误原样上抛；TooLarge 触发唯一一次重试。
**并发：** 单请求内同步；WithTimeout filter 已将处理放入 goroutine，闭包内 cancel 仅控制 context。
**副作用：** 审计日志、span、watch 广播（存储成功后由 storage 层触发）。

### 函数：AdmissionToValidateObjectFunc
**源码：** `[staging] registry/rest/create.go AdmissionToValidateObjectFunc+0`（:218）
**职责：** 把 admission 的 validating 阶段（固定 Attributes）包成 Store 需要的 `ValidateObjectFunc`。
**关键逻辑：** 返回的函数在被调用时执行 `admit.Validate(ctx, finalAttributes, o)`（finalAttributes 重建，把 staticAttributes 的 name 换为对象实际 name 以覆盖 generateName）；deep copy 语义由调用侧保证（Store.create 传入 deep copy）。

### 2.3b 准入控制（Admission）链深度拆解

准入是「默认化之后、持久化之前」的横切校验/改写层。它分两阶段：**mutating（Admit，可改写对象）** 在 handler 内同步执行；**validating（Validate，只读校验）** 包成 `ValidateObjectFunc` 延迟到 `Store.create` 内执行。两阶段共享同一份 `admissionAttributes`。

#### 链的载体：chainAdmissionHandler

**源码：** `[staging] apiserver/pkg/admission/chain.go`
- `Struct：chainAdmissionHandler 偏移：+23`（`[]Interface`）；`NewChainHandler`（+26）把多个插件串成链。
- `Admit`（+31）：遍历链，`handler.Handles(operation)` 不匹配则跳过；只对实现 `MutationInterface` 的插件调 `Admit`（+38 ～ +42），首个错误立即返回。
- `Validate`（+47）：同构，只对实现 `ValidationInterface` 的插件调 `Validate`（+54 ～ +58）。
- `Handles`（+63）：任一插件处理该 operation 即返回 true。

#### 两阶段接口

**源码：** `[staging] apiserver/pkg/admission/interfaces.go`
- `Struct：Interface 偏移：+124`（核心接口）：`Handles(operation)` +127、`Admit` +135（mutating，可实现为 `MutationInterface`）、`Validate` +144（validating，可实现为 `ValidationInterface`）。
- 一个插件可同时实现 Admit 与 Validate；链在两个阶段分别按能力挑插件执行。

#### mutating 阶段：webhook 与内置插件

**mutating webhook：** `[staging] admission/plugin/webhook/mutating/plugin.go`
- `Struct：Plugin 偏移：+49`；`NewMutatingWebhook`（+72）读 kubeconfig 建客户端。
- `Admit`（+99）：把请求转成 `admissionreview` 发给 webhook service，按 JSON Patch 回写对象。

**validating webhook：** `[staging] admission/plugin/webhook/validating/plugin.go`
- `Struct：Plugin 偏移：+51`；`NewValidatingAdmissionWebhook`（+74）；`Validate`（+93）同构，只判允许/拒绝不改对象。

#### handler 内的执行位置（createHandler 内，偏移以闭包入口 `createHandler+1`（:54）为基线）

1. `admissionAttributes := admission.NewAttributesRecord(...)`（`createHandler+129`）：构造本次请求的固定 Attributes（Create 操作、目标 kind/resource/subresource、userInfo、dryRun）。
2. `requestFunc` 闭包（`createHandler+135`）内把 `rest.AdmissionToValidateObjectFunc(admit, admissionAttributes, scope)` 作为 `ValidateObjectFunc` 传给 `r.Create`——**validating 阶段在此刻才被延迟绑定**，真正执行在 `Store.create` 内。
3. `admit = fieldmanager.NewManagedFieldsValidatingAdmissionController(admit)`（`createHandler+147`）：装饰一层 managed fields 校验。
4. `mutatingAdmission.Admit(ctx, admissionAttributes, scope)`（`createHandler+150~+152`）：**mutating 阶段同步执行**——仅当 `admit` 实现 `MutationInterface` 且 `Handles(Create)`。
5. `dedupOwnerReferencesAndAddWarning(..., true)`（`createHandler+155`）：mutating 改写后**再次**去重 ownerReferences（mutating 前已去过一次，见 `createHandler+125` 附近）。
6. `requestFunc()`（`createHandler+159`）→ `r.Create` → `Store.Create/create` → 内部 `createValidation` 执行 validating 阶段。
7. `isTooLargeError(err)`（`createHandler+162`）：对象序列化过大时清空 managedFields 重试一次。

#### 插件注册与执行顺序

**源码：** `pkg/kubeapiserver/options/plugins.go`
- `AllOrderedPlugins`（偏移：+73）：全部插件的**执行顺序**权威清单；注册顺序无关（注释 +127），执行按此表。
- `RegisterAllAdmissionPlugins`（偏移：+128）：把插件注册进 `admission.Plugins`。
- 默认开启集与全量集之差（偏移：+196，`sets.New(AllOrderedPlugins...).Difference(defaultOnPlugins)`）决定哪些需显式 `--enable-admission-plugins`。

**审计装饰：** `admission.WithAudit`（`[staging] apiserver/pkg/admission/audit.go WithAudit+0`，:39）在 handler 内包裹链，使每个插件决策都进审计日志。

#### 两阶段对比

| 维度 | mutating（Admit） | validating（Validate） |
|---|---|---|
| 能否改对象 | 能（webhook 返 JSON Patch / 内置插件就地改） | 不能（只读） |
| 执行位置 | handler 内同步（createHandler +149） | 延迟到 `Store.create` 内（`createValidation` +28 ～ +32） |
| Attributes | `admissionAttributes`（固定） | `AdmissionToValidateObjectFunc` 重建（覆盖 generateName 后的实际 name） |
| 接口 | `MutationInterface.Admit` | `ValidationInterface.Validate` |
| 失败语义 | 首个错误立即返回（403/插件自带状态） | 首个错误立即返回，经 Store.create 上抛 |

**设计要点（代码推断）：**
1. **mutating 早、validating 晚**：mutating 在 handler 内同步改写（含 generateName 之前），validating 延迟到 storage 层，确保校验看到的是最终 name（含 generateName 展开结果）。
2. **能力接口分流**：链不为每个插件强制实现 Admit/Validate，而是运行时类型断言 `MutationInterface`/`ValidationInterface`，插件按需实现。
3. **顺序即语义**：`AllOrderedPlugins` 固定执行顺序，内置插件间有依赖（如 NamespaceLifecycle 须早于依赖 namespace 的插件）；注册顺序无关，避免初始化竞态。

### 函数：(*Store).Create
**源码：** `[staging] generic/registry/store.go e.Create+0`（:564）
**职责：** 命名创建分流：对象带 `generateName` 而无 `name` 走 `createWithGenerateNameRetry`（最多 `maxNameGenerationCreateAttempts` 次冲突重试），否则直接委托 `e.create`。

### 函数：(*Store).create
**源码：** `[staging] generic/registry/store.go e.create+0`（:595）
**职责：** 通用创建主流程。
**参数：** `ctx`、`obj`（Pod）、`createValidation rest.ValidateObjectFunc`、`options *metav1.CreateOptions`。
**返回值：** `runtime.Object, error`。
**执行流程（以函数体首语句 `e.create+1`（:596）为基线 +0）：**
1. meta.Accessor（+3）→ `FillObjectMetaSystemFields`：填 UID/CreationTimestamp（+6）
2. GenerateName 场景调 `CreateStrategy.GenerateName`（+7 ～ +9；本场景 name 已指定，跳过）
3. `BeginCreate` 钩子（+12 ～ +21，Pod 未用，finishCreate=finishNothing）
4. `rest.BeforeCreate(e.CreateStrategy, ctx, obj)`（+23）——默认化+声明式校验，细节见下
5. `createValidation(ctx, obj.DeepCopyObject())`（+28 ～ +32）：validating admission（含 validating webhook）
6. `ObjectNameFunc` 取 name（+34）→ `KeyFunc` 生成 key（+38）：`/registry/pods/default/nginx`
7. `calculateTTL`（+43）；Pod 无 ttl，得 0
8. `e.NewFunc()` 建 out（+47）
9. `e.Storage.Create(ctx, key, obj, out, ttl, dryRun)`（+48）
10. 失败：`InterpretCreateError` → `CheckGeneratedNameError`（+49）；AlreadyExists 时 Get 现存对象并检查删除戳（+51 ～ +65）
11. 成功：finishCreate 提交（+69 ～ +71）→ `AfterCreate`（+73 ～ +75）→ 返回 out
**调用方：** createHandler 的 requestFunc。
**被调用：** BeforeCreate、createValidation、KeyFunc、etcd3 store.Create、AfterCreate。
**状态变化：** 对象系统字段填充；etcd 新增；本地无持久状态。
**关键数据结构：** storage key、QualifiedResource。
**后置加工说明：** create 成功后仅调用 `e.AfterCreate(out, options)`（`e.create+74~+76`，AfterCreate 字段定义 `Store+88`），**无对象后置 Decorator**——`Decorator` 词仅出现在存储配置装饰（`e.CompleteWithOptions+109` DecodeDecorator、`e.CompleteWithOptions+112`、`e.CompleteWithOptions+119` opts.Decorator，装饰 storage.Interface 后端），create 期无对象 Decorator。

### 函数：BeforeCreate
**源码：** `[staging] registry/rest/create.go BeforeCreate+0`（:103）
**职责：** 校验前置元数据 + 执行 strategy 的默认化与声明式校验。
**执行流程（以函数体首语句 `BeforeCreate+1`（:104）为基线 +0）：**
1. `objectMetaAndKind` 取 meta 与 kind（+0）
2. 确认系统字段已被填充（Store.create 第 1 步已做，+6 ～ +8）
3. 确认 GenerateName 已生成（+11 ～ +13）
4. 取 ctx 中 namespace 并核对对象 namespace（+16 ～ +22）
5. `strategy.PrepareForCreate`（+24）
6. `ValidateCreate`（声明式校验，+26 ～ +29）；有错聚合成 Invalid（422）
7. warnings（+31 ～ +33）；Canonicalize（+35）

### 函数：podStrategy.PrepareForCreate
**源码：** `pkg/registry/core/pod/strategy.go podStrategy.PrepareForCreate+0`（:88）
**职责：** Pod 创建默认化。
**内部步骤（以函数体首语句 `podStrategy.PrepareForCreate+1`（:89）为基线 +0）：**
1. Generation 置 1（+1）
2. Status 重置为 `{Phase: Pending}`（+3 ～ +5）
3. `DropDisabledPodFields`：按 feature gate 丢弃未启用字段（+7）
4. `applySchedulingGatedCondition`：有 scheduling gates 时补 Unschedulable 条件（+9）
5. `mutatePodAffinity`、`mutateTopologySpreadConstraints`：默认化亲和与拓扑分布（+10 ～ +11）
6. `applyAppArmorVersionSkew`（+12）、`DefaultPodLevelResources`（+13）
7. 计算 QOSClass（+15，`qos.GetPodQOS`：Guaranteed/Burstable/BestEffort）
**副作用：** 就地修改 Pod；无 IO。

### 函数：podStrategy.Validate
**源码：** `pkg/registry/core/pod/strategy.go podStrategy.Validate+0`（:128）
**职责：** 组装校验选项后调用 `corevalidation.ValidatePodCreate`（容器规范、资源、安全上下文、卷、亲和等的声明式校验全集）。
**返回值：** `field.ErrorList`；非空即导致 422。

### 函数：etcd3 store.Create
**源码：** `[staging] storage/etcd3/store.go s.Create+0`（:293）
**职责：** 对象落 etcd。
**参数：** `ctx`、`key`、`obj`、`out`、`ttl uint64`。
**返回值：** `error`。
**执行流程（以函数体首语句 `s.Create+1`（:294）为基线 +0）：**
```text
Create (+0)
├── 1. prepareKey 加命名空间前缀 (+0 ～ +3)
├── 2. tracing "Create etcd3" (+4 ～ +11)
├── 3. 拒绝创建时自带 ResourceVersion (+12 ～ +14)
├── 4. PrepareObjectForStorage（versioner 清 RV）(+15 ～ +17)
├── 5. runtime.Encode 序列化 (+19 ～ +23)
├── 6. ttl≠0 时 leaseManager.GetLease（Pod ttl=0，跳过）(+28 ～ +31)
├── 7. transformer.TransformToStorage 加密/变换 (+34 ～ +38)
├── 8. OptimisticPut(ctx, key, data, expectedRev=0, {LeaseID}) (+42)
│      expectedRev=0 ⇒ 仅当 key 不存在时事务成功
├── 9. 指标记录 (+43)
├── 10. !txnResp.Succeeded → KeyExistsError (+50 ～ +51)
└── 11. 用未加密 data decode 到 out，带返回 Revision (+55 ～ +62)
```
**外部依赖：** etcd KV（魔改 client `go.etcd.io/etcd/client/v3/kubernetes`）、KMS/encryption transformer、lease manager。
**错误处理：** 编码/变换错误包装返回；key 冲突返回 storage KeyExists（在 Store.create 被解释为 AlreadyExists）；decode 失败记录指标并返回。
**设计意图（源码可证）：** 先 TransformToStorage 后写入，落盘数据始终为密文；decode out 使用加密前明文 data，调用方不接触解密读路径。

## 4. 端到端调用关系
```text
restful POST handler
 ↓
CreateResource → createHandler 闭包
 ├── decoder.Decode
 ├── FieldManager.UpdateNoErrors
 ├── mutatingAdmission.Admit（webhook/插件）
 └── Store.Create → Store.create
      ├── FillObjectMetaSystemFields
      ├── BeforeCreate
      │    ├── podStrategy.PrepareForCreate
      │    └── ValidateCreate（corevalidation.ValidatePodCreate）
      ├── createValidation（validating admission）
      ├── KeyFunc
      └── etcd3 store.Create
           ├── Encode
           ├── TransformToStorage
           └── OptimisticPut(expectedRev=0)
```

## 5. 错误来源汇总
| 来源 | 形态 | 最终处理位置 |
|---|---|---|
| body 读取/ decode | 400 BadRequest | createHandler scope.err |
| strategy 声明式校验 | 422 Invalid | BeforeCreate → createHandler |
| mutating admission 拒绝 | 403/状态错误 | FinishRequest 上抛 |
| validating admission 拒绝 | 403/状态错误 | Store.create 上抛 |
| etcd key 已存在 | 409 AlreadyExists | Store.create 解释后经 scope.err |
| 加密/编码/网络 | 500 InternalError | 各层包装上抛 |

## 5b. 写入后的 watch 广播（第二层补充视角）

写入成功不只返回 201——apiserver 同时通过 **watch** 把这次变更推给所有订阅者（各控制器的 informer、scheduler、kubelet 配置源）。这是「声明式调谐」的起点：没有 watch，控制器就只能靠轮询。本节拆解 etcd3 层如何把一次 etcd 写变成一条 watch 事件并送达客户端。

**入口：** `[staging] storage/etcd3/store.go 函数：Watch 偏移：+0`（:1115）→ `w.createWatchChan+0`（:175）建立到 etcd 的 watch 通道。

#### 从 etcd 事件到客户端事件

| 环节 | 符号 | 做什么 |
|---|---|---|
| etcd watch 回调 | `wc.startWatching+0`（watcher.go :468） | 收 etcd `wres.Events`，逐个 `parseEvent`（调用点 `wc.startWatching+72`，:540） |
| 解析 | `parseEvent+0`（event.go :66） | etcd `Event` → 内部 `event{key, value, rev, isDeleted, isCreated, prevValue}`；`expectPrevKV` 且 `PrevKv==nil`（已被 compact）则报错 |
| 进度通知 | `progressNotifyEvent+0`（event.go :86） | etcd bookmark（仅 rev、无对象），维持客户端 revision 前进 |
| 排队 | `wc.queueEvent+0`（watcher.go :843） | 推入 `incomingEventChan`，**channel 满则阻塞上游**（不丢事件） |
| 并发转换 | `p.scheduleEventProcessing+0`（watcher.go :627）/ `p.collectEventProcessing+0`（:653） | 用 `processingQueue` 保序地把事件交给 worker 做 transform |
| 转换 | `wc.transform` | 内部 event → `watch.Event`（Added/Modified/Deleted，带对象） |
| 送达 | `wc.sendEvent+0`（watcher.go :829） | `resultChan <- *event`；客户端接收不够快则**阻塞**（同样不丢） |

**Create 对应的 watch 事件：** 一次 `OptimisticPut` 成功后，etcd 产生 `EventTypePut`；`parseEvent` 判 `isCreated = e.IsCreate()`（首次 Put）→ 上层转成 **ADDED** 事件，携带存回的 Pod 对象（`spec.nodeName` 仍空）。这正是 scheduler / ReplicaSet 控制器 informer 收到的第一个信号。

#### watch 的两个关键保证（源码可证）

1. **不丢事件（背压而非丢弃）**：`wc.sendEvent`/`wc.queueEvent` 都是阻塞 `select`（`wc.sendEvent+6~+11`、`wc.queueEvent+3~+6`）——客户端消费慢时反向阻塞 etcd 事件读取，而非像 PLEG 那样丢弃。这保证 informer 不会漏掉任何变更。
2. **保序**：`concurrentOrderedEventProcessing`（类型 :618，`processingQueue` 字段 :621，缓冲容量 `processEventConcurrency-1`，见 `wc.concurrentProcessEvents+3`）经 `p.scheduleEventProcessing+0~+25` / `p.collectEventProcessing+0~+26` 串行化转换结果，同一 watch 的事件按 etcd revision 顺序送达。

**与 PLEG 的对比（为何一个丢弃一个背压）：**
- PLEG 面向「最新状态对账」，丢一次事件下轮 relist 会重新发现 → 丢弃安全。
- watch 面向「变更流」，丢一次就永久错过（除非 relist 兜底）→ 必须背压阻塞。这也是 informer 还要周期 resync 兜底的原因。

#### 设计要点（源码可证）

1. **watch 从 etcd 直接起**：`s.Watch+0`（:1115）在 etcd3 层就建 watch 通道，apiserver 不轮询 etcd——一次 etcd watch 服务所有订阅该资源的客户端。
2. **prevKV 用于计算旧对象**：`parseEvent` 带 `prevValue`（`parseEvent+14~+16`），使 `OnUpdate` 能拿到 oldObj 算 diff（见篇 11 informer）。
3. **compact 防御**：`expectPrevKV && PrevKv==nil` 报错（`parseEvent+3~+6`），处理 etcd 历史已被 compact 的场景。
4. **progress notify 维持 revision**：bookmark 事件（`wc.startWatching+56~+59`）让客户端 revision 前进，避免长时间无事件后重连从旧 revision 开始。

## 6. 设计意图与验证
**设计意图（源码可证）：**
- **先解码后默认化再校验再持久化**：decode（`createHandler+74`）在 handler 层完成版本转换；`PrepareForCreate` 只做就地默认化（`podStrategy.PrepareForCreate+0`）；`ValidateCreate` 聚合并把错误转成 422 Invalid（`BeforeCreate+27~+29`）；持久化只在全部通过后进入 etcd3。职责分层清晰：handler 管协议/协商，strategy 管资源语义，generic store 管通用编排，etcd3 管持久化与冲突。
- **写路径始终密文**：etcd3 `TransformToStorage`（`s.Create+35`）在 `OptimisticPut`（`s.Create+43`）之前，落盘 data 恒为变换后密文；读回 out 用加密前明文 data（`s.Create+56`），调用方不接触解密读路径。
- **创建冲突用 expectedRev=0 表达**：`OptimisticPut` 以 expectedRev=0 构造「仅当 key 不存在才成功」的事务，失败返回 `KeyExists`（`s.Create+51`），由 Store.create 解释为 AlreadyExists（409）；这保证同一 key 不可能被两个并发创建同时写入。
- **幂等与重试**：对象过大时清 managedFields 重试一次（`createHandler+159~+164`）；generateName 冲突由 `createWithGenerateNameRetry` 重试。

**验证结论（基准 HEAD 6bf0bfd，2026-11-26）：**
- 本目录全部符号在位，签名与旧文档一致，无消失项。
- **本轮（8893923 → 6bf0bfd）核对**：请求处理与 etcd 写入主链路**无语义变化**。`createHandler`（def 53）、`CreateResource`（def 245）、`Store.Create`（def 564）、`Store.create`（def 595）、etcd3 `store.Create`（def 293）、`podStrategy` 全套（61/68/88/128）、`BeforeCreate`（103）、`AdmissionToValidateObjectFunc`（218）def 行均与上轮一致。
- **etcd3 Update/Delete 已改 TxnDelegator 代理模式**（`storeTxnDelegator` 实现 `storage.TxnDelegator` 的 `GetCurrentState`/`UpdateOnce`/`DeleteOnce`，cacher 侧 `txnDelegator` 代理 watch cache），但**仅作用于 Update/Delete 路径**；本篇讲的 Create 仍由 `store.Create` 直连 `OptimisticPut(expectedRev=0)`，不经代理。故本篇不受影响，Update 路径另见 etcd3写入路径篇。
- **本轮修正两处陈述性漂移**：`progressNotifyEvent` def 行 80→86；etcd3 Create 第 4 步 versioner 方法名 `PrepareObjectResourceVersion`→`PrepareObjectForStorage`（当前源码 `s.versioner.PrepareObjectForStorage`）。
- 关键差异确认：Store.create 后置加工仅 `AfterCreate`，**无对象后置 Decorator**；etcd3 包内无独立 `Write`，create 直接走 `OptimisticPut`。
- 未确认项：无；本目录所有锚点均经证据复核（codegraph_explore + sr_tools offset）。

[上一篇](05-apiserver启动与路由.md) · [总目录](README.md) · [下一篇](07-scheduler调度与绑定.md)
