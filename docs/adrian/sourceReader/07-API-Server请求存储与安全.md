# 07. API Server：请求链、RESTStorage、etcd 与安全

## 7.1 三层 Server Chain

`cmd/kube-apiserver/app/server.go` 的 `CreateServerChain` 先创建最终 NotFound，再创建 APIExtensions Server、Kube API Server，最后创建最外层 Aggregator。对象创建顺序与请求进入顺序相反：

```text
Client -> Aggregator -> built-in Kubernetes API -> CRD APIExtensions -> NotFound
```

每层实现 DelegationTarget。这样 aggregated API、内置 API 和 CRD 共用地址、认证上下文、发现体系和部分过滤器，但各自保留 registry 与 storage。

## 7.2 GenericAPIServer 生命周期

[genericapiserver.go](../../../staging/src/k8s.io/apiserver/pkg/server/genericapiserver.go) 的核心阶段：Config 聚合依赖；Complete 补默认并冻结不变量；New 创建 handler/server；InstallAPIGroups 注册资源；PrepareRun 安装 OpenAPI、health 等最终路由；RunWithContext 启动 serving 和 hooks。

PostStartHook 用于 server 已监听后启动 informer、控制器或初始化任务；PreShutdownHook 做停止前清理。Hook 名必须唯一，readyz 可等待关键 hook 完成。

## 7.3 HTTP Filter Chain

概念顺序：panic recovery、request info/context、审计、认证、impersonation、授权、priority-and-fairness、timeout、admission 相关请求处理和 API handler。真实嵌套顺序见 `DefaultBuildHandlerChain` 及 `pkg/endpoints/filters`。

顺序是安全属性：没有 RequestInfo 就无法正确授权 resource/verb；没有认证 user 就不能授权；审计需要记录最终身份和响应；timeout 与 long-running watch 的处理不同。

## 7.4 Authentication

Authenticator 把 x509、Bearer token、service account token、bootstrap token、webhook 等凭证转换为 `user.Info`。多个实现可 union/chain。成功认证不表示有权限；anonymous 是否启用由配置决定。

Kubelet、aggregated server、front-proxy 等场景还有专门证书和 request-header 身份链。错误配置代理 CA 或允许客户端伪造身份头会形成高风险边界。

## 7.5 Authorization

Authorizer 根据 Attributes：user、groups、verb、API group/version、resource/subresource、namespace、name 或 non-resource URL，返回 Allow、Deny、NoOpinion。RBAC、Node、Webhook、ABAC 等可以组合。

RBAC rule 只表达允许；没有匹配通常 NoOpinion，最终组合结果决定拒绝。Node Authorizer 与 NodeRestriction admission 共同限制 kubelet，只配一侧不完整。

## 7.6 Admission

Mutating admission 可修改对象，validating admission 只允许或拒绝。插件初始化器注入 informer、client、RESTMapper 等依赖。Webhook 还需处理 matchPolicy、namespace/object selector、timeoutSeconds、failurePolicy、sideEffects、reinvocationPolicy 和版本转换。

Admission 在授权后执行，不能替代 RBAC。Webhook 必须有超时和可用性策略，否则控制面写请求会被外部服务拖死。

## 7.7 API Priority and Fairness

APF 用 FlowSchema 将请求匹配到 PriorityLevelConfiguration，提供隔离、排队、并发份额和拒绝。目标是过载时保护系统关键请求，不是简单令牌桶。Watch 建立阶段和持续执行阶段的资源计算有专门语义。

## 7.8 API 安装

Kubernetes `RESTStorageProvider` 按 API group 创建 `APIGroupInfo.VersionedResourcesStorageMap`。generic server 将 `rest.Storage` 能力翻译成 routes。实现 Getter、Lister、Creater、Updater、GracefulDeleter、Watcher 等接口会决定公开 verbs。

Subresource 是独立 storage，例如 `/status`、`/scale`、`/binding`、`/eviction`。它们可以使用不同 strategy 和权限，不能当成主对象普通字段更新。

## 7.9 Registry Store 与 Strategy

`pkg/registry` 的通用 Store 实现 CRUD 框架。Strategy 决定 namespace scope、PrepareForCreate/Update、Validate、Warnings、Canonicalize、AllowCreateOnUpdate、AllowUnconditionalUpdate 等资源语义。

例如 status strategy 在 `/status` 更新时保留旧 spec，主资源 strategy 可能保留旧 status。把规则放 strategy 而不是 HTTP handler，能复用统一 storage 流程并单测。

## 7.10 etcd Storage

StorageFactory 根据 group/resource 选择 codec、prefix、server list、transformer 和 storage version。对象在持久化前转换为 storage version，并可经过 envelope encryption transformer。

Update 使用乐观并发和 etcd transaction。GuaranteedUpdate 循环读取、调用更新函数、比较版本并提交；冲突要重新读取重算。资源 key 通常包含 registry prefix、namespace 和 name。

Watch cache 位于 etcd 与多数 API 读/watch 之间，降低 etcd 负载并提供事件历史窗口。resourceVersion 语义决定请求是否可由 cache 满足。

## 7.11 加密与 KMS

EncryptionConfiguration 可按 resource 配置 identity、AES、secretbox、KMS 等 providers。Provider 顺序决定写入使用第一个，读取尝试所有，因此轮换通常把新 provider 放首位并重写对象。KMS v2 通过插件边界管理 KEK/DEK 相关操作。

## 7.12 删除协议

Delete 可能设置 deletionTimestamp/gracePeriod/finalizers；GC 根据 ownerReferences 处理传播；Pod graceful deletion 还需 Kubelet 执行。API 返回删除成功不总表示外部资源已清理。

## 7.13 重建顺序

先实现 Scheme + 内存 CRUD + resourceVersion CAS + watch，再实现 RESTStorage/Strategy、List selector、status subresource，随后替换 etcd，最后加入 authn/authz/admission/APF/audit/encryption/aggregation。不要一开始复制完整 GenericAPIServer 配置。

