[上一篇](../sourceReader/21-设备插件与网络边界.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# Deployment 滚动更新

> **层级**：跨篇机制纵深专题——把 Deployment 滚动更新的**先扩后缩、可用性预算、比例伸缩、回滚**讲透：`syncDeployment` 策略分派、`rolloutRolling` 调谐新/旧 RS、`maxSurge`/`maxUnavailable` 计算、`GetReplicaSetProportion` 比例分配。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `dc.rolloutRolling+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[12-deployment控制器](../sourceReader/12-deployment控制器.md) §6 是浓缩版；本专题纵向展开滚动更新的副本数计算与新旧 RS 调谐语义。

---

## 0. 结论速览

### 0.1 滚动更新解决什么问题

Deployment 更新 Pod 模板时，要**平滑地**把旧版本 Pod 换成新版本，且**全程满足可用性要求**（不因更新导致可用副本跌破预算）。核心是两个旋钮：
- **`maxSurge`**：更新过程中允许**超出**期望副本数多少个（先扩的空间）。
- **`maxUnavailable`**：更新过程中允许**不可用**多少个（可缩的预算）。

### 0.2 关键结论

1. **先扩后缩**：滚动更新先按 `maxSurge` 扩新 RS，再按 `maxUnavailable` 缩旧 RS，逐步推进直到新 RS 达到期望副本、旧 RS 归零。
2. **新旧 RS 靠 `pod-template-hash` 区分**：每次模板变化生成新 hash → 新 RS；旧 RS 保留做回滚。
3. **可用性预算是绝对数**：`MaxSurge`/`MaxUnavailable` 把百分比换算成绝对副本数（`ResolveFenceposts` 至少保证 1）。
4. **比例伸缩**：`kubectl scale` 不推进 rollout，而是按各活跃 RS 当前规模**比例**分配增减（`GetReplicaSetProportion`）。
5. **回滚 = 模板回退**：回滚本质是把模板改回旧 revision，触发新一轮滚动（非特殊路径）。

---

## 1. 生产视角：一次滚动更新可见什么

### 1.1 用户操作与可见结果

- `kubectl set image` / `kubectl apply` 改 Pod 模板 → 触发滚动。
- `kubectl rollout status` 观察 `Progressing` 条件；`kubectl rollout undo` 回滚。
- 过程中 ReplicaSet 数量变化：新 RS 从 0 扩到 N，旧 RS 从 N 缩到 0。

### 1.2 可观测信号

- Deployment `status.conditions`：`Progressing`（原因 `NewReplicaSet`/`ReplicaSetUpdated`/`NewRSAvailable`）、`Available`。
- 各 RS 的 `spec.replicas` 逐步变化。

---

## 2. 开发视角：策略分派（`syncDeployment`）

### 2.1 入口 `syncDeployment`（deployment_controller.go `dc.syncDeployment+0`，:596）

```text
syncDeployment(key)
├── 取 Deployment + getAllReplicaSetsAndSyncRevision
├── 分派策略：
│   ├── Paused → sync（sync.go，纯 scale + status）
│   ├── Recreate → rolloutRecreate（recreate.go，先全缩旧再建新）
│   └── RollingUpdate（默认）→ rolloutRolling（rolling.go dc.rolloutRolling+0，:31）
```

---

## 3. 开发视角：滚动更新核心（`rolloutRolling`）

### 3.1 `rolloutRolling`（rolling.go `dc.rolloutRolling+0`，:31）

```text
rolloutRolling(ctx, d, rsList)
└── reconcileRollingReplicaSets(ctx, d, allRSs, oldRSs, newRS)   dc.reconcileRollingReplicaSets+0 (:42)
     ├── reconcileNewReplicaSet(...)                             dc.reconcileNewReplicaSet+0 (:62)
     │     ↑ 先扩新 RS（受 maxSurge 约束）
     ├── reconcileOldReplicaSets(...)                            dc.reconcileOldReplicaSets+0 (:80)
     │     ↑ 再缩旧 RS（受 maxUnavailable 约束）
     └── syncDeploymentStatus
```

**先扩后缩顺序（关键）：** `reconcileNewReplicaSet` 在 `reconcileOldReplicaSets` **之前**——先保证新版本有容量（surge），再减旧版本（unavailable 预算）。

### 3.2 先扩新 RS（`reconcileNewReplicaSet`，rolling.go :62）

- 用 `NewRSNewReplicas`（util :820）算新 RS 目标副本数（受 maxSurge 上限）。
- 新 RS 未就绪时不继续缩旧 RS（等新 Pod Ready 才腾出 unavailable 配额）。

### 3.3 再缩旧 RS（`reconcileOldReplicaSets`，rolling.go :80）

```text
reconcileOldReplicaSets(ctx, allRSs, oldRSs, newRS, d)
├── scaleDownOldReplicaSetsForRollingUpdate(...)   dc.scaleDownOldReplicaSetsForRollingUpdate+0 (:187)
│     ↑ 按 maxUnavailable 可用性预算算本轮能缩多少旧 Pod
└── cleanupUnhealthyReplicas(...)                  dc.cleanupUnhealthyReplicas+0 (:149)
      ↑ 清理不健康（超时未 Ready）的旧 RS
```

---

## 4. 开发视角：副本数计算（util/deployment_util.go）

### 4.1 两个预算旋钮

| 函数 | 锚点 | 作用 |
|---|---|---|
| `MaxSurge` | `MaxSurge+0` (:470) | 把 `maxSurge`（百分比或绝对数）换算为绝对副本数 |
| `MaxUnavailable` | `MaxUnavailable+0` (:449) | 把 `maxUnavailable` 换算为绝对不可用数 |
| `ResolveFenceposts` | `ResolveFenceposts+0` (:886) | 两者都为 0 时兜底至少 1（避免死锁） |

### 4.2 新 RS 目标副本数（`NewRSNewReplicas`，util :820）

`NewRSNewReplicas+0`（:820）：`当前新RS副本 + maxSurge`（不超过期望总数 + surge）。这是「先扩」的上限。

### 4.3 比例伸缩（`GetReplicaSetProportion`，util :482）

`kubectl scale` 触发 `isScalingEvent`（sync.go `dc.isScalingEvent+0`，:551）→ `scale`（sync.go `dc.scale+0`，:320）→ `GetReplicaSetProportion+0`（:482）：按各活跃 RS 当前规模**比例**分配新增/减少的副本，而非推进 rollout。

**比例分配语义：** 若 Deployment 扩到 10 副本，旧 RS A 有 3、旧 RS B 有 2、新 RS 有 1（共 6），则新增 4 个按 3:2:1 比例分给 A/B/新。

### 4.4 新旧 RS 区分（`pod-template-hash`）

- `EqualIgnoreHash`（util `EqualIgnoreHash+0`，:619）：比较模板时忽略 hash label。
- `SetNewReplicaSetAnnotations`（util :232）：给新 RS 打 revision annotation。

---

## 5. 全链路时序（一次滚动更新）

```text
Pod 模板变更 → Deployment 入队 → syncDeployment (deployment_controller.go:596)
└── RollingUpdate → rolloutRolling (rolling.go:31)
     └── reconcileRollingReplicaSets (:42)
          ├── reconcileNewReplicaSet (:62)
          │    └── NewRSNewReplicas (util:820) 受 MaxSurge (util:470) 约束 → 扩新 RS
          ├── reconcileOldReplicaSets (:80)
          │    ├── scaleDownOldReplicaSetsForRollingUpdate (:187) 受 MaxUnavailable (util:449) 约束 → 缩旧 RS
          │    └── cleanupUnhealthyReplicas (:149) 清不健康旧 RS
          └── 循环直到新 RS = 期望、旧 RS = 0 → syncDeploymentStatus（Progressing=Complete）
```

---

## 6. 验证与边界

### 6.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `dc.syncDeployment+0`（deployment_controller.go:596）
- `dc.rolloutRolling+0`（rolling.go:31）、`dc.reconcileRollingReplicaSets+0`（:42）、`dc.reconcileNewReplicaSet+0`（:62）、`dc.reconcileOldReplicaSets+0`（:80）、`dc.cleanupUnhealthyReplicas+0`（:149）、`dc.scaleDownOldReplicaSetsForRollingUpdate+0`（:187）
- `MaxSurge+0`（util:470）、`MaxUnavailable+0`（:449）、`ResolveFenceposts+0`（:886）、`NewRSNewReplicas+0`（:820）、`GetReplicaSetProportion+0`（:482）、`EqualIgnoreHash+0`（:619）、`SetNewReplicaSetAnnotations+0`（:232）
- `dc.scale+0`（sync.go:320）、`dc.isScalingEvent+0`（:551）

### 6.2 生产实测

**未实测** `[待运行验证]`。滚动节奏（每轮扩缩几个）、比例分配实际值、可用性预算边界行为均为代码推断，未在真实集群观测滚动过程的 RS 副本数序列。

### 6.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| 进度/超时判定 | 未深挖 | `progress.go` 的 ProgressDeadlineSeconds 超时判定未逐行读 |
| 回滚细节 | 未深挖 | `rollback.go` 的 revision 回退细节未展开 |
| surge/unavailable 百分比换算 | 未深挖 | 百分比 round up/down 精确规则未读 |

### 6.4 修改联动

- 改预算计算 → `MaxSurge`/`MaxUnavailable`/`ResolveFenceposts`，同步本文 §4.1。
- 改比例分配 → `GetReplicaSetProportion`，同步 §4.3。
- 改先扩后缩顺序 → `reconcileRollingReplicaSets`，同步 §3.1。

**关联文档：** [12-deployment控制器](../sourceReader/12-deployment控制器.md)（Deployment 全貌）、[08-controller副本控制](../sourceReader/08-controller副本控制.md)（ReplicaSet 侧）、[workqueue机制](workqueue机制.md)（控制器队列）。

[总目录](../sourceReader/README.md)
