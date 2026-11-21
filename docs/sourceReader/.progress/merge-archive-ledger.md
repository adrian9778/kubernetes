# 旧库合并归档台账（sourceReader1 + sourceReader2 → sourceReader）

> 日期：2026-11-21。操作人：lead（AI）。
> 结论：`docs/adrian/sourceReader1`、`docs/adrian/sourceReader2` 两份旧库中，有价值且主库缺失的主题已按 source-doc-refresh 规范重写进 `docs/sourceReader/topics/`；其余主题被主库既有篇目覆盖。两个旧目录归档后删除，最终只保留 `docs/sourceReader`。

## 1. 操作前状态

| 目录 | 文件数 | 基准 | 锚点风格 |
|---|---|---|---|
| `docs/sourceReader`（主库，保留） | 22 主线 + 26 topics + README/00 | master@`57d78241ff96c94130c509681b12b4a1715434af`（2026-11-20） | 符号+偏移（`Symbol+N`），零 `#L` 行号 |
| `docs/adrian/sourceReader1`（旧，删除） | 6（README + 01~05） | 2026-08-13 | 裸文件名，几乎无锚点 |
| `docs/adrian/sourceReader2`（旧，删除） | 42（README + 01~20 多系列） | commit `b882c60b402`（非当前 HEAD） | GitHub `#L` 绝对行号 |

旧库问题：均基于不同 commit、锚点不合规（`#L` 行号会随代码漂移失效），**不能直接搬运**，必须回当前源码重写为符号+偏移。

## 2. 覆盖判定与处置

### 2.1 被主库覆盖（不重复搬运，随目录删除）

| 旧库主题 | 主库对应篇目 | 处置 |
|---|---|---|
| sr1 01-架构总览 | `01-系统骨架.md` | 已覆盖（进程/组件/数据流） |
| sr1 02-核心组件结构 | `01`、`05`、`06`、`08`、`09`、`16` 等 | 已覆盖（各组件函数级） |
| sr1 03-API设计与实现 | `06-apiserver请求处理与etcd.md` + topics（`api-server`、`etcd3写入路径`、`优先级与公平性流控`、`admission-webhook`、`apiserver-discovery与OpenAPI`） | 已覆盖 |
| sr1 04-控制器框架 | `08-controller副本控制.md` + `11-informer与workqueue机制.md` + topics（`replicaset副本控制`、`workqueue机制`、`informer-resync机制`、`leader-election机制`） | 已覆盖 |
| sr1 05-调度器架构 | `07-scheduler调度与绑定.md` + topics（`scheduler过滤打分框架`、`调度器抢占-preemption`） | 已覆盖 |
| sr2 kubelet 系列（01~04） | `09-kubelet同步循环.md`、`10-kubelet-CRI运行时.md` + topics（`kubelet同步循环与PLEG`、`kubelet探针机制`、`kubelet-volume-manager`、`kubelet-image-pull背压`） | 已覆盖 |
| sr2 kube-proxy 系列（05~06） | `16-kube-proxy网络代理.md` + topics（`kube-proxy规则生成`、`conntrack与ServiceCIDR`） | 已覆盖 |
| sr2 20 章概览系列（01~20 中的概览/重建手册/索引） | 主线 01~21 + README 学习路径 | 已覆盖（概览/术语/学习验收属导航性内容） |

### 2.2 主库缺失、已重写新增（topics/）

| 新专题 | 源自旧库 | 证据 | 锚点校验 |
|---|---|---|---|
| `topics/v1.37-PodGroup与组合调度.md` | sr2 `11-v1.37-Workload-PodGroup与DRA.md`（组调度部分） | `pkg/scheduler/schedule_one_podgroup.go`、`algorithm_podgroup.go`、`framework/types.go`、`pkg/features/kube_features.go` | 19 OK / 0 MISS（2 特性门控用 `Const:`） |
| `topics/DRA动态资源分配.md` | sr2 `11-v1.37-Workload-PodGroup与DRA.md`（DRA 部分）、`13-Volume-CSI与资源管理.md`（DRA 侧） | `pkg/scheduler/framework/plugins/dynamicresources/`、`pkg/controller/resourceclaim/`、`pkg/controller/devicetainteviction/`、`pkg/kubelet/cm/dra/`、`staging/src/k8s.io/api/resource/v1beta1/` | 31 OK / 0 MISS |
| `topics/测试与质量体系.md` | sr2 `01-测试体系总图与选型.md`、`02-单元测试`、`03-integration与etcd-testserver`、`04-e2e-node-perf-soak`、`16-测试体系与质量策略.md` | `staging/src/k8s.io/client-go/{kubernetes/fake,testing}/`、`cmd/kube-apiserver/app/testing/`、`test/integration/framework/`、`test/e2e/` | 16 OK / 0 MISS |
| `topics/构建发布与代码生成.md` | sr2 `05-update-verify-codegen.md`、`07-build-release-cluster-vendor-staging.md`、`17-代码生成验证构建与发布.md` | `Makefile`、`hack/update-codegen.sh`、`hack/verify-all.sh`、`build/release*.sh`、`.go-version` | 稳定名定位（脚本/目标，非 Go 符号） |

「无需单列」理由：sr2 的 apimachinery 序列化（`05-apimachinery与序列化.md`）、CRD 聚合（`08-CRD聚合API与扩展机制.md`）等已被主库 topics（`etcd3写入路径`、`apiserver-discovery与OpenAPI`、`CRD建立与懒分发`、`CRD-conversion-webhook`、`aggregator代理转发`）覆盖；sr2 的「Mini-Kubernetes 重建手册」属学习路线导航，非当前源码能力，主库 README 学习路径已承载。

## 3. 执行命令原文

```bash
# 锚点核验（每篇）
cd docs/sourceReader
python3 .progress/verify_anchors.py "topics/<新专题>.md" <相关 .go 文件...>
# 链接/行号检查
grep -c '#L[0-9]' topics/<新专题>.md        # 期望 0
# 归档删除（git 跟踪，可由历史恢复）
git rm -r docs/adrian/sourceReader1 docs/adrian/sourceReader2
```

## 4. 终态

- 新增 4 篇 topics，全部登记进 `docs/sourceReader/README.md` 专题表。
- 删除 `docs/adrian/sourceReader1`、`docs/adrian/sourceReader2`（含 `docs/adrian` 空目录）。
- 仅保留 `docs/sourceReader`。
- 新增专题零 `#L` 绝对行号，符号+偏移锚点经 `verify_anchors.py` 反查通过。

## 5. 遗留与后续

- 主库既有 22 主线篇与 26 旧 topics 的锚点有效性未在本次全量 sweep（本次只保证新增 4 篇合规）；如需全库 `verify` 可后续单独跑。
- `GenericWorkload`/`CompositePodGroup`/resource API 版本的默认启用状态标为 `[待运行验证]`，以 `pkg/features/` 与 `staging/.../api/resource/` 当前值为准。
