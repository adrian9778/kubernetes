# 05. apimachinery：Scheme、Codec、Watch 与通用语义

## 5.1 runtime.Object

`runtime.Object` 提供 `GetObjectKind` 和 `DeepCopyObject`，让未知具体类型的对象进入统一编码、转换、缓存和 REST 流程。Informer 对象被共享，修改前必须 DeepCopy。

## 5.2 Scheme

核心在 [scheme.go](../../../staging/src/k8s.io/apimachinery/pkg/runtime/scheme.go)。Scheme 保存 GVK 到 Go type、type 到 GVK、conversion、defaulting 和相关映射。`AddKnownTypes` 注册对象与 List；`New` 按 GVK 创建实例；`Convert` 调 conversion；`Default` 应用默认函数。

Scheme 是进程内类型注册表，不是 OpenAPI schema，也不是 discovery。一个类型能被 Scheme decode，不代表 REST 路由已经安装。

## 5.3 CodecFactory

[serializer](../../../staging/src/k8s.io/apimachinery/pkg/runtime/serializer) 根据 Scheme 构建 JSON、protobuf、CBOR 等 serializer。UniversalDeserializer 读取 TypeMeta 识别类型；versioning codec 在 wire version、internal version 和 storage version 间转换。

HTTP Content-Type 表示请求体，Accept 表示响应偏好。NegotiatedSerializer 把 media type、encoder、decoder 和 stream framing 连接起来。Watch 使用流式帧，每个事件仍包含类型化对象或 Status。

## 5.4 conversion 与 defaulting

生成 conversion 处理同名可转换字段，手写 conversion 处理语义变化。Unsafe conversion 优化可能共享底层内存，调用方必须遵循不可变约束。Defaulting 只补未指定值；指针字段常用于区分未指定和显式零值。

## 5.5 meta 与 unstructured

`metav1` 定义 TypeMeta、ObjectMeta、ListMeta、Status、APIResource 等协议对象。`unstructured.Unstructured` 用 `map[string]any` 承载运行时未知类型，dynamic client 和通用工具依赖它。Nested helpers 访问字段时要处理缺失、类型不符和副本语义。

## 5.6 labels、fields 和 selection

Label selector 支持 equality 和 set requirements；field selector 只支持服务器为资源实现的字段集合。Selector 可在 List/Watch 请求中减少服务器返回，但不是任意查询语言。

## 5.7 watch

[watch.go](../../../staging/src/k8s.io/apimachinery/pkg/watch/watch.go) 定义 Added、Modified、Deleted、Bookmark、Error。Watch 是有限历史窗口，不是可靠永久日志。resourceVersion 过旧时客户端必须 relist。Bookmark 推进版本，不表示业务对象改变。

## 5.8 errors 与 Status

API 错误在 wire 上是 metav1.Status，client 转为 StatusError。应使用 `apierrors.IsNotFound/IsConflict/IsTooManyRequests` 等判断。Retry-After、reason 和 details 比字符串更稳定。

## 5.9 wait、clock 和 sets

apimachinery 还提供 wait/backoff、sets、fields、labels、version、util runtime 等广泛基础能力。控制器测试应注入 fake clock，避免真实 sleep；sets 用于表达成员关系，但返回 map/slice 时仍要关注共享可变性。

## 5.10 教学实现

最小 API Machinery 需要：GVK/GVR 类型、Scheme 注册表、runtime.Object、JSON envelope、DeepCopy、conversion/default hooks、StatusError、label selector、ListMeta/resourceVersion 和 watch.Event。先实现正确语义，再考虑反射生成和 protobuf 性能。

