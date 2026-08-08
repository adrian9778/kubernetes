# API、OpenAPI、Discovery 与兼容性

## 1. API 的源码分层

### 外部版本化类型

位于 `staging/src/k8s.io/api/<group>/<version>/types.go`，是客户端和线协议可见类型。

### 内部 hub 类型

位于 `pkg/apis/<group>/types.go`，控制面内部逻辑通常围绕它工作。

### 注册与安装

常见文件：

- `register.go`：GroupName、SchemeGroupVersion、AddKnownTypes。
- `install/install.go`：把内部/外部类型、转换和默认值加入 Scheme。
- `pkg/controlplane/import_known_versions.go`：确保已知 API group 被控制面链接并安装。

### 转换、默认值和验证

- `conversion.go` 与生成 conversion：外部版本和内部 hub 互转。
- `defaults.go` 与生成 defaults：补齐未指定值。
- `validation`：Create/Update 等操作的语义约束。

## 2. 请求执行链

```text
HTTP path
  -> API group/version/resource 路由
  -> negotiated serializer 解码外部对象
  -> defaulting
  -> conversion 到内部类型
  -> field validation / admission
  -> REST strategy
  -> storage encode
  -> etcd
```

读取方向相反：etcd 解码到 storage/internal 表示，转换为请求版本，再由 serializer 编码。

这里要区分：

- GVK：对象的 group/version/kind。
- GVR：REST 资源的 group/version/resource。
- Scheme：Go 类型、GVK、转换和默认值注册中心。
- RESTMapper：GVR、GVK、scope 之间的发现映射。

## 3. OpenAPI

关键入口：

- `hack/update-openapi-spec.sh`
- `hack/verify-openapi-spec.sh`
- `staging/src/k8s.io/apiserver/pkg/server/routes/openapi.go`
- API 类型上的 openapi/swagger doc 生成代码。

链路：

```text
Go API 类型与字段注释
  -> openapi-gen 生成模型 schema
  -> API Server 聚合 definitions/routes
  -> /openapi/v2 与 /openapi/v3 暴露
  -> update-openapi-spec 启动/查询规范
  -> 写入规范快照
  -> verify 比较快照
```

OpenAPI 影响 kubectl explain、客户端校验、server-side apply schema、生态工具生成和文档。字段能 JSON 序列化不代表 OpenAPI 就正确。

`verify-openapi-spec.sh` 先按 strict alpha removed API handling 检查；失败后可在非 strict 模式重试，以处理版本号切换与 release cutoff 之间的窗口。

## 4. Discovery

API discovery 至少包括：

- `/api` 与 `/apis` group/version/resource 信息。
- 聚合 discovery 文档。
- OpenAPI v2/v3 文档。

聚合 discovery handler 需要并发安全地维护 group/version 结果，并在 APIService、资源版本或 server 安装变化时更新。客户端以 ETag/cache-control 或资源内容判断是否刷新缓存。

调用关系：

```text
API 安装 RESTStorage
  -> GenericAPIServer 建立 APIGroupInfo
  -> discovery manager 收集 GroupVersionResources
  -> handler 序列化 APIGroupDiscoveryList
  -> discovery client 拉取
  -> RESTMapper/DeferredDiscovery 缓存
```

新增 API 后若 discovery 未出现，依次检查：类型注册、install import、storage map、group 安装、discovery handler，而不是只盯 OpenAPI。

## 5. API 兼容性

关键位置：

- `api/api-rules/`
- `api/api-rules/violation_exceptions.list`
- `hack/update-generated-api-compatibility-data.sh`
- `hack/verify-prerelease-lifecycle-tags.sh`
- `hack/verify-api-groups.sh`

### API rules

API linter 检查命名、字段、列表、状态等约定。历史 API 可能已经违反今天的规则，例外被记录在 known violations 中。新增 violation 不应通过随手增加 exception 掩盖。

### Prerelease lifecycle

生命周期标签描述 introduced、deprecated、replacement、removed 等版本信息。生成器据此产生方法，verify 检查标签完整性与版本关系。

### Generated compatibility data

兼容性快照把当前 API 表面固化为机器可比较数据，用于发现字段、类型或版本意外删除/变化。更新快照意味着接受新的契约，必须审查 diff，而不是机械提交。

### `verify-api-groups.sh`

它从 `pkg/apis/**/register.go` 收集 group：

1. 要求每个 register 文件定义 `GroupName`。
2. 检查需生成 client 的 group 是否进入 client-gen。
3. 检查内部 group install 包是否被控制面导入。
4. 检查外部 group/version 是否列入 `hack/lib/init.sh` 的已知版本集合。

## 6. API 变更类型与动作

### 新增可选字段

需要：类型注释、JSON/protobuf tag、deepcopy/protobuf/OpenAPI、default/validation、round-trip 测试、compatibility data、integration。

### 修改默认值

需要：默认值单测、版本转换测试、create/read integration；要评估旧存储对象读取时是否改变语义。

### 新增 API 版本

需要：外部类型、register、conversion、install、storage version、client/lister/informer/apply、OpenAPI/discovery、lifecycle、兼容性和 e2e。

### 删除/废弃 API

需要：生命周期标签、严格 removed API 行为、discovery/OpenAPI 更新、compatibility 审查、迁移测试和 release note。不能只删除目录。

## 7. v1.37 开发树的观察点

仅基于当前源码可确认：

- staging publishing 图新增 `streaming` 仓库，并成为 apimachinery、api、client-go、code-generator 等发布依赖之一。
- scheduler 相关 API/informer 出现 PodGroup、Workload、CompositePodGroup 等资源路径。
- scheduler 单测出现 gang scheduling、PodGroup preemption、API dispatcher/cache 等新链路。
- 发布规则已覆盖 `release-1.36`，master 因而承载下一开发周期的 API 与依赖演进。

这些变化意味着修改调度 API 时不能只跑 plugin 单测，还要检查生成 client/informer、scheduler integration/perf、OpenAPI/discovery、staging dependency/publishing。

