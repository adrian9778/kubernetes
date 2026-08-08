# 04. API 对象、元数据与版本设计

## 4.1 GVK 与 GVR

Group/Version/Kind 标识编码对象，例如 `apps/v1, Deployment`；Group/Version/Resource 标识 REST 集合，例如 `apps/v1/deployments`。Kind 面向 Go 对象和序列化，Resource 面向 URL 和 discovery。RESTMapper 负责映射，不能总靠字符串复数化。

## 4.2 API 类型位置

- 外部公开类型：`staging/src/k8s.io/api/<group>/<version>/types.go`。
- Kubernetes 内部 hub 类型：`pkg/apis/<group>/types.go`。
- register/default/conversion/validation：同 group 目录及 `pkg/apis`。
- REST strategy/storage：`pkg/registry/<group>/<resource>`。
- 生成 client/informer/lister：`staging/src/k8s.io/client-go`。

## 4.3 spec、status 和 metadata

Spec 是期望，Status 是观察结果，ObjectMeta 提供身份和并发协议。控制器应通过 status subresource 写自己负责的状态，避免覆盖用户同时修改的 spec。

关键字段：

- UID：一次对象生命周期的身份，同名重建会变。
- resourceVersion：不透明并发版本和 watch 续接点。
- generation：期望状态变更代数。
- observedGeneration：控制器 status 已处理到的 generation。
- ownerReferences：垃圾回收依赖图。
- finalizers：删除前尚未完成的清理责任。
- managedFields：Server-Side Apply 字段所有权。

## 4.4 创建链上的对象处理

```text
decode external version
 -> default
 -> convert to internal
 -> mutating admission
 -> validation / validating admission
 -> strategy PrepareForCreate
 -> storage version conversion
 -> encode and persist
```

实际阶段由 API Server request scope 和 registry 组合，顺序影响安全和兼容性。默认值不应覆盖显式输入；validation 应返回 field path；conversion 理想情况下往返不丢语义。

## 4.5 删除不是立即消失

有 finalizer 时 Delete 设置 deletionTimestamp，对象仍可 List/Get。负责外部清理的控制器完成幂等动作后只移除自己的 finalizer；列表为空后 storage 才真正删除。Grace period 还会约束 Pod 终止时间。

OwnerReference 的 UID 防止同名新对象被旧依赖误关联。Foreground、Background 和 Orphan propagation 决定 owner 与 dependents 的删除顺序。

## 4.6 API 兼容性

公开版本增加字段要考虑 optionality、零值、默认值、protobuf tag、OpenAPI schema、patch/list/map semantics、feature gate 和旧存储对象转换。删除字段、改变类型、收窄合法值或改变默认行为都可能破坏客户端。

Storage version 不一定等于请求版本。升级时 etcd 中可能长期存在旧编码，需要 conversion 和 storage version migration。

## 4.7 Patch 与 Apply

JSON Patch 表达操作序列；Merge Patch 递归合并对象；Strategic Merge Patch 使用 Go tag 理解列表合并；Server-Side Apply 使用结构化 schema 和 managedFields 追踪字段所有者。CRD 必须有结构化 schema 才能获得完整 apply 行为。

## 4.8 API 设计步骤

1. 写用户故事和不变量。
2. 定义 spec/status，避免把可推导状态放 spec。
3. 选择 namespaced 或 cluster scoped。
4. 定义 default、validation、subresources、printer columns。
5. 注册 Scheme 和 conversion。
6. 生成 DeepCopy、client、OpenAPI、protobuf 等。
7. 实现 registry strategy/storage。
8. 加 round-trip、compatibility、integration 和 e2e 测试。

## 4.9 当前版本的新对象线索

`scheduling` API 已包含 PodGroup，并出现 CompositePodGroup 相关版本；Pod spec 中可表达 scheduling group。`resource` API 包含 ResourceClaim、ResourceSlice、DeviceClass、DeviceTaintRule 等 DRA 对象。阅读这些 API 时必须同步检查 feature gate 和版本级别。

