[上一篇](api-server.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# apiserver 操作使用专题

> **层级**：跨篇操作纵深专题——把"怎么操作 kube-apiserver"从**凭证从哪来**、**URL 怎么拼**、**怎么建 Pod/Service**、**怎么监听集群变化**四条主线讲透。与 [API Server 全景](api-server.md)（讲内部机制：启动/路由/中间件/etcd）互补：那篇回答"请求进来后发生了什么"，本篇回答"我该怎么把请求发进去"。
>
> **代码基准**：`adrian@6bf0bfd51d78d1b3cb0917dc7001cd2aaf5549d4`（2026-11-26，adrian，Go 1.27.4）。锚点用「符号+偏移」，函数声明行为 `+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。

---

## 0. 结论速览

### 0.1 操作 apiserver 的本质

apiserver 就是一个 **HTTPS REST 服务**。所有操作（kubectl、Dashboard、client-go、curl）最终都是**发 HTTP 请求**到它。发请求需要**三样东西**：

```text
① 服务器地址（URL）   →  https://<VIP>:6443（集群外）/ https://kubernetes.default.svc（Pod 内）
② 证明"我是谁"的凭证  →  客户端证书 / ServiceAccount token / Bearer token
③ 校验服务器的 CA     →  certificate-authority-data（防中间人）
```

**记住这张图，后面全是在填这三样：**

```text
你想做的事（建 Pod / 查 Service / 监听事件）
        │
        ▼
拼一个 URL  ──────────→  方法(POST/GET/PUT/DELETE) + 路径(/api/v1/namespaces/<ns>/pods)
        │
        ▼
带上凭证  ───────────→  --cert/--key(证书) 或 Authorization: Bearer <token>
        │
        ▼
带上 CA  ────────────→  --cacert(校验服务器，别用 -k 跳过)
        │
        ▼
    apiserver
```

### 0.2 四条操作主线一句话结论

| 主线 | 最常见方式 | 凭证来源 | 关键 URL |
|---|---|---|---|
| **命令行操作** | `kubectl` | kubeconfig（默认 `~/.kube/config`） | kubectl 帮你拼 |
| **REST 直调（集群外）** | `curl` + 客户端证书 | kubeconfig 里解出来的 `client-certificate-data`/`client-key-data` | `/api/v1/namespaces/<ns>/pods` |
| **REST 直调（Pod 内）** | `curl` + SA token | 挂载的 `/var/run/secrets/kubernetes.io/serviceaccount/token` | `https://kubernetes.default.svc/api/v1/...` |
| **监听集群变化** | `kubectl get -w` / `watch=true` / informer | 同上 | `?watch=true`（长连接推事件） |

---

## 1. 新手层：先用 kubectl，理解"三样东西"

### 1.1 生活化比喻

把 apiserver 想成**一个需要门禁卡的大楼**：

- **服务器地址** = 大楼地址（你得知道去哪栋楼）。
- **凭证** = 你的门禁卡（证明你是谁、能不能进）。
- **CA** = 你确认"这真是公司大楼、不是骗子伪造的"（防止有人冒充大楼骗你的卡号）。

`kubectl` 就是**帮你自动带卡、认门、填表的秘书**——你只说"建个 nginx"，它把 URL、凭证、CA 都备齐了。用 `curl` 直调就是**你自己跑腿**，三样都得自己带。

### 1.2 最小可运行例子

```bash
# ① 确认地址（大楼在哪）
kubectl cluster-info
#   Kubernetes control plane is running at https://10.0.0.100:6443

# ② 确认凭证（当前用谁的身份、连哪个集群）
kubectl config current-context
kubectl config view --minify | head -20
#   会看到 server / certificate-authority-data / client-certificate-data

# ③ 做一件事（建 Pod）—— 三样 kubectl 全帮你带了
kubectl run nginx --image=nginx:1.27 --restart=Never -n default
#   pod/nginx created
```

**预期输出**：`kubectl run` 返回 `pod/nginx created`。这三条命令分别验证了"地址通、凭证有效、能写"。

### 1.3 凭证在哪里看（新手最常问）

管理员凭证（客户端证书方式）由 kubeadm 生成，落在**控制面节点**：

| 文件 | 内容 | 谁用 |
|---|---|---|
| `/etc/kubernetes/admin.conf` | 集群管理员 kubeconfig（含客户端证书） | 你从集群外 `kubectl`（拷到本地 `~/.kube/config`） |
| `/etc/kubernetes/pki/ca.crt` | 集群 CA | 校验服务器身份 |
| `/etc/kubernetes/pki/apiserver.crt/.key` | apiserver 服务端证书 | apiserver 自己 |

> **证书过期怎么看**：`kubeadm certs check-expiration`（官方 [证书管理](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/)）。`admin.conf` 里的客户端证书默认 1 年过期，过期后 `kubectl` 会报 `you must be logged in to the server`。

---

## 2. 熟手层：URL、凭证、CRUD 速查

### 2.1 URL 怎么拼（速查表）

apiserver 的 URL 有固定规律，记住**前缀 + 资源路径**：

| URL 前缀 | 用途 | 例子 |
|---|---|---|
| `/api/v1/...` | 核心组（core，无 group 名） | `/api/v1/namespaces/default/pods` |
| `/apis/<group>/<version>/...` | 非 core 组 | `/apis/apps/v1/namespaces/default/deployments` |
| `/api`、`/apis` | discovery（列出所有组/版本） | `GET /apis` |
| `/openapi/v2`、`/openapi/v3` | OpenAPI schema | `GET /openapi/v3` |
| `/version`、`/healthz`、`/livez`、`/readyz` | 版本/健康 | `GET /version` |

**资源路径模板**：

```text
集群级资源：   /apis/<group>/<version>/<resource>
命名空间资源： /apis/<group>/<version>/namespaces/<ns>/<resource>
核心组资源：   /api/v1/namespaces/<ns>/<resource>
子资源：       .../<resource>/<name>/<subresource>   如 pods/<name>/log、pods/<name>/exec
```

> **core 组为什么没有 group 名**：Pod/Service/ConfigMap/Secret 等最老的资源在 core 组，路径是 `/api/v1`，不是 `/apis/...`。其他组（apps、batch、rbac…）都是 `/apis/<group>/<version>`。discovery 会告诉你某个资源到底在哪个前缀：`kubectl api-resources` 看到的 `APIVERSION` 列就是它。

### 2.2 凭证怎么生成（三种用户身份）

**① 管理员（客户端证书）**——kubeadm 建集群时自动生成 `admin.conf`。给**其他人**发证书有两种办法：

```bash
# 方法 A：kubeadm 直接生成一份某用户的 kubeconfig（最省事）
kubeadm kubeconfig user --client-name dev-user --client-organization dev-team > dev-user.conf

# 方法 B：走 CSR 流程（证书签名请求，适合要自己管私钥的场景）
# 1) 本地生成私钥+CSR
openssl req -new -newkey rsa:2048 -nodes \
  -keyout dev-user.key -out dev-user.csr \
  -subj "/CN=dev-user/O=dev-team"
# 2) 把 CSR 提交成 CertificateSigningRequest 对象，approve，取回签好的证书
#    详见官方 CSR 页
```

> `kubeadm kubeconfig user` 官方文档：[kubeadm kubeconfig](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-kubeconfig/)。CSR 流程：[Certificate Signing Requests](https://kubernetes.io/docs/reference/access-authn-authz/certificate-signing-requests/)。

**证书里的 CN/O 怎么变成用户名/组**：apiserver 用 `CommonNameUserConversion`（`staging/src/k8s.io/apiserver/pkg/authentication/request/x509/x509.go` `CommonNameUserConversion+0`）把证书 `CN` 当**用户名**、`O` 当**组**。所以 `CN=dev-user/O=dev-team` → 用户 `dev-user`、组 `dev-team`，再由 RBAC 决定这组能干啥。

**② 长期 token（给脚本/CI）**——建一个 ServiceAccount，再拿它的 token：

```bash
kubectl create sa my-sa -n default
kubectl create token my-sa -n default          # 打印一个短期 JWT（1.24+ 推荐）
#   或建一个长期 token Secret：
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: Secret
metadata: {name: my-sa-token, namespace: default}
annotations: {kubernetes.io/service-account.name: my-sa}
type: kubernetes.io/service-account-token
YAML
```

**③ Pod 内身份（SA token 自动挂载）**——不用手动生成，Pod 起来时自动挂了三件套：`token`/`ca.crt`/`namespace`（源码见 §3.2）。

### 2.3 curl 直调模板（集群外，客户端证书）

从 kubeconfig 把 base64 解成文件，再用 curl：

```bash
# 把 kubeconfig 里的三样解出来（假设 kubeconfig 是 admin.conf）
grep certificate-authority-data admin.conf | awk -F'"' '{print $4}' | base64 -d > ca.crt
grep client-certificate-data  admin.conf | awk -F'"' '{print $4}' | base64 -d > client.crt
grep client-key-data          admin.conf | awk -F'"' '{print $4}' | base64 -d > client.key
APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')

# 建一个 Pod
curl -s --cacert ca.crt --cert client.crt --key client.key \
  -X POST $APISERVER/api/v1/namespaces/default/pods \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"v1","kind":"Pod","metadata":{"name":"curl-pod"},
 "spec":{"containers":[{"name":"c","image":"nginx:1.27"}]}}
JSON

# 建一个 Service
curl -s --cacert ca.crt --cert client.crt --key client.key \
  -X POST $APISERVER/api/v1/namespaces/default/services \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"v1","kind":"Service","metadata":{"name":"curl-svc"},
 "spec":{"selector":{"app":"curl-pod"},"ports":[{"port":80,"targetPort":80}]}}
JSON
```

**预期输出**：两个 POST 都返回 `201` 和带 `metadata.uid` 的 JSON（`"kind":"Pod"`/`"kind":"Service"`）。

### 2.4 curl 直调模板（Pod 内，SA token）

```bash
# Pod 里执行
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
NS=$(cat   /var/run/secrets/kubernetes.io/serviceaccount/namespace)
curl -s --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  -H "Authorization: Bearer $TOKEN" \
  https://kubernetes.default.svc/api/v1/namespaces/$NS/pods
```

**预期输出**：返回 `{"kind":"PodList","items":[...]}`（前提：这个 SA 的 RBAC 允许 list pods，否则 `403`）。

### 2.5 监听集群操作（三种）

| 方式 | 命令/URL | 特点 |
|---|---|---|
| **kubectl watch** | `kubectl get pods -w` | 最简单，命令行看变化 |
| **REST watch** | `GET /api/v1/namespaces/<ns>/pods?watch=true` | 长连接，服务端推 `ADDED/MODIFIED/DELETED` 事件流 |
| **client-go informer** | 代码里用 `ListWatch` + `SharedIndexInformer` | 生产推荐：本地缓存 + 事件回调，不打爆 apiserver |

**watch 的 REST 形态**：

```bash
curl -sN --cacert ca.crt --cert client.crt --key client.key \
  "$APISERVER/api/v1/namespaces/default/pods?watch=true"
```

**预期输出**：一连串 JSON 对象，每行一个 `{"type":"ADDED","object":{...}}`（`-N` 关闭缓冲，否则看不到流式输出）。`type` 取值 `ADDED`/`MODIFIED`/`DELETED`/`BOOKMARK`。

> watch 的服务端实现（`Cacher` 内存 watch cache、etcd watch、流式推送）见专题 [etcd watch 链路](etcd-watch链路.md) 和 [informer resync 机制](informer-resync机制.md)。

---

## 3. 高手层：源码锚点与边界陷阱

### 3.1 凭证链的源码位置

客户端发来的凭证由 apiserver 的 **authenticator 链**逐个尝试解析（任一命中即得到 `user.Info`，全不命中 → `401`）。装配在 `pkg/kubeapiserver/authenticator/config.go Config.New+0`：

```text
Config.New（authenticator/config.go）
├── requestheader authenticator        前置代理转发来的身份头
├── x509 authenticator                 ← 客户端证书分支
│     x509.NewDynamic(VerifyOptions, CommonNameUserConversion)
│       CommonNameUserConversion+0     （x509.go:289）CN→用户名、O→组
├── token 类（先经 tokenunion.New 合成一个）
│     ├── legacy service-account token
│     ├── projected service-account token（JWT）
│     ├── bootstrap token
│     ├── OIDC
│     └── webhook token
└── union.New（所有 authenticator 串成链）
      └── Anonymous.Enabled 时再追加一个匿名 authenticator
```

**Pod 内 SA token 的路径写死在 client-go**（`staging/src/k8s.io/client-go/rest/config.go` `InClusterConfig+0`）：

```go
const (
    tokenFile  = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    rootCAFile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)
host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
// Host = "https://" + net.JoinHostPort(host, port)，BearerToken = token 文件内容
```

> 注意 `InClusterConfig` 里 `// TODO: switch to using cluster DNS.`——client-go 默认用 `KUBERNETES_SERVICE_HOST/PORT` 环境变量拼地址，**不是** `kubernetes.default.svc` DNS 名（DNS 名是你手写 curl 时才用，两者指向同一个 ClusterIP）。

### 3.2 四类情况全覆盖

**① 正常用法**：见 §1.2/§2.3/§2.4。三样齐 → 请求通。

**② 边界情况**：

| 边界 | 表现 | 处理 |
|---|---|---|
| 大列表 | `kubectl get pods` 只返回部分 | 用 `?limit=` + `?continue=` 分页（服务端定义 continue token） |
| watch 断线 | 长连接被 LB/LB idle 超时掐断 | 客户端带 `?resourceVersion=` 重连，从上次 RV 续（informer 自动做） |
| 名字冲突 | POST 一个已存在的 Pod | 返回 `409 Conflict`（Create 幂等：已存在即失败，见 etcd3 `OptimisticPut expectedRev=0`） |
| resourceVersion=0 | list 走缓存（可能读到旧值） | 需强一致时显式传具体 RV |

**③ 出错处理（高频报错对照）**：

| 报错 | 含义 | 定位 |
|---|---|---|
| `401 Unauthorized` / `you must be logged in` | **凭证无效**（证书过期/token 坏/没带凭证） | 查 `kubeadm certs check-expiration`；确认带了 `--cert/--key` 或 `Authorization` 头 |
| `403 Forbidden` | **身份有效但没权限**（RBAC 不放行） | `kubectl auth can-i <verb> <resource> -n <ns> --as <user>` |
| `certificate signed by unknown authority` | **没带 CA 或 CA 不对**（别用 `-k` 掩盖） | 确认 `--cacert` 用的是集群 CA |
| `x509: certificate has expired` | 服务器或客户端证书过期 | 同 401 第一条 |
| `connection refused` | 地址/端口不对，或 apiserver 没起 | `kubectl cluster-info`；确认 `--secure-port`（默认 6443） |

**④ 什么时候不该直调 REST**：

- **日常操作用 kubectl**——它帮你拼 URL、带凭证、处理分页/watch 重连/protobuf 编码。直调 curl 适合**脚本、调试、CI**，不适合当日常。
- **生产程序监听集群用 informer，别裸 watch**——裸 `?watch=true` 断线要自己处理 RV 续传、限流、重试；informer（client-go）把这些都封装好了（[informer 与 workqueue](../sourceReader/11-informer与workqueue机制.md)）。
- **别用 `curl -k`（跳过证书校验）当常态**——它放弃了防中间人，只在临时调试连通性时用一次。

### 3.3 声明 ≠ 完成（新手最易踩的坑）

POST 返回 `201` 只代表**期望状态落进了 etcd**，不代表对象已就绪：

- Pod 的 `status.podIP` 要等 scheduler 绑节点 + kubelet 起容器后才回写。
- Service 的 `spec.clusterIP` 由 apiserver 内置 service registry 在 create 时分配；`Endpoints`/EndpointSlice 由控制器根据 Pod 就绪状态生成。

所以 `curl` 建完 Pod 立刻 GET，可能看到 `status.phase: Pending`、`podIP` 为空——这是正常的收敛过程，不是 bug。声明式系统"提交即返回、收敛需时间"，完整链路见主线篇 [02 kubectl run 全路径](../sourceReader/02-kubectl-run全路径走读.md)。

---

## 4. 验证与参考

### 4.1 代码回核

本专题关键锚点在 HEAD `6bf0bfd5` 复核：
- `InClusterConfig+0`（`staging/src/k8s.io/client-go/rest/config.go`，token/CA 路径常量、`KUBERNETES_SERVICE_HOST/PORT`）。
- `CommonNameUserConversion+0`（`staging/src/k8s.io/apiserver/pkg/authentication/request/x509/x509.go` · `CommonNameUserConversion` `+0`），由 `staging/src/k8s.io/apiserver/pkg/authentication/authenticatorfactory/delegating.go` · `DelegatingAuthenticatorConfig.New` `+20` 的 `x509.NewDynamic(..., CommonNameUserConversion)` 装配。
- authenticator 链装配：`pkg/kubeapiserver/authenticator/config.go Config.New+0`（x509/tokenunion/union/anonymous 顺序）。

> **基准重钉（2026-11-26）**：代码基准从 `88939239`（2026-11-18 master）重钉到 `6bf0bfd5`（2026-11-26 adrian）。本篇为实操向专题（操作方式 kubectl/curl/informer、凭证生成与查看、URL 拼接规则、建 Pod/Service、监听集群变化、高频报错对照），这类内容不随代码漂移。抽查涉及的 apiserver 侧符号定义行均未变化：`CommonNameUserConversion`（x509.go 定义行 289，CN→用户名、O→组逻辑不变）、`InClusterConfig`（client-go rest/config.go 定义行 563，token/CA 路径常量与 `// TODO: switch to using cluster DNS.` 注释均在）、authenticator 装配入口 `Config.New`、x509 分支装配点 `DelegatingAuthenticatorConfig.New`（x509.NewDynamic 传 `CommonNameUserConversion`）。故除重钉基准与消除 `delegating.go` 裸行号的二义外，正文实操内容未改。

### 4.2 生产实测

`[待运行验证]` 本专题所有 curl/kubectl 命令均为标准用法（对齐官方文档），但**未在真实集群逐条跑过**。证书过期时间、TokenRequest TTL 等随集群配置变化，以实际环境为准。

### 4.3 官方参考链接

| 主题 | 链接 |
|---|---|
| 访问控制总览（传输安全/认证/鉴权三阶段） | [Controlling Access to the Kubernetes API](https://kubernetes.io/docs/concepts/security/controlling-access/) |
| kubeconfig 组织集群访问 | [使用 kubeconfig 文件组织集群访问](https://kubernetes.io/zh-cn/docs/concepts/configuration/organize-cluster-access-kubeconfig/) |
| 认证机制（客户端证书/SA token/OIDC…） | [Authenticating](https://kubernetes.io/docs/reference/access-authn-authz/authentication/) |
| RBAC 鉴权（Role/ClusterRole/Binding） | [Using RBAC Authorization](https://kubernetes.io/docs/reference/access-authn-authz/rbac/) |
| 证书签名请求 CSR（给用户发证书） | [Certificate Signing Requests](https://kubernetes.io/docs/reference/access-authn-authz/certificate-signing-requests/) |
| kubeadm 证书管理（查看/续期过期） | [Certificate Management with kubeadm](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/) |
| kubeadm kubeconfig user（生成用户 kubeconfig） | [kubeadm kubeconfig](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-kubeconfig/) |
| Kubernetes API 概念（Discovery + OpenAPI） | [The Kubernetes API](https://kubernetes.io/docs/concepts/overview/kubernetes-api/) |
| REST API 参考（各资源端点） | [API Reference](https://kubernetes.io/docs/reference/kubernetes-api/) |
| client-go 库（informer/REST 客户端） | [client-go on GitHub](https://github.com/kubernetes/client-go) |

### 4.4 关联文档

- 内部机制：[API Server 全景](api-server.md)、[apiserver 启动与路由](../sourceReader/05-apiserver启动与路由.md)、[apiserver 请求处理与 etcd](../sourceReader/06-apiserver请求处理与etcd.md)
- 监听机制：[etcd watch 链路](etcd-watch链路.md)、[informer 与 workqueue](../sourceReader/11-informer与workqueue机制.md)、[informer resync 机制](informer-resync机制.md)
- 主线场景：[02 kubectl run 全路径走读](../sourceReader/02-kubectl-run全路径走读.md)、[04 kubectl 客户端](../sourceReader/04-kubectl客户端.md)

[总目录](../sourceReader/README.md)
