[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# CRD conversion webhook

> **层级**：跨篇机制纵深专题——把 **CRD 多版本转换**讲透：CRD 声明多个 version 时，对象在不同 version 间如何转换（webhook vs 内部默认转换）、`Converter` 接口、safe/unsafe 包装、转换校验。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `c.Convert+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md) 和 [CRD建立与懒分发](CRD建立与懒分发.md) 是浓缩版；本专题纵向展开多版本转换语义。

---

## 0. 结论速览

### 0.1 conversion 解决什么问题

一个 CRD 可以声明**多个 version**（如 v1、v2），但存储只有一个「storage version」。当客户端读写非 storage version 时，需要把对象**转换**到目标 version。转换有两种方式：**webhook**（用户提供的转换服务）或**内部默认**（仅结构相同的版本间）。

### 0.2 关键结论

1. **两种转换器**：`webhookConverter`（调用户的 conversion webhook）vs `nopConverter`（单版本/结构相同，无需转换）。
2. **由 CRD spec 决定**：CRD 声明 `conversion.strategy`（`Webhook` 或 `None`）→ 工厂选对应转换器。
3. **safe/unsafe 包装**：`safeConverterWrapper` 做防御性拷贝（转换前 copy，避免污染原对象）。
4. **转换后校验**：`validateConvertedObject` 校验转换结果（UID/name 不变等）。
5. **batch 转换**：List 场景把多个对象打包一次 webhook 调用（`getObjectsToConvert`）。

---

## 1. 生产视角：conversion 对外行为

### 1.1 用户操作与可见结果

| 场景 | 行为 |
|---|---|
| CRD 单 version | nopConverter（无需转换） |
| CRD 多 version + None 策略 | 仅结构相同版本间转换（内部默认） |
| CRD 多 version + Webhook 策略 | 调用户的 conversion webhook 转换 |
| 客户端读写非 storage version | 自动转换到目标 version |

### 1.2 可观测信号

- conversion webhook 调用延迟/失败。
- 转换后对象 UID/name 不匹配 → 拒绝（validateConvertedObject）。

---

## 2. 开发视角：转换器工厂（converter.go）

### 2.1 `NewCRConverterFactory`（converter.go `NewCRConverterFactory+2`，:47）

```text
NewCRConverterFactory(...)
├── newWebhookConverterFactory(...)        webhookConverterFactory (:47)
│     ↑ 构造 webhook 转换器工厂（serviceResolver + authResolver）
└── NewConverter(crd):
      ├── 单 version / None 策略 → nopConverter        m.NewConverter+9 (:65)
      └── Webhook 策略 → webhookConverterFactory.NewWebhookConverter(crd)  m.NewConverter+11 (:67)
```
**策略分派：** 由 CRD `spec.conversion.strategy` 决定用 nopConverter 还是 webhookConverter。

### 2.2 `nopConverter`（nop_converter.go `nopConverter` :26）

`Convert+0`（:32）：单版本/结构相同场景，直接返回原对象（无实际转换）。这是大多数单版本 CRD 的路径。

---

## 3. 开发视角：webhook 转换器（webhook_converter.go）

### 3.1 工厂与转换器（`newWebhookConverterFactory+0`，:48 → `webhookConverter+0`，:69）

`webhookConverterFactory`（:44）持有 serviceResolver（解析 conversion webhook Service）+ authResolver。`NewWebhookConverter+0`（:107）为单个 CRD 构造 `webhookConverter`。

### 3.2 `webhookConverter.Convert`——调 webhook 转换（`c.Convert+0`，:236）

```text
Convert(in, toGV)                                c.Convert+0
├── getObjectsToConvert(in, apiVersion)          getObjectsToConvert+0 (:126)
│     ↑ List 场景：把多个对象打包
├── 调 conversion webhook（POST /convert）
│     └── webhook 返回转换后的对象
├── getConvertedObjectsFromResponse(...)          getConvertedObjectsFromResponse+0 (:186)
│     ↑ 从响应取回转换后的对象
├── validateConvertedObject(in, out)              validateConvertedObject+0 (:392)
│     ↑ 校验 UID/name 不变
└── 返回转换后的对象
```

### 3.3 batch 转换（`getObjectsToConvert+0`，:126）

List 场景把多个对象打包成一次 webhook 调用（减少往返）。`getConvertedObjectsFromResponse+0`（:186）从响应取回按 UID 匹配的转换后对象。

---

## 4. 开发视角：safe/unsafe 包装（converter.go）

### 4.1 `crConverter.ConvertToVersion`（converter.go `c.ConvertToVersion+0`，:175）

```text
ConvertToVersion(in, target)
├── 若目标 version == 当前 version → 直接返回（无需转换）
├── 单版本/结构相同 → typedscheme.Scheme.ConvertToVersion（内部默认转换）
└── 否则 → 委托 webhookConverter.Convert
```

### 4.2 `safeConverterWrapper`——防御性拷贝

`safeConverterWrapper.ConvertFieldLabel+0`（:218）/`ConvertToVersion+0`（:232）：转换前**拷贝**对象，避免转换污染原对象（unsafe 转换器可能就地修改）。
- `ConvertFieldLabel+0`（:218）：委托 unsafe converter。
- `ConvertToVersion+0`（:232）：**先 copy** 再委托。

---

## 5. 全链路时序（读写非 storage version）

```text
客户端 GET /apis/<group>/v2/<plural>（storage version 是 v1）
└── crdHandler → CR storage REST
     └── ConvertToVersion (converter.go:175)
          ├── 目标 v2 == 当前 v2 → 直接返回
          └── 需转换（storage v1 → v2）：
               └── safeConverterWrapper.ConvertToVersion (:232) 先 copy
                    └── webhookConverter.Convert (webhook_converter.go:236)
                         ├── getObjectsToConvert (:126) 打包
                         ├── POST conversion webhook /convert
                         ├── getConvertedObjectsFromResponse (:186) 取回
                         └── validateConvertedObject (:392) 校验 UID/name
                              └── 返回 v2 对象给客户端
```

---

## 6. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| 策略分派 | `m.NewConverter+9/+11`（:65/:67） | 单版本不调 webhook（省开销） |
| 防御性拷贝 | `safeConverterWrapper.ConvertToVersion+0`（:232） | 转换不污染原对象 |
| 转换后校验 | `validateConvertedObject+0`（:392） | UID/name 不变，防 webhook 篡改 |
| batch 转换 | `getObjectsToConvert+0`（:126） | List 场景减少 webhook 往返 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- webhook_converter.go：`newWebhookConverterFactory+0`（:48）、`webhookConverter+0`（:69）、`NewWebhookConverter+0`（:107）、`getObjectsToConvert+0`（:126）、`getConvertedObjectsFromResponse+0`（:186）、`c.Convert+0`（:236）、`validateConvertedObject+0`（:392）
- converter.go：`NewCRConverterFactory+2`（:47）、`m.NewConverter+9`（:65 nopConverter）、`m.NewConverter+11`（:67 webhookConverter）、`c.ConvertToVersion+0`（:175 crConverter）、`c.ConvertFieldLabel+0`（:218）/`c.ConvertToVersion+0`（:232 safeConverterWrapper）
- nop_converter.go：`nopConverter`（:26）、`Convert+0`（:32）

### 7.2 生产实测

**未实测** `[待运行验证]`。conversion webhook 实际调用、batch 打包、转换后校验均为代码推断，未在真实多版本 CRD 集群观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| conversion webhook 配置 | 未深挖 | ConversionReview 版本、service/webhook 解析细节未展开 |
| 结构相同判定 | 未深挖 | None 策略下「结构相同」的精确判定未读 |
| field label 转换 | 未深挖 | `ConvertFieldLabel` 的 label 映射细节未展开 |
| 转换失败处理 | 未深挖 | webhook 失败/超时的错误传播未读 |

### 7.4 修改联动

- 改转换分派 → `NewConverter`/`NewCRConverterFactory`，同步本文 §2。
- 改 webhook 调用 → `webhookConverter.Convert`/`getObjectsToConvert`，同步 §3.2。
- 改 safe 包装 → `safeConverterWrapper`，同步 §4.2。

**关联文档：** [CRD建立与懒分发](CRD建立与懒分发.md)（CRD 懒分发）、[17-CRD扩展与聚合层](../sourceReader/17-CRD扩展与聚合层.md)（浓缩版）、[aggregator代理转发](aggregator代理转发.md)（聚合代理）。

[总目录](../sourceReader/README.md)
