# 源码文档库按最新代码同步（adrian @ 6bf0bfd51d7）

## 一句话结论
docs/sourceReader 源码文档库从旧基准 `88939239381`(11-18) 同步到 `6bf0bfd51d7`(11-26)：全库源码引用失效从 **111 处降到 9 处（-92%）**，剩余 9 处经逐条 grep 确认**全部是校验工具 `sr_tools.py` 的固有局限、非文档错误**。重写 13 篇 + 定点修复 13 篇 + 新增 1 篇，共 28 个 commit。

## 环境
- 分支 adrian，工作区干净（仅 `.codegraph/` 未跟踪）。Go 1.27.4（`.go-version`）。
- 文档库：`docs/sourceReader/*.md` 23 篇 + `docs/topics/*.md` 32 篇 + `evidence/*.md` 2 篇。
- 工具：`docs/sourceReader/.progress/sr_tools.py`（5 子命令，本次大幅修复）；MCP codegraph（~20658 文件已索引）。

## 操作前状态
- 文档旧基准 `88939239381` → HEAD 差 **168 提交 / 323 个非测试代码文件**。
- 被文档引用的 159 个 `.go` 文件中 22 个被改动（无删除）。
- 全库源码引用 **2147 处**，其中 ✗ 疑似失效 **111 处**、裸名歧义 620 处。
- 主要失效形态：`.go:行号` 绝对行号引用（脆弱，随代码漂移）。

## 前置条件与确认结果
- 用户拍板：深度=骨架重写并逐篇修补；PodCertsSigner 新增独立篇目。
- 用户已加 codegraph 索引（探查效率关键）。
- 交付纪律：commit/push 由用户手动执行（本次用户明确授权过 commit&push，后续 commit 为延续同一授权）。

## 脚本清单
| 工具/脚本 | 用途 | 结果 |
|---|---|---|
| `sr_tools.py offset` | 取符号定义行+体末 | 修 3 bug 后可用 |
| `sr_tools.py verify` | 全库引用校验，分 ✗/?/·/✓ 四档 | 终验 ✗=9 |
| `sr_tools.py convert` | `.go:行号`→符号+偏移 | 辅助 |
| `sr_tools.py selftest` | 10 项回归 | 全过 |
| codegraph_explore | 符号定义行权威取证 | 全程使用 |

## 执行命令原文（可复跑）
```bash
# 全库校验
ALL=$(git ls-files -z 'docs/sourceReader/*.md' 'docs/topics/*.md' 'docs/sourceReader/evidence/*.md' | tr '\0' '\n')
python3 docs/sourceReader/.progress/sr_tools.py verify $ALL
# 单篇偏移
python3 docs/sourceReader/.progress/sr_tools.py offset <file.go> <Symbol>
# 引用残留自测
grep -ohE '[A-Za-z0-9_./-]+\.go:[0-9]+' <doc.md> | wc -l   # 应为 0
```

## 终态数据
- 全库 ✗：**111 → 9**。? 裸名歧义 595（工具按 base-name 报，非失效）。✓ 1530。
- 提交：28 个 docs commit（`27ccd1ec`…`f60dd8a3`）。
- 已同步篇目（引用清零 + 基准更新到 6bf0bfd51d7）：
  重写 13 篇 = kubelet探针机制、调度器抢占、APF、etcd3写入路径、主链路03、leader-election、workqueue、DRA、kube-proxy规则生成、api-server全景、admission-webhook、19-kubeadm、04-kubectl、apiserver-discovery；
  定点修复 = aggregator、CRD建立与懒分发、09/14/16/20/21 篇、07-scheduler、设备插件、evidence 两篇记法；
  新增 = podcertssigner服务签名（独立篇）。

## 本次独有发现
1. **失效主力在 topics**：386 处 `.go:行号` 引用里 356 处在 topics 32 篇，顶层 23 篇仅 30 处——骨架重写火力应集中 topics。
2. **`go_anchor` 按 receiver 变量名索引方法**：`func (d *storeTxnDelegator) DeleteOnce` 索引成 `d.DeleteOnce`，按类型名/裸名都查不到。etcd3 篇锚点须写成 `d.DeleteOnce`/`s.UpdateOnce`。
3. **`go_anchor` 不索引 const 块成员**：K8s 的 feature gate / 事件 / API 常量几乎全在 `const (` 块里 → `sr_tools` 全部 MISS。
4. **`body_end` 空复合字面量 bug**：签名含 `struct{}`/`interface{}` 时花括号深度提前归零，体末误判为定义行自身（`podWorkerLoop`、`r.Body` 两处独立发现）。
5. 两个子代理从不同篇目独立收敛到同一 `body_end` 根因——并行子代理的交叉验证价值。

## 幂等性验证
- `verify` 可重复跑，结果稳定（✗=9）。
- `offset` 对同一符号返回稳定行号。
- selftest 10 项确定性通过。

## 可复用复核语句
```bash
# 复核终态：全库 ✗ 应为 9（均为工具局限，见下）
python3 docs/sourceReader/.progress/sr_tools.py verify $(git ls-files 'docs/sourceReader/*.md' 'docs/topics/*.md' 'docs/sourceReader/evidence/*.md')
# 复核任一篇无绝对行号引用
grep -ohE '[A-Za-z0-9_./-]+\.go:[0-9]+' <doc.md> | wc -l   # 0
```

## 遗留与后续
- 剩余 9 处 ✗ 全为 `sr_tools` 局限（符号均 grep 确认存在）：Go 泛型 `[T]`（3）、receiver 变量名索引（2）、函数内局部闭包（2）、多 receiver 同名（1）、适配器内方法（1）。文档锚点正确，已 codegraph 回核。若要清零需再改工具（泛型感知 + 闭包变量索引），收益低、风险高，未做。
- `?` 595 处裸名歧义为工具按 base-name 报告，非失效（同名符号跨文件），锚点已带路径或 receiver 消歧。
- 生产运行期行为（DRA 抢占、APF 过载、leader 切换、退避节奏等）均标 `[待运行验证]`，未在真实集群实测。
- `sr_tools.py` 在 `.progress/`（gitignore），其修复未进版本库——台账即记录。
