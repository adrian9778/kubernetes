# E2E、E2E Node、性能与 Soak

## 1. 集群 E2E 入口

关键文件：

- `test/e2e/e2e.go`
- `test/e2e/framework/framework.go`
- `test/e2e/framework/test_context.go`
- `hack/ginkgo-e2e.sh`
- `cluster/kube-util.sh`
- `cluster/log-dump/log-dump.sh`

执行链：

```text
先构建 ginkgo、kubectl、e2e.test
  -> hack/ginkgo-e2e.sh
  -> 找到二进制
  -> 加载 provider util / prepare-e2e
  -> 从 kubeconfig 探测 API Server
  -> 组装 Ginkgo 参数
  -> 执行 ginkgo e2e.test -- suite args
  -> framework 创建每个 spec 的 namespace/client
  -> 执行测试
  -> dump artifact / JUnit / Ginkgo JSON
  -> framework 清理 namespace 和资源
```

`hack/ginkgo-e2e.sh` 的重要参数：

- `GINKGO_PARALLEL=y` 开启并行；race 模式默认节点数更低。
- `GINKGO_PARALLEL_NODES` 精确控制 worker 数。
- `GINKGO_TIMEOUT` 默认被设回 24h，避免 Ginkgo v2 的 1h 默认值改变历史行为。
- `KUBE_E2E_TEST_ARGS` 传 suite 参数。
- `E2E_REPORT_DIR` 与 `E2E_REPORT_PREFIX` 控制报告。
- `E2E_TEST_DEBUG_TOOL=delve|gdb` 可调试，但二进制必须 `DBG=1` 构建。
- CI 下可在 SIGTERM 时转发信号，要求 Ginkgo 输出最终进度。

## 2. Framework 生命周期

每个 spec 通常通过 `framework.NewDefaultFramework` 获得：

- namespace 生命周期。
- typed、dynamic、discovery client。
- test context 中的 provider、镜像、超时配置。
- failure dump 和清理 hook。

测试应尽量通过公开 API 驱动，不应导入控制面私有实现来“证明” e2e 行为。

并行 E2E 的资源隔离依赖唯一 namespace/name。集群级资源不能只靠 namespace 隔离，必须加唯一前缀并注册 cleanup。

## 3. Conformance 与标签

Kubernetes 的 e2e 不只是 Ginkgo focus/skip 字符串。测试可能有 SIG、Feature、Serial、Slow、Disruptive、Conformance 等标签。新增或修改 conformance 测试还会触发 ownership、conformance requirement 和 YAML 生成/校验。

选择测试时优先使用标签表达意图，避免用脆弱的测试标题正则长期维护 CI。

## 4. E2E Node

关键位置：

- `test/e2e_node/e2e_node_suite_test.go`
- `test/e2e_node/framework.go`
- `test/e2e_node/services`
- `test/e2e_node/remote`
- `test/e2e_node/runner`
- `test/e2e_node/criproxy`
- `test/e2e_node/testing-manifests`

本地模式典型链路：

```text
构建 e2e_node.test、ginkgo、kubelet 等
  -> 准备 root/CRI/cgroup 环境
  -> 启动必要的 API Server 或内部服务
  -> 启动/连接 kubelet
  -> 预拉镜像
  -> Ginkgo 运行 node specs
  -> 收集 kubelet/runtime/system 日志
  -> 停止服务并恢复主机状态
```

远端模式由 `test/e2e_node/remote` 和 runner 通过 SSH/GCE 等方式部署二进制、执行测试、取回 artifacts。

### CRI proxy

`test/e2e_node/criproxy` 位于 kubelet 和真实 runtime 之间，可观察或修改 CRI 调用。适合验证：

- Pod sandbox/container 创建调用序列。
- runtime 返回特定错误时 kubelet 状态。
- resize、condition、重启和失败恢复。

状态链：

```text
kubelet -> CRI proxy -> runtime
            | 记录请求/响应
            | 按规则阻断、改写或注入失败
test <- 观察 Pod status、event、metrics
```

## 5. 性能测试

### Scheduler perf

`test/integration/scheduler_perf` 使用 integration 控制面和配置化 workload，测量调度吞吐与延迟。关键不是“跑很多 Pod”，而是固定：

- 节点与 Pod 分布。
- 插件/profile 配置。
- churn 和并发模型。
- metric 采样窗口。
- warm-up 与正式测量边界。

性能回归验收必须比较相同环境和配置，单次 wall clock 没有统计意义。

### E2E/node perf

`test/e2e/framework/perf` 提供集群性能测量辅助；`test/e2e_node/perf/workloads` 定义 NPB、PyTorch 等节点 workload。节点 perf 更关注资源隔离、NUMA、CPU/内存管理和 runtime 开销。

## 6. Soak

`test/soak/serve_hostnames` 是长期运行服务样例。Soak 的目标不是发现单次功能失败，而是：

- 内存/goroutine/FD 是否持续增长。
- watch/reconnect 是否长期稳定。
- controller queue 是否积压。
- 节点重启、网络抖动后是否恢复。
- 长时间 churn 下尾延迟是否恶化。

Soak 必须定义时长、负载、故障注入、采样周期和失败阈值，否则只是“运行很久”。

## 7. 失败证据链

E2E 失败时按以下顺序保存证据：

1. Ginkgo spec timeline 和 JUnit。
2. 被测对象 YAML、Event、namespace dump。
3. API Server/controller/scheduler 日志。
4. kubelet/runtime/journal 日志。
5. 节点状态、网络和存储插件状态。
6. 测试开始前后的 feature gate、版本和镜像 digest。

`cluster/log-dump/log-dump.sh` 会按 provider 收集 master/node 日志，并限制并发连接数，避免大集群耗尽文件描述符。

