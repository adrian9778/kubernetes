[上一篇](../21-设备插件与网络边界.md) · [总目录](../README.md) · [返回专题索引](../README.md#专题topics)

# kubelet image pull 与背压

> **层级**：跨篇机制纵深专题——把 kubelet **镜像拉取与背压**讲透：`EnsureImageExists` 拉取决策、pull backoff（指数退避）、QPS 节流（throttleImagePulling）、并行/串行 puller、镜像 GC。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `m.EnsureImageExists+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[10-kubelet-CRI运行时](../10-kubelet-CRI运行时.md) §imagePuller 是浓缩版；本专题纵向展开拉取决策、背压与 GC 语义。

---

## 0. 结论速览

### 0.1 image pull 背压解决什么问题

大量 Pod 同时调度到节点时，若并发拉镜像会**打爆镜像仓库 + 占满节点带宽/磁盘**。kubelet 用三层背压控制拉取速率：**pull backoff**（失败退避）+ **QPS 节流**（throttleImagePulling）+ **串行/并行 puller**（控制并发度）。

### 0.2 关键结论

1. **三层背压**：pull backoff（同 Pod+image 失败退避）+ QPS 节流（throttleImagePulling 限速）+ puller 并发控制（serial/parallel）。
2. **EnsureImageExists 决策**：先查本地有无镜像 → pullPolicy 决定是否拉 → 拉前查 backoff。
3. **pull backoff**：同 `podUID_image` 键失败后指数退避（`flowcontrol.Backoff`），避免快速连续失败。
4. **parallel vs serial puller**：并行（maxParallelImagePulls 控制并发）或串行（sandbox 串行拉取）。
5. **镜像 GC**：image_gc_manager 按磁盘水位清理未使用镜像。

---

## 1. 生产视角：image pull 对外行为

### 1.1 可见结果

| 场景 | 行为 |
|---|---|
| 镜像本地已有 | 不拉（除非 pullPolicy=Always） |
| 镜像拉取失败 | pull backoff 退避，发 `BackOffPullImage` 事件 |
| 大量 Pod 同时拉 | QPS 节流限速，避免打爆仓库 |
| 磁盘满 | image GC 清理未使用镜像 |

### 1.2 可观测信号

- 事件：`Pulling`/`Pulled`/`Failed`/`BackOffPullImage`。
- 节点镜像数/磁盘占用。

---

## 2. 开发视角：imageManager 骨架

### 2.1 `NewImageManager`（image_manager.go `NewImageManager+0`，:81）

```text
NewImageManager(...)
├── imageService = throttleImagePulling(imageService, qps, burst)   NewImageManager+23 (:104)
│     ↑ ★ QPS 节流包装（限速拉取）
├── backOff = imageBackOff（flowcontrol.Backoff）                    (:111)
└── serialized（串行/并行 puller 选择）                              (:87/:95)
```

### 2.2 QPS 节流（`throttleImagePulling`，helpers.go :32）

`throttleImagePulling(imageService, qps, burst)`：用 rate limiter 包装 ImageService，限制拉取 QPS/burst——**避免并发拉取打爆仓库**。

---

## 3. 开发视角：拉取决策（`EnsureImageExists`）

### 3.1 `EnsureImageExists`（image_manager.go `m.EnsureImageExists+0`，:181）

```text
EnsureImageExists(ctx, objRef, pod, image, pullSecrets, ...)   m.EnsureImageExists+0
├── imagePullPrecheck(...)                                     m.imagePullPrecheck+0 (:120)
│     ↑ 前置检查（镜像名合法性、pullPolicy）
├── pullPolicy 判定：
│   ├── Never + 本地无 → imageNotPresentOnNeverPolicyError     m.imageNotPresentOnNeverPolicyError+0 (:173)
│   ├── Always → 总是拉
│   └── IfNotPresent → 本地无才拉
├── pullImage(...)                                             m.pullImage (:335)
│     ├── backOffKey = podUID_image                            m.pullImage+19 (:354)
│     ├── backOff.IsInBackOffSinceUpdate → 退避中，跳过         m.pullImage+20 (:355)
│     │     └── 发 BackOffPullImage 事件
│     └── 实际拉取（经 throttle 后的 imageService）
└── evalCRIPullErr(...)                                         m.evalCRIPullErr+0 (:402)
      ↑ 解析 CRI 拉取错误（区分镜像不存在/认证失败等）
```

### 3.2 pull backoff（指数退避）

`backOff`（image_manager.go :62，`flowcontrol.Backoff`）：按 `podUID_image` 键记录失败退避。
- 拉取失败 → 记入 backoff。
- 退避期内再拉 → 跳过 + 发 `BackOffPullImage` 事件。
- **目的：** 同一 Pod 的同一镜像失败后不快速连续重试（避免打爆仓库）。

---

## 4. 开发视角：parallel vs serial puller（puller.go）

### 4.1 parallel puller（`parallelImagePuller+0`，:46）

`newParallelImagePuller+0`（:51，maxParallelImagePulls 控制并发）→ `pullImage+0`（:58）：多个 Pod 的镜像**并行拉取**（受 maxParallelImagePulls 限制），适合多容器 Pod 加速启动。

### 4.2 serial puller（`serialImagePuller+0`，:97）

`newSerialImagePuller+0`（:102）→ `pullImage+0`（:118）：sandbox 的镜像**串行拉取**（一次一个），避免并发拉取打爆资源。

### 4.3 pull 记录持久化（pullmanager）

`pullmanager/image_pull_manager.go`：记录镜像拉取记录（checkpoint），用于 kubelet 重启后恢复 pull 状态。

---

## 5. 开发视角：镜像 GC（image_gc_manager.go）

### 5.1 `NewImageGCManager`（image_gc_manager.go `NewImageGCManager+0`，:192）

构造 image GC manager（按磁盘水位策略清理未使用镜像）。

### 5.2 `detectImages` / `GarbageCollect`

- `detectImages+0`（:244）：检测节点上所有镜像 + 使用情况。
- `GarbageCollect+0`（:349）：按磁盘水位清理未使用镜像（高水位触发、低水位目标）。

**GC 策略：** 磁盘用量超高水位 → 删未使用镜像直到降到低水位。保证节点磁盘不被镜像撑满。

---

## 6. 全链路时序（Pod 镜像拉取）

```text
SyncPod → startContainer → imagePuller.EnsureImageExists (image_manager.go:181)
├── imagePullPrecheck (:120) 前置检查
├── pullPolicy 判定（Always/IfNotPresent/Never）
├── pullImage (:335)
│    ├── backOffKey = podUID_image (:354)
│    ├── backOff 退避中 → 跳过 + BackOffPullImage 事件 (:355)
│    └── 实际拉取（经 throttleImagePulling 限速，helpers.go:32）
│         └── parallel/serial puller 并发控制 (puller.go)
└── evalCRIPullErr (:402) 解析错误
     └── 镜像就绪 → 创建容器
（磁盘满 → image_gc_manager.GarbageCollect :349 清理未使用镜像）
```

---

## 7. 关键保证汇总

| 保证 | 锚点 | 为什么必须保持 |
|---|---|---|
| QPS 节流 | `throttleImagePulling`（helpers.go:32） | 并发拉取不打爆镜像仓库 |
| pull backoff | `m.pullImage+20`（flowcontrol.Backoff） | 失败不快速连续重试 |
| 并发控制 | parallel/serial puller（puller.go:46/:97） | 控制拉取并发度 |
| 磁盘保护 | `GarbageCollect+0`（image_gc_manager.go:349） | 镜像不撑满节点磁盘 |

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- image_manager.go：`NewImageManager+0`（:81）、`throttleImagePulling`（NewImageManager+23/:104，helpers.go:32）、`m.EnsureImageExists+0`（:181）、`m.imagePullPrecheck+0`（:120）、`m.imageNotPresentOnNeverPolicyError+0`（:173）、`m.pullImage`（:335）、backOff（:62/:354/:355）、`m.evalCRIPullErr+0`（:402）
- puller.go：`parallelImagePuller+0`（:46）、`newParallelImagePuller+0`（:51）、`serialImagePuller+0`（:97）、`newSerialImagePuller+0`（:102）
- image_gc_manager.go：`NewImageGCManager+0`（:192）、`detectImages+0`（:244）、`GarbageCollect+0`（:349）

### 7.2 生产实测

**未实测** `[待运行验证]`。QPS 节流实际限速、pull backoff 退避间隔、GC 触发水位均为代码推断，未在真实节点压测观测。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| credential 解析 | 未深挖 | `makeLookupPullCredentialsFunc`（:302）/imagePullSecrets 细节未读 |
| evalCRIPullErr | 未深挖 | CRI 拉取错误分类（不存在/认证/限流）未逐行读 |
| pullmanager checkpoint | 未深挖 | 拉取记录持久化格式未读 |
| maxParallelImagePulls | 未深挖 | 并发度默认值与调优未展开 |

### 7.4 修改联动

- 改背压 → `m.pullImage`/`backOff`，同步本文 §3.2。
- 改 QPS 节流 → `throttleImagePulling`，同步 §2.2。
- 改并发 → parallel/serial puller，同步 §4。
- 改 GC → `GarbageCollect`，同步 §5。

**关联文档：** [10-kubelet-CRI运行时](../10-kubelet-CRI运行时.md)（imagePuller 浓缩）、[kubelet同步循环与PLEG](kubelet同步循环与PLEG.md)（SyncPod）、[kubelet-volume-manager](kubelet-volume-manager.md)（卷挂载）。

[总目录](../README.md)
