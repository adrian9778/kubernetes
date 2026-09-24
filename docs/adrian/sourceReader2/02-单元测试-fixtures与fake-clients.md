# 单元测试、Fixtures 与 Fake Clients

## 1. 同包测试的结构

典型文件：

- `pkg/scheduler/scheduler_test.go`
- `pkg/kubelet/kubelet_test.go`
- `pkg/controller/deployment/deployment_controller_test.go`

常见骨架：

```go
func TestFeature(t *testing.T) {
    cases := []struct {
        name    string
        input   Input
        want    Output
        wantErr string
    }{ /* cases */ }

    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            got, err := Run(tc.input)
            // 分别断言错误和结果
        })
    }
}
```

Kubernetes 常用 `cmp.Diff` 比较复杂对象，`testify/require` 终止当前 case，`featuregatetesting.SetFeatureGateDuringTest` 临时切换 feature gate，`ktesting.NewTestContext` 创建带 logger 的 context。

## 2. 测试状态机的四个控制旋钮

### 2.1 Fake clock

时间驱动控制器不能依赖 `time.Sleep`。使用 `k8s.io/utils/clock/testing.FakeClock`：

```text
建立初始时间
  -> 启动待测 goroutine
  -> 等待其注册 timer
  -> Step 推进时间
  -> 观察 queue/action/state
```

需要先确认 goroutine 已经进入等待，否则先 Step 可能丢失时序，制造偶发测试。

### 2.2 Context 与 stop channel

新代码优先 `context.Context`；旧 informer/controller 常用 `<-chan struct{}`。清理顺序应是：

```text
cancel/close stop
  -> 等待 worker 退出
  -> 关闭 server/runtime
  -> 删除临时目录
```

Kubelet 测试尤其容易在插件管理器仍访问临时目录时提前清理。

### 2.3 Fake recorder

`record.FakeRecorder` 验证 Event。Event 是副作用，不应只检查返回值；同时要避免测试永久阻塞在未消费的 channel 上。

### 2.4 Action 记录

fake client 会记录 action。控制器单测常断言：

- verb：`get/list/watch/create/update/patch/delete`
- resource/subresource
- namespace/name
- patch 类型和内容
- action 次序与次数

这比只检查 fake tracker 最终对象更能发现控制器发错 API 调用。

## 3. Client-go fake 的执行链

关键文件：

- `staging/src/k8s.io/client-go/testing/fake.go`
- `staging/src/k8s.io/client-go/testing/fixture.go`
- `staging/src/k8s.io/client-go/kubernetes/fake/clientset_generated.go`

核心类型：

- `testing.Fake`：保存 reactor 链、watch reactor、proxy reactor 和 action 历史。
- `ReactionFunc`：匹配 action 后返回 `(handled, object, error)`。
- `ObjectTracker`：内存对象仓库，实现基础 CRUD/watch。
- 生成的 `fake.Clientset`：暴露 typed client，并把调用导向 `testing.Fake.Invokes`。

调用链：

```text
client.CoreV1().Pods(ns).Create(...)
  -> 生成 typed fake method
  -> Fake.Invokes(action, defaultReturn)
  -> 记录 action
  -> 依次扫描 ReactorChain
  -> 第一个 handled=true 的 reactor 截断链
  -> 默认 ObjectReaction 操作 ObjectTracker
  -> 返回 runtime.Object/error
```

`PrependReactor` 比 `AddReactor` 优先级高，适合覆盖默认 tracker 行为。匹配时应尽量限定 verb/resource，使用 `*/*` 容易吞掉无关调用。

### `NewSimpleClientset` 与 `NewClientset`

当前树同时存在二者。阅读调用点时不要假设语义完全相同，应以生成的 clientset 文件为准。通常 simple 版本围绕 tracker 提供传统 fake 行为；更新的 fake 可能加入字段选择、apply 或生成器增强。无论哪种，它们都不是 HTTP API Server。

### Fake client 明确不保证什么

- 不执行 admission。
- 不执行 API defaulting/conversion 的完整 HTTP 链。
- 不保证 etcd resourceVersion、compaction 和一致性语义。
- 不自动执行真实 server-side apply field manager。
- 不运行 controller。
- 不模拟认证、授权、限流和网络失败。

如果被测逻辑依赖这些性质，应升级为 integration。

## 4. Reactor 的正确用途

适合：

- 注入某次 API 调用失败。
- 捕获并检查 patch/update 内容。
- 模拟 conflict、NotFound、Forbidden。
- 控制 watch 返回事件。
- 验证 retry 后的调用次数。

示例状态机：

```text
第 1 次 Update -> Conflict
controller 重新 Get
第 2 次 Update -> Success
断言最终 action 序列与对象版本
```

不要用 reactor 手写整个 API Server。测试代码比生产代码更复杂时，通常说明应该写 integration。

## 5. Fixtures、testdata 与 testing-manifests

### `test/fixtures`

当前树约 80 个文件，主要服务 kubectl、文档 YAML 和命令行为。典型用途：

- 输入 manifest。
- golden output。
- shell plugin fixture。
- 证书、token、JSONL 等固定测试材料。

### 包内 `testdata`

Go 工具默认忽略名为 `testdata` 的目录作为普通包，但测试可按相对路径读取。适合把 fixture 与所属包绑定。

### `test/e2e/testing-manifests`

这是 e2e 可部署资源模板，不等同于单测 fixture。部分文件后缀为 `.in`，运行时替换镜像或参数。

### `go:embed`

例如 e2e_node testing manifests 可嵌入测试二进制。优势是远端执行时不依赖源码目录；代价是 fixture 更新必须重新构建二进制。

## 6. 新手重建这一层

最小实现顺序：

1. 实现 table-driven test 与 `cmp.Diff`。
2. 实现内存 ObjectTracker：按 GVR/namespace/name 存对象。
3. 定义 Action 接口和 action recorder。
4. 实现 reactor 链，支持 prepend/add 和首个 handled 截断。
5. 实现 fake typed client，把 CRUD 转成 Action。
6. 实现 watch reactor 与事件 channel。
7. 加 fake clock，验证队列重试和退避。

验收标准：能复现成功、NotFound、Conflict、watch event、重试和 cancel 清理；测试不能依赖真实睡眠。

