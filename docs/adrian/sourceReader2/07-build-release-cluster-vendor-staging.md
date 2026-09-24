# Build、Release、Cluster、Vendor 与 Staging 发布

## 1. 本地构建

入口：

```bash
make all
make all WHAT=cmd/kubelet GOFLAGS=-v
make all WHAT=cmd/kube-apiserver DBG=1
```

调用链：

```text
Makefile:all
  -> hack/make-rules/build.sh
  -> kube::golang::setup_env
  -> kube::golang::build_binaries
  -> kube::golang::place_bins
  -> _output/bin
```

`DBG=1` 关闭优化，便于 delve；普通构建会通过 linker flags 注入版本并裁剪符号。`WHAT` 可接受目录、包和 vendor alias。

## 2. 跨平台与 Release

`build/release.sh` 的主链：

```text
kube::build::verify_prereqs
  -> 在 build container 中 make cross
  -> 默认 make test
  -> 默认 make test-integration
  -> kube::release::package_tarballs
```

`KUBE_RELEASE_RUN_TESTS=n` 可以跳过 release 脚本中的测试，但这不等于产物经过质量验证。

`build/lib/release.sh` 和 `build/package-tarballs.sh` 负责：

- server binaries。
- node binaries。
- client binaries。
- test binaries 与测试镜像材料。
- manifests、licenses、README 等附属文件。
- server image build context 与镜像。
- client/server/node/source tarball。

产物应从 `_output` 的受控 staging 目录打包，并固定 tar owner/group，避免开发机用户信息进入归档。

## 3. Cluster 脚本

关键文件：

- `cluster/kube-up.sh`
- `cluster/kube-down.sh`
- `cluster/kube-util.sh`
- `cluster/common.sh`
- `cluster/<provider>/util.sh`
- `cluster/kubectl.sh`

Provider 分派：

```text
KUBERNETES_PROVIDER，默认 gce
  -> kube-util.sh 加载 cluster/<provider>/util.sh
  -> provider 定义 kube-up/kube-down/prepare-e2e/detect-master
  -> 顶层脚本调用统一函数名
```

`cluster/skeleton/util.sh` 是 provider 接口说明书：默认函数只报未实现。GCE 的 `util.sh` 是完整实现，包含实例、网络、磁盘、启动脚本、控制面和节点配置。

`cluster/kubectl.sh` 负责定位 kubectl、provider 特定认证和 kubeconfig。它不是 kubectl 本身。

### Cluster 生命周期

```text
读取 config-default/config-test 与环境变量
  -> 验证 provider 与云端前置条件
  -> 构建或定位 release tarball
  -> 创建网络/控制面/节点资源
  -> 分发二进制与 manifests
  -> 等待 API Server
  -> 配置 kubeconfig
  -> validate/prepare-e2e
```

销毁必须按 provider 资源依赖逆序执行。测试失败时先 log dump，再 kube-down；否则证据会和集群一起消失。

## 4. Vendor 管理

不要手改生成的 `go.mod/go.work`，不要直接 `go mod tidy`。依赖变更流程：

```text
hack/pin-dependency.sh <module> <version>
  -> hack/update-vendor.sh
  -> 统一所有 staging/root module 版本
  -> tidy 与 prune replace
  -> 更新内部 module
  -> go work vendor
  -> 更新 LICENSES
  -> 检查 staging 不得反向依赖 k8s.io/kubernetes
```

`hack/update-vendor.sh` 的关键不变量：

- root 与 staging 模块对公共依赖保持一致版本。
- staging module 不能直接或传递依赖 `k8s.io/kubernetes`。
- generated comments 说明文件不可手改。
- vendor 与 licenses 同步。
- `go.work vendor` 是 vendor 树来源。

`hack/verify-vendor.sh` 会复制一个干净树，删除 vendor/LICENSES，重新运行 update-vendor，然后比较：

- 所有 go.mod/go.sum/go.work/go.work.sum。
- vendor 内容。
- dependency lint。
- 每个 staging repo 在记录版本下能否 tidy。

## 5. Staging 是源码真相

`staging/src/k8s.io/*` 存放会发布到独立仓库的源码。主仓通过 Go workspace 和 replace 让：

```go
import "k8s.io/client-go/dynamic"
```

解析到本树的 `staging/src/k8s.io/client-go/dynamic`。

约束：

- 可以直接修改 staging 源码。
- staging 禁止 import `k8s.io/kubernetes/...`，否则拆分后无法独立构建。
- `staging/publishing/import-restrictions.yaml` 定义允许的依赖边界。
- `staging/publishing/rules.yaml` 定义目标仓库、分支、源码目录、依赖和 smoke test。

## 6. Publishing

发布不是简单复制目录。Publishing bot 根据 rules：

```text
kubernetes/kubernetes 的 staging 目录
  -> 按 destination 切分源码
  -> 按依赖图确定发布顺序
  -> 重写/生成模块元数据
  -> 推送目标 kubernetes/<repo> 分支
  -> 执行可选 smoke test
```

当前树可确认的变化是 `streaming` 已成为独立 destination，并在 master/release-1.36 上被 apimachinery、api、client-go、code-generator 等依赖。发布顺序必须先满足 streaming，再发布下游。

## 7. 交付验收

| 层 | 验收 |
|---|---|
| binary | 目标平台可执行，version/linker 信息正确 |
| image | 入口、用户、架构、所需二进制和证书路径正确 |
| tarball | 文件集、权限、owner/group、licenses 完整 |
| cluster | kube-up、ready、kubectl、e2e、log dump、kube-down 闭环 |
| vendor | clean regeneration 无 diff，无 staging loopback dependency |
| staging | import restrictions 通过，拆分模块可独立 build/test |
| publishing | 依赖顺序、目标分支、source dirs、smoke test 正确 |

