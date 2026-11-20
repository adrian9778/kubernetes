[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# admission webhook

> **层级**：跨篇机制纵深专题——把 **admission webhook**（mutating + validating）讲透：两种 webhook 插件的装配、Dispatch 分发、webhook 调用与 patch 应用、失败语义（fail-open/fail-closed）。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `a.Dispatch+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[api-server](api-server.md) §3.2 是浓缩版（admission 在 handler 内、mutating→validation 顺序）；本专题纵向展开 webhook 插件的分发与调用语义。

---

## 0. 结论速览

### 0.1 admission webhook 解决什么问题

让**外部服务**（webhook server）在对象持久化前参与决策：**mutating webhook** 可修改对象（注入 sidecar、加标签），**validating webhook** 可拒绝不合规对象。这是 Kubernetes 最主要的扩展点之一。

### 0.2 两种 webhook 对比

| | mutating webhook | validating webhook |
|---|---|---|
| 接口方法 | `Admit` | `Validate` |
| 能否改对象 | **能**（返回 JSON Patch） | **不能**（只 allow/deny） |
| 配置对象 | `MutatingWebhookConfiguration` | `ValidatingWebhookConfiguration` |
| 调用时机 | validation **之前** | validation **之后**（看最终对象） |
| 失败语义 | 可配 fail-open/fail-closed | 同 |

### 0.3 关键结论

1. **admission 在 handler 内，不在中间件链**：`createHandler`/`updateHandler` 显式调用，需解码后的对象 + 操作语义。
2. **顺序**：mutating admission → 对象默认化/validation → validating admission。
3. **两种触发方式**：mutating 内联 `Admit`；validating 经 `AdmissionToValidateObjectFunc` 在 storage 写内触发（看最终写入对象）。
4. **Dispatch 分发到多个 webhook**：按配置的规则（namespaceSelector/objectSelector/rules）筛选匹配的 webhook，逐个调用。
5. **fail-open vs fail-closed**：webhook 调用失败时，按 `failurePolicy`（Ignore=继续/`Fail` =拒绝）决定。

---

## 1. 生产视角：webhook 对外行为

### 1.1 用户操作与可见结果

| 操作 | 结果 |
|---|---|
| 创建 MutatingWebhookConfiguration | 匹配的对象创建/更新时被 webhook 修改（如注入 sidecar） |
| 创建 ValidatingWebhookConfiguration | 匹配的对象创建/更新时被校验（不合规拒绝） |
| webhook server 挂了 | 按 failurePolicy：Ignore 放行 / Fail 拒绝 |

### 1.2 可观测信号

- 对象上的 mutation annotation（`k8s.io/.../mutating-webhook`）。
- webhook 调用延迟/失败指标。
- 对象被拒时的事件/错误（含 webhook name）。

---

## 2. 开发视角：mutating webhook

### 2.1 装配（mutating/plugin.go）

`NewMutatingWebhook+0`（mutating/plugin.go :72）读配置文件、构造 `Plugin`（含 `newMutatingDispatcher`，dispatcher.go :73）。

### 2.2 `Plugin.Admit`——入口（mutating/plugin.go `a.Admit+0`，:99）

```text
Admit(ctx, attr, o)                              a.Admit+0
└── Dispatch(ctx, attr, o, hooks)                a.Dispatch+0 (dispatcher.go:105)
      ↑ 选出匹配的 mutating webhook，逐个调用
```

### 2.3 `mutatingDispatcher.Dispatch`——分发（dispatcher.go `a.Dispatch+0`，:105）

```text
Dispatch(ctx, attr, o, hooks)                    a.Dispatch+0
├── 按规则筛选匹配的 webhook（namespaceSelector/objectSelector/rules/apiGroups/verbs）
├── for each 匹配的 webhook（可多轮 reinvocation）:
│    └── callAttrMutatingHook(...)                a.callAttrMutatingHook+0 (:244)
│         ↑ 调 webhook server，拿 JSON Patch，应用到对象
└── 应用所有 patch 到对象（mutated 则记 annotation）
```

### 2.4 `callAttrMutatingHook`——调单个 webhook（dispatcher.go `a.callAttrMutatingHook+0`，:244）

调 webhook server 的 `/mutate` 接口，拿 `AdmissionReview` 响应（含 JSON Patch），应用到对象。失败按 `failurePolicy` 处理。

---

## 3. 开发视角：validating webhook

### 3.1 装配（validating/plugin.go）

`NewValidatingAdmissionWebhook+0`（validating/plugin.go :74）构造 `Plugin`（含 `newValidatingDispatcher`，:60）。

### 3.2 `Plugin.Validate`——入口（validating/plugin.go `a.Validate+0`，:93）

```text
Validate(ctx, attr, o)                           a.Validate+0
└── Dispatch(ctx, attr, o, hooks)                d.Dispatch+0 (dispatcher.go:88)
      ↑ 选出匹配的 validating webhook，逐个调用（只 allow/deny，不改对象）
```

### 3.3 `validatingDispatcher.Dispatch`（dispatcher.go `d.Dispatch+0`，:88）

与 mutating 类似，但**不应用 patch**——只收集各 webhook 的 allow/deny 决定，任一 deny 则拒绝。

---

## 4. 两种触发方式（在 handler 内）

### 4.1 mutating 内联触发

`createHandler` 直接调 `mutatingAdmission.Admit(ctx, admissionAttributes, scope)`（create.go :203）——在对象默认化之前修改对象。

### 4.2 validating 经 storage 写触发

`createHandler` 把 `rest.AdmissionToValidateObjectFunc(admit, attrs, scope)`（create.go :188，函数在 registry/rest/create.go `AdmissionToValidateObjectFunc+0`，:218）作为 validate 回调传给 `Store.Create`——validating 在 storage 写路径内执行，**看最终写入对象**。update 用 `AdmissionToValidateObjectUpdateFunc+0`（update.go :286）。

### 4.3 admission 链（chain）

`NewChainHandler+0`（admission/chain.go :26）串联多个 admission 插件；`admission.Interface`（interfaces.go :124）定义 `Admit`/`Validate`。

---

## 5. 全链路时序（一次 CREATE 带 webhook）

```text
POST /api/v1/namespaces/default/pods
└── createHandler (create.go:53)
     ├── mutatingAdmission.Admit (mutating/plugin.go:99)
     │    └── Dispatch (dispatcher.go:105)
     │         └── callAttrMutatingHook (:244) × N 匹配的 mutating webhook
     │              └── webhook server /mutate → JSON Patch → 应用到对象
     ├── 对象默认化 + validation
     └── Store.Create → validating 回调 AdmissionToValidateObjectFunc (create.go:218)
          └── validatingAdmission.Validate (validating/plugin.go:93)
               └── Dispatch (dispatcher.go:88) × N 匹配的 validating webhook
                    └── webhook server /validate → allow/deny
                         └── 全部 allow → 落 etcd；任一 deny → 拒绝
```

---

## 6. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| mutating 在 validation 前 | `a.Admit+0` → `a.Dispatch+0` | 先改对象再校验最终形态 |
| validating 看最终对象 | `AdmissionToValidateObjectFunc+0`（storage 写内） | 校验的是真正要写入的对象 |
| 失败语义可控 | `callAttrMutatingHook+0`（failurePolicy） | fail-open/fail-closed 按配置 |
| 多 webhook 顺序分发 | `a.Dispatch+0`（逐个调用 + reinvocation） | 一个 webhook 的修改对后续可见 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- mutating：`NewMutatingWebhook+0`（plugin.go:72）、`a.Admit+0`（:99）、`a.Dispatch+0`（dispatcher.go:105）、`a.callAttrMutatingHook+0`（:244）、`newMutatingDispatcher`（:73）
- validating：`NewValidatingAdmissionWebhook+0`（plugin.go:74）、`a.Validate+0`（:93）、`d.Dispatch+0`（dispatcher.go:88）、`newValidatingDispatcher`（:60）
- chain：`NewChainHandler+0`（chain.go:26）、`admission.Interface`（interfaces.go:124）
- 触发：`AdmissionToValidateObjectFunc+0`（registry/rest/create.go:218）、`AdmissionToValidateObjectUpdateFunc+0`（update.go:286）、create.go 调用点（:188/:203）

### 7.2 生产实测

**未实测** `[待运行验证]`。webhook 实际调用时序、patch 应用、fail-open/fail-closed 行为均为代码推断，未在真实集群挂 webhook 观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| reinvocation 细节 | 未深挖 | mutating 多轮 reinvocation 的触发条件未逐行读 |
| namespaceSelector 匹配 | 未深挖 | webhook 规则匹配（selector/rules/verbs）细节未展开 |
| failurePolicy 实现 | 未深挖 | Ignore/Fail 的精确分支未读 |
| conversion webhook | 见 CRD 专题 | CRD 多版本转换 webhook 未在本专题覆盖 |

### 7.4 修改联动

- 改 mutating 分发 → `mutatingDispatcher.Dispatch`/`callAttrMutatingHook`，同步本文 §2。
- 改 validating 分发 → `validatingDispatcher.Dispatch`，同步 §3。
- 改触发时机 → `AdmissionToValidateObjectFunc`，同步 §4.2。

**关联文档：** [api-server](api-server.md)（admission 定位浓缩）、[06-apiserver请求处理与etcd](../06-apiserver请求处理与etcd.md)（createHandler）、[17-CRD扩展与聚合层](../17-CRD扩展与聚合层.md)（conversion webhook）。

[总目录](../README.md)
