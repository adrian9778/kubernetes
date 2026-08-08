# 08. CRD、Aggregated API、Webhook 与扩展边界

## 8.1 CRD 做了什么

apiextensions-apiserver 观察 CustomResourceDefinition，动态创建对应 REST storage、路由、discovery 和 OpenAPI。CRD 的 schema 不只是文档，还驱动 pruning、default、validation、conversion、Server-Side Apply 和字段相关语义。

核心源码在 `staging/src/k8s.io/apiextensions-apiserver/pkg`：apiserver 装配、registry/customresource、schema、conversion、controller 和 webhook。

## 8.2 Structural Schema

结构化 schema 要求类型、properties/items 和组合关键字满足约束，服务器才能安全裁剪未知字段并建立 typed field set。`x-kubernetes-list-type`、`list-map-keys`、`map-type` 和 preserve-unknown-fields 直接影响 merge/apply。

设计 CRD 列表时必须明确 atomic、set 或 map。选错后再修改会改变 patch/ownership 语义，属于兼容性风险。

## 8.3 CRD Conversion

多个 served version 共享一个 storage version。None strategy 只做字段兼容；Webhook conversion 把对象批量发送给转换服务。转换必须保留 metadata、对象顺序和数量，并保证往返语义。

Conversion webhook 不应实现 default 或业务副作用；失败会影响所有对应版本读写，应有高可用和短延迟。

## 8.4 CEL Validation

CRD 可通过 `x-kubernetes-validations` 使用 CEL 表达对象级规则。规则成本预算防止表达式耗尽 API Server。transition rule 可比较 oldSelf，适合不可变字段等更新约束。CEL 不是任意外部查询语言，不能访问集群其他对象。

## 8.5 Aggregated API

kube-aggregator 观察 APIService，根据 group/version 把请求代理到独立 extension apiserver。Aggregator 负责 discovery 合并、可用性状态、proxy transport 和 front-proxy 身份传递。

Extension server 必须验证 request-header client CA，只信任 aggregator 发来的身份头；同时正确暴露 discovery/OpenAPI。APIService Available condition 是首要排障入口。

## 8.6 CRD 与 Aggregated API 的选择

- CRD：声明式数据 API、无需自定义存储协议、部署简单。
- Aggregated API：需要自定义 REST 行为、非 etcd 后端、特殊子资源或协议控制。

大多数 operator 应从 CRD 开始。Aggregated API 带来独立 server、安全、证书、发现聚合和升级兼容成本。

## 8.7 Admission Webhook 与 CRD Controller 的边界

Webhook 位于写请求同步路径，必须快速、确定、无长事务；Controller 位于异步收敛路径，可重试并处理外部资源。能异步完成的云资源创建不应放 webhook。Webhook 适合默认和拒绝，Controller 适合实现状态。

## 8.8 Sample 源码

- `staging/src/k8s.io/sample-apiserver`：聚合 API server 示例。
- `staging/src/k8s.io/sample-controller`：client-go 控制器示例。
- `pkg/controlplane/apiserver/samples/generic`：仓库内 generic server 装配示例。

阅读样例时要对照生产实现补齐 auth、HA、APF、审计、schema 兼容和测试，样例只展示主干。

