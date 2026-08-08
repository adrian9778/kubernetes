# Update、Verify 与 Codegen

## 1. 三者的职责

- `update-*`：从源码真相重建派生文件，会修改工作树。
- `verify-*`：在隔离环境运行对应生成器或检查器，然后比较差异，不应修复源码。
- `codegen`：API 和配置类型驱动的代码生成集合，是 update 的一个核心子集。

生成文件只读。不要手改 `zz_generated.*`、`generated.pb.go`，也不要通过手工编辑 `go.mod` 绕过依赖工具。

## 2. `make update`

调用链：

```text
Makefile:update
  -> hack/make-rules/update.sh
  -> 顺序执行 BASH_TARGETS
```

当前目标顺序：

1. `update-codegen`
2. `update-featuregates`
3. `update-generated-api-compatibility-data`
4. `update-generated-docs`
5. `update-openapi-spec`
6. `update-gofmt`
7. `update-golangci-lint-config`

默认 silent 且 short-circuit：首个失败即停止。`SILENT=false` 显示日志；`FORCE_ALL=true` 收集更多失败。

## 3. `make verify`

调用链：

```text
Makefile:verify
  -> hack/make-rules/verify.sh
  -> 发现根模块和所有 staging/**/go.mod
  -> 对每个模块执行 hack/verify-*.sh 与 *.py
  -> shell2junit 记录结果
  -> 汇总失败项
```

`WHAT` 选择具体检查：

```bash
make verify WHAT="gofmt typecheck"
```

`make quick-verify` 只运行已知快速检查，例如 boilerplate、API groups、feature gates、gofmt、imports、package names、staging metadata、test images 等。

默认全 verify 也有显式排除项，例如 dockerized wrapper、PR 独立 lint job、周期性 license 检查。CI 还可通过 `EXCLUDE_TYPECHECK`、`EXCLUDE_GODEP`、`EXCLUDE_GOLANGCI_LINT` 控制拆分 job。

## 4. Verify-generated 模式

许多 verify 脚本使用 `hack/lib/verify-generated.sh`：

```text
复制或保存当前工作树状态
  -> 在隔离上下文运行 update 脚本
  -> 比较生成前后差异
  -> 无差异成功
  -> 有差异打印应运行的 update 命令
```

因此 verify 失败通常不是“检查器坏了”，而是源码和已提交派生文件不一致。

## 5. `hack/update-codegen.sh`

脚本先：

- 初始化 Go 环境和 protoc。
- 安装 goimports。
- 用 git pathspec 找出受控文件，同时包含修改和未跟踪文件。
- 排除 vendor、go.work、`_codegenignore` 和不应触碰目录。
- 扫描列首 `+k8s:` 标签，决定生成目标。
- 固定 `LC_ALL=C`，保证排序可重现。

当前代码生成函数覆盖的主要类别：

- protobuf
- deepcopy
- swagger typed docs
- conversion
- defaulting
- clientset
- lister
- informer
- apply configuration
- OpenAPI model
- validation
- prerelease lifecycle

### 关键标签

```go
// +k8s:deepcopy-gen=package
// +k8s:conversion-gen=...
// +k8s:defaulter-gen=TypeMeta
// +k8s:openapi-gen=true
// +k8s:prerelease-lifecycle-gen=true
// +k8s:protobuf-gen=package
```

标签必须位于生成器预期的位置和列。标签写错时最危险的情况不是编译失败，而是生成器根本没有把包识别为目标。

## 6. 生成物依赖顺序

```text
API Go 类型与标签
  -> protobuf/deepcopy
  -> conversion/defaulting/validation
  -> clientset/lister/informer/applyconfiguration
  -> OpenAPI model/spec
  -> generated compatibility data
  -> gofmt
  -> verify diff
```

protobuf 必须较早生成，因为后续生成器可能读取其结果。OpenAPI spec 在 codegen 之后运行，因为它依赖生成的模型定义和可启动 API Server。

## 7. 常见生成文件

- `zz_generated.deepcopy.go`
- `zz_generated.conversion.go`
- `zz_generated.defaults.go`
- `zz_generated.validations.go`
- `generated.pb.go`
- `generated.proto`
- generated clientset、typed fake、lister、informer
- apply configuration builder
- `types_swagger_doc_generated.go`
- OpenAPI JSON 规范
- generated API compatibility data

## 8. API 改动后的最小闭环

```text
修改源类型/标签
  -> 修改 defaulting/conversion/validation 源逻辑
  -> 更新 round-trip/default/validation 单测
  -> hack/update-codegen.sh
  -> hack/update-openapi-spec.sh
  -> hack/update-generated-api-compatibility-data.sh
  -> 检查生成 diff 是否符合预期
  -> 运行受影响包单测
  -> 运行 API integration
  -> make verify 相关目标
```

不能把 `make update` 当作设计工具。先确定 API 语义，再生成；否则会得到大量“正确生成但设计错误”的文件。

