[上一篇](13-job与cronjob控制器.md) · [总目录](README.md) · [下一篇](15-garbage-collector垃圾回收.md)

# DaemonSet 控制器
> **层级**：第四层——函数级源码解析。
> **范围**：DaemonSet 如何把 Pod 与节点一一对应、节点准入判定、节点亲和性注入、surge 滚动更新。

## 1. 模块级
| 项 | 内容 |
|---|---|
| 目录 | `pkg/controller/daemon/` |
| 主文件 | `daemon_controller.go`（DaemonSetsController、syncDaemonSet、manage、syncNodes）、`update.go`（滚动/历史）、`util/daemonset_util.go` |
| 依赖 | DS/Node/Pod/ControllerRevision 四类 informer、queue + nodeUpdateQueue、expectations、failedPodsBackoff、consistencyStore |
| 数据流 | DS/Node/Pod 事件 → queue → syncDaemonSet → manage：逐节点比对期望与现存 Pod → syncNodes 增删 → 写 status |

## 2. 类型级
| 类型 | 文件 | 职责 |
|---|---|---|
| `DaemonSetsController` | `daemon_controller.go:103` | 持有 podControl、crControl、ds/node/pod/history Lister、podIndexer（按 node 名与 ControllerRef UID 双索引）、burstReplicas、failedPodsBackoff |
| `history` | update.go | 当前/历史 ControllerRevision 的包装 |

## 3. 运行函数级

### 函数：NewDaemonSetsController
**源码：** `daemon_controller.go:158`
**职责：** 注册四类 informer handler（Node 事件进 nodeUpdateQueue，其余进主 queue），赋值 syncHandler=syncDaemonSet。

### 函数：Run
**源码：** `daemon_controller.go:359`
**执行流程：** WaitForNamedCacheSync 后启动主 worker（runWorker:395/processNextWorkItem:401）、额外启动 `runNodeUpdateWorker`（节点事件独立消费）与 backoff GC。

### 函数：syncDaemonSet
**源码：** `daemon_controller.go:1281`
**执行流程：**
```text
syncDaemonSet (+0)
├── 1. consistencyStore.EnsureReady (+20)
├── 2. dsLister.Get (+32)
├── 3. nodeLister.List (+44)
├── 4. constructHistory（update.go:299，cur/old ControllerRevision）(+75)
├── 5. expectations 未满足 → 跳过 manage 只更新 status (+81)
├── 6. updateDaemonSet（:973）(+87)
│      ├─ manage（:1003）
│      ├─ rollingUpdate（update.go:46）
│      └─ cleanupHistory（update.go:356）
└── 7. updateDaemonSetStatus（:1211）(+88)
```

### 函数：updateDaemonSet
**源码：** `daemon_controller.go:973`
**职责：** 期望侧变更：manage 求节点-Pod 差集，rollingUpdate 处理滚动节奏，cleanupHistory 按 revisionHistoryLimit 删旧 ControllerRevision。

## 4. 节点-Pod 差集函数级

### 函数：manage
**源码：** `daemon_controller.go:1003`
**职责：** 逐节点比对，产出待创建节点与待删除 Pod。
**执行流程：**
1. `getNodesToDaemonPods`（:796；内部 getDaemonPods:764，按 `util.GetTargetNodeName` 分组）（+2）
2. 预备 daemonPodTolerations（:1400）与 requiredNodeAffinity（+13 ～ +14）
3. 逐节点调 podsShouldBeOnNode（:849）（+16 ～ +20）
4. getUnscheduledPodsWithoutNode（:1488）清理指向已消失节点的未调度 Pod（+26）
5. 结果交 syncNodes（+30）

### 函数：podsShouldBeOnNode
**源码：** `daemon_controller.go:849`
**职责：** 单节点上的全部判定，返回节点名（需创建）与 Pod 名（需删除）。
**前置：** `nodeShouldRunDaemonPod` 先算两个布尔：shouldRun（是否该有 Pod）、shouldContinueRunning（现存 Pod 是否该继续）。
**分支：**
```text
podsShouldBeOnNode
├── shouldRun && !exists            → nodesNeedingDaemonPods（+12 ～ +14）
├── shouldContinueRunning:
│   ├── Failed Pod → backoff 后加入 podsToDelete（+22 ～ +46）
│   ├── Succeeded Pod → podsToDelete（+47 ～ +54）
│   ├── 非 surge：running Pod 超 1 个，按创建时间只留最旧（+58 ～ +68）
│   └── surge：按 ControllerRevisionHash 区分新旧 Pod:
│        旧 Pod 不 ready 或新 Pod 已 Available → 删旧 Pod（+81 ～ end）
└── !shouldContinueRunning && exists → 该节点全部 Pod 入 podsToDelete（+111 ～ +118）
```
**热循环防护（:874 ～ +902）：** kubelet 拒绝 DS Pod（如资源不足）时 Failed Pod 会反复出现，failedPodsBackoff 按 node 维度指数退避，退避期间 enqueueDaemonSetAfter 延迟重试。

### 函数：nodeShouldRunDaemonPod / predicates
**源码：** `daemon_controller.go:1413 / :1439`
**职责：** 节点准入：taint 是否被 toleration 容忍、required node affinity 是否匹配；导出包装 NodeShouldRunDaemonPod（:1392）。

## 5. 创建/删除执行函数级

### 函数：syncNodes
**源码：** `daemon_controller.go:1040`
**执行流程：**
1. SetExpectations（+18）
2. `util.CreatePodTemplate`（:1071，加 hash/generation label）（+31）
3. **节点亲和性注入**（:1091）：每个待创建 Pod 调 `ReplaceDaemonSetPodNodeNameNodeAffinity`（util:176），写入 `kubernetes.io/hostname` 的 RequiredDuringScheduling 节点选择条件（+51）
4. `podControl.CreatePods`（:1095）
5. `podControl.DeletePod`（:1130）
6. slow-start 分批，错误聚合

### 节点亲和性相关（util/daemonset_util.go）
| 函数 | 行 | 职责 |
|---|---|---|
| `ReplaceDaemonSetPodNodeNameNodeAffinity` | 176 | 构造指向指定节点 hostname 的 NodeSelectorTerm |
| `GetTargetNodeName` | 226 | 从 Pod 的 node affinity 反取节点名（NodeName 为空时） |
| `AddOrUpdateDaemonPodTolerations` | 48 | 注入 not-ready/unreachable 的 NoExecute toleration，保证 DS Pod 能在节点故障时保留 |
| `CreatePodTemplate` | 107 | Pod 模板补 ControllerRevisionHash label |
| `AllowsSurge/SurgeCount/UnavailableCount` | 126/134/153 | 更新策略边界解析 |
| `IsPodUpdated` | 165 | Pod template hash 是否匹配当前 revision |

**调度模型（源码实证）：** DaemonSet 不设置自定义 schedulerName（全包 grep SchedulerName 零命中）；Pod 经节点亲和性绑定目标节点，后续过滤与默认调度由默认 scheduler 完成。文件内另有 NewPod（:1450，直接设 spec.nodeName）的旧路径，主 sync 流程未使用。

## 6. 状态函数级

### 函数：updateDaemonSetStatus
**源码：** `daemon_controller.go:1211`
**职责：** 状态计算与写回（无独立 calculateStatus 符号，计算内联）。
**内部步骤：** 逐节点用 nodeShouldRunDaemonPod 的结果统计 DesiredNumberScheduled、CurrentNumberScheduled、NumberMisscheduled、NumberReady、NumberAvailable、UpdatedNumberScheduled（:1259 得 numberUnavailable=desired-available），随后 storeDaemonSetStatus（:1155）写回。

## 7. 调用关系总览
```text
syncDaemonSet
 ├── constructHistory
 ├── updateDaemonSet
 │    ├── manage
 │ │    ├── getNodesToDaemonPods
 │ │    ├── podsShouldBeOnNode → nodeShouldRunDaemonPod
 │ │    └── getUnscheduledPodsWithoutNode
 │    ├── syncNodes
 │ │    ├── CreatePodTemplate
 │ │    ├── ReplaceDaemonSetPodNodeNameNodeAffinity
 │ │    ├── podControl.CreatePods / DeletePod
 │ │    └── rollingUpdate / cleanupHistory
 └── updateDaemonSetStatus → storeDaemonSetStatus
```

## 8. 设计意图（源码可证）
- DS 的调谐单位是"节点 × Pod"，每节点独立判定，节点增删即自动增删 Pod。
- Pod 经亲和性进默认调度器，使资源请求、污点、优先级等通用调度语义对 DS 同样生效。
- surge 更新允许新旧两个 Pod 短暂共存，切换条件（旧不 ready 或新已 available）在 podsShouldBeOnNode 集中判定。

[上一篇](13-job与cronjob控制器.md) · [总目录](README.md) · [下一篇](15-garbage-collector垃圾回收.md)
