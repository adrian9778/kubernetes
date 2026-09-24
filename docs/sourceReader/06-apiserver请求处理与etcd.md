[上一篇](05-apiserver启动与路由.md) · [总目录](README.md) · [下一篇](07-scheduler调度与绑定.md)

# apiserver 请求处理与 etcd
> **层级**：第四层——函数级源码解析。
> **范围**：POST 创建 Pod 的 handler 内部全流程、Pod strategy 默认化与校验、泛型 Store.create、etcd3 写入与冲突处理。

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
| `podStrategy` | `pkg/registry/core/pod/strategy.go:61` | Pod 的 RESTCreateStrategy；单例 `Strategy`（:68），组合 `rest.DeclarativeValidation` 与 SimpleNameGenerator |
| `Store` | `[staging] generic/registry/store.go` | 泛型资源存储，持有 CreateStrategy、KeyFunc、Storage（storage.Interface） |
| `store`（etcd3） | `[staging] storage/etcd3/store.go` | storage.Interface 的 etcd 实现，持有 codec、versioner、transformer、leaseManager、client |
| `ValidateObjectFunc` | `[staging] registry/rest/types.go` | validating admission 回调的函数类型 |

## 3. 函数级

### 函数：CreateResource
**源码：** `[staging] endpoints/handlers/create.go:245`
**职责：** 入口适配，直接返回 `createHandler(r, scope, admission, false)` 构造的 handler 闭包。
**参数：** `r rest.Creater`、`scope *RequestScope`、`admission admission.Interface`。

### 函数：createHandler
**源码：** `[staging] endpoints/handlers/create.go:53`
**所属：** 包 handlers
**职责：** Pod 创建的完整请求处理。
**参数（外层）：** `r rest.NamedCreater`、`scope *RequestScope`、`admit admission.Interface`、`includeName bool`（集合创建为 false）。
**返回（闭包）：** 无返回值；经 `ResponseWriter` 输出。
**前置条件：** 请求已通过全部 filter（认证/鉴权等），context 中带 user 与 RequestInfo。
**后置条件：** 成功时对象已持久化，201 已写出。
**执行流程（偏移相对函数体返回的闭包定义行 :54 计 +0）：**
```text
闭包入口 (+0)
├── 1. tracing span "Create" (+1 ～ +5)
├── 2. Namer.Name 取 namespace/name (+7 ～ +22)
│      name 缺失时回退取 namespace（本场景 name=nginx 来自对象，+97）
├── 3. context 34s 超时 (+26 ～ +27)
├── 4. 协商输出媒体类型 (+29 ～ +33)
├── 5. 协商输入序列化器 (+36 ～ +39)
├── 6. limitedReadBody 限量读 body (+40 ～ +46)
├── 7. 解析 CreateOptions（query）并校验 (+48 ～ +59)
├── 8. decode body → Pod（strict 视 FieldValidation）(+74 ～ +87)
├── 9. 校验对象 GV 与 scope 匹配 (+89 ～ +93)
├── 10. name 为空则取对象 name (+97 ～ +99)
├── 11. 写 namespace 入 ctx (+99)
├── 12. admission.WithAudit 装饰 + 审计记录对象 (+106 ～ +107)
├── 13. 擦除 ObjectMeta 系统字段 (+112 ～ +118)
├── 14. 保证对象 namespace 与请求一致 (+121 ～ +124)
├── 15. 构造 admissionAttributes（Create）(+127)
├── 16. requestFunc 闭包 → r.Create(+129 ～ +135)
├── 17. FinishRequest：
│      ├─ FieldManager.UpdateNoErrors（managed fields）(+146 ～ +147)
│      ├─ mutatingAdmission.Admit（+148 ～ +151）
│      ├─ dedup owner refs（+153）
│      └─ requestFunc；过大则清 managedFields 重试一次（+154 ～ +162）
├── 18. 错误检查 (+164 ～ +169)
└── 19. transformResponseObject 写 201 (+183 ～ +219)
```
**调用方：** go-restful RouteFunction（经 restfulCreateResource）。
**被调用：** negotiation、decoder.Decode、`FieldManager.UpdateNoErrors`、`mutatingAdmission.Admit`、`r.Create`（Pod storage.REST）。
**错误处理：** 前半段错误经 `scope.err` 做问题细节化输出（400/404/409 等）；admission 错误原样上抛；TooLarge 触发唯一一次重试。
**并发：** 单请求内同步；WithTimeout filter 已将处理放入 goroutine，闭包内 cancel 仅控制 context。
**副作用：** 审计日志、span、watch 广播（存储成功后由 storage 层触发）。

### 函数：AdmissionToValidateObjectFunc
**源码：** `[staging] registry/rest/create.go:218`
**职责：** 把 admission 的 validating 阶段（固定 Attributes）包成 Store 需要的 `ValidateObjectFunc`。
**关键逻辑：** 返回的函数在被调用时执行 `admit.Validate(ctx, staticAttributes, o)`；deep copy 语义由调用侧保证（Store.create 传入 deep copy）。

### 函数：(*Store).Create
**源码：** `[staging] generic/registry/store.go:514`
**职责：** dry-run 分流：dry-run 走不落盘路径，常规委托 `e.create`。

### 函数：(*Store).create
**源码：** `[staging] generic/registry/store.go:545`
**职责：** 通用创建主流程。
**参数：** `ctx`、`obj`（Pod）、`createValidation rest.ValidateObjectFunc`、`options *metav1.CreateOptions`。
**返回值：** `runtime.Object, error`。
**执行流程（以函数定义行 +0）：**
1. meta.Accessor（+4）→ `FillObjectMetaSystemFields`：填 UID/CreationTimestamp（+5 ～ +7）
2. GenerateName 场景调 `CreateStrategy.GenerateName`（+6 ～ +8；本场景 name 已指定，跳过）
3. `BeginCreate` 钩子（+14 ～ +23，Pod 未用，finishCreate=finishNothing）
4. `rest.BeforeCreate(CreateStrategy, ...)`（+23）——默认化+声明式校验，细节见下
5. `createValidation(ctx, obj.DeepCopyObject())`（+28 ～ +31）：validating admission（含 validating webhook）
6. `ObjectNameFunc` 取 name（+35）→ `KeyFunc` 生成 key（+38）：`/registry/pods/default/nginx`
7. `calculateTTL`（+43 ～ +45）；Pod 无 ttl，得 0
8. `e.NewFunc()` 建 out（+46）
9. `e.Storage.Create(ctx, key, obj, out, ttl, dryRun)`（+47）
10. 失败：`InterpretCreateError` → `CheckGeneratedNameError`（+48 ～ +49）；AlreadyExists 时 Get 现存对象并检查删除戳（+51 ～ +66）
11. 成功：finishCreate 提交（+69 ～ +72）→ `AfterCreate`/`Decorator`（+74 ～ +79）→ 返回 out
**调用方：** createHandler 的 requestFunc。
**被调用：** BeforeCreate、createValidation、KeyFunc、etcd3 store.Create、AfterCreate。
**状态变化：** 对象系统字段填充；etcd 新增；本地无持久状态。
**关键数据结构：** storage key、QualifiedResource。

### 函数：BeforeCreate
**源码：** `[staging] registry/rest/create.go:103`
**职责：** 校验前置元数据 + 执行 strategy 的默认化与声明式校验。
**执行流程：**
1. `objectMetaAndKind` 取 meta 与 kind（+1 ～ +3）
2. 确认系统字段已被填充（Store.create 第 1 步已做，+7 ～ +9）
3. 确认 GenerateName 已生成（+12 ～ +14）
4. 取 ctx 中 namespace 并核对对象 namespace（+17 ～ +24）
5. `strategy.PrepareForCreate`（+26）
6. `ValidateCreate`（声明式校验，+27 ～ +29）；有错聚合成 Invalid（422）
7. warnings（+31 ～ +33）；Canonicalize（+35）

### 函数：podStrategy.PrepareForCreate
**源码：** `pkg/registry/core/pod/strategy.go:88`
**职责：** Pod 创建默认化。
**内部步骤：**
1. Generation 置 1（+1）
2. Status 重置为 `{Phase: Pending}`（+3 ～ +5）
3. `DropDisabledPodFields`：按 feature gate 丢弃未启用字段（+7）
4. `applySchedulingGatedCondition`：有 scheduling gates 时补 Unschedulable 条件（+9）
5. `mutatePodAffinity`、`mutateTopologySpreadConstraints`：默认化亲和与拓扑分布（+10 ～ +11）
6. `applyAppArmorVersionSkew`（+12）、`DefaultPodLevelResources`（+13）
7. 计算 QOSClass（+15，`qos.GetPodQOS`：Guaranteed/Burstable/BestEffort）
**副作用：** 就地修改 Pod；无 IO。

### 函数：podStrategy.Validate
**源码：`pkg/registry/core/pod/strategy.go:125`
**职责：** 组装校验选项后调用 `corevalidation.ValidatePodCreate`（容器规范、资源、安全上下文、卷、亲和等的声明式校验全集）。
**返回值：** `field.ErrorList`；非空即导致 422。

### 函数：etcd3 store.Create
**源码：** `[staging] storage/etcd3/store.go:288`
**职责：** 对象落 etcd。
**参数：** `ctx`、`key`、`obj`、`out`、`ttl uint64`。
**返回值：** `error`。
**执行流程：**
```text
Create (+0)
├── 1. prepareKey 加命名空间前缀 (+1 ～ +3)
├── 2. tracing "Create etcd3" (+4 ～ +12)
├── 3. 拒绝创建时自带 ResourceVersion (+13 ～ +14)
├── 4. PrepareObjectResourceVersion（versioner 清 RV）(+15 ～ +17)
├── 5. runtime.Encode 序列化 (+19 ～ +23)
├── 6. ttl≠0 时 leaseManager.GetLease（Pod ttl=0，跳过）(+27 ～ +32)
├── 7. transformer.TransformToStorage 加密/变换 (+34 ～ +38)
├── 8. OptimisticPut(ctx, key, data, expectedRev=0, {LeaseID}) (+43)
│      expectedRev=0 ⇒ 仅当 key 不存在时事务成功
├── 9. 指标记录 (+44)
├── 10. !txnResp.Succeeded → KeyExistsError (+52 ～ +53)
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

[上一篇](05-apiserver启动与路由.md) · [总目录](README.md) · [下一篇](07-scheduler调度与绑定.md)
