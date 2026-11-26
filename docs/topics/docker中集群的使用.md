[上一篇](apiserver操作使用.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# Docker Desktop 内置 Kubernetes 使用专题

> **层级**：跨篇操作纵深专题——讲透 **Docker Desktop 自带的 Kubernetes 集群**（Settings → Kubernetes → Enable 一键启用的那个）：常用操作、YAML 写法、apiserver 怎么连、网络怎么通、以及这个环境特有的坑。
>
> **定位澄清**：这是 **Docker Desktop 内置的 Kubernetes 集群**（一键启用），跟"把 Docker 当容器运行时"（K8s 1.24 起已移除 dockershim，见 [19-kubeadm集群引导](../sourceReader/19-kubeadm集群引导.md)）不是一回事，别混。但它有**两种引擎形态**（老版 kubeadm / 新版 kind），见 §0.1——**apiserver 地址、节点名都不同**，别拿错地址去 curl。
>
> **事实红线**：环境相关行为（端口、context 名、版本）以本机实际为准。**本文凡涉及 apiserver 地址，一律从 kubeconfig 动态读，不写死端口**（新版端口是随机的）。未实测项标 `[待运行验证]`。

---

## 0. 结论速览

### 0.1 它是什么 + 两种引擎形态

Docker Desktop 里一键启用的 Kubernetes，本质是**帮你起一个本地单节点集群**（控制面 + 工作节点合一），跑在 Docker 容器/VM 里。特点是**零配置、随 Docker Desktop 启停**。但**按 Docker Desktop 版本不同，底层引擎有两种形态**——**apiserver 地址和节点名都不一样**，curl 前务必先分清自己是哪种：

| 特性 | 老版（kubeadm 引擎） | 新版（kind 引擎） | 怎么判 |
|---|---|---|---|
| 节点名 | `docker-desktop` | `desktop-control-plane`（control-plane-xxx） | `kubectl get nodes` 看名字 |
| apiserver 地址 | `https://kubernetes.docker.internal:6443`（**固定**） | `https://127.0.0.1:<随机高位端口>`（**重建会变**） | 见下方命令 |
| 端口 | 固定 6443 | 随机高位（如 58087） | 同上 |
| kubeconfig context | `docker-desktop` | `docker-desktop` | 一样 |
| 集群/用户名 | `docker-desktop` | `docker-desktop` | 一样 |
| NodePort/LoadBalancer | 绑宿主机 `localhost` | 绑宿主机 `localhost` | 一样 |

**一条命令拿到你本机真实的 apiserver 地址**（两种形态通用，本文后续 curl 都用它，**别写死端口**）：

```bash
APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')
echo "$APISERVER"
#   老版 → https://kubernetes.docker.internal:6443
#   新版 → https://127.0.0.1:58087（随机）
```

### 0.2 和"自己搭集群"最大的区别

**你不用碰凭证、证书、kubeconfig 生成的细节**——Docker Desktop 全自动了。所以本专题的重点不是"怎么生成凭证"（见 [apiserver操作使用](apiserver操作使用.md)），而是"**在这个受限的单节点环境里，哪些能用、哪些不能、网络怎么绕**"。

---

## 1. 新手层：启用 + 跑通第一个 Pod

### 1.1 生活化比喻

Docker Desktop 的 Kubernetes 就像**手机里的"开发者模式"**：开关一拨，底下整套东西就跑起来了，你不用知道它怎么装。但它只有一台"手机"（单节点），所以跟真正的多节点集群（能调度到不同机器、有真实负载均衡）行为不完全一样。

### 1.2 启用（三步）

```text
Docker Desktop → Settings（齿轮）→ Kubernetes → 勾选 "Enable Kubernetes"
   → Apply & Restart → 等几分钟（首次要拉镜像）
```

新版 Docker Desktop 也可在左侧 **Kubernetes 视图**直接点 **Create cluster**，选 **Kubeadm**（单节点，版本随 Docker Desktop）。

### 1.3 最小可运行例子

```bash
# ① 确认连上了这个集群（context 是 docker-desktop）
kubectl config current-context
#   docker-desktop

kubectl get nodes
#   NAME                    STATUS   ROLES           AGE   VERSION
#   docker-desktop          Ready    control-plane,master   5m   v1.3x.x   ← 老版
#   desktop-control-plane   Ready    control-plane          5m   v1.36.1   ← 新版(kind)

# ② 跑一个 Pod
kubectl run nginx --image=nginx:1.27 --restart=Never
#   pod/nginx created

# ③ 看它起来了没
kubectl get pod -w
#   NAME    READY   STATUS    RESTARTS   AGE
#   nginx   1/1     Running   0          30s
```

**预期输出**：`get nodes` 看到 `docker-desktop` 一个节点、`Ready`；Pod 变 `Running`。三样都通 = 环境正常。

### 1.4 第一个 Service（浏览器能访问）

```bash
kubectl expose pod nginx --port=80 --type=NodePort --name=nginx-svc
kubectl get svc nginx-svc
#   NAME        TYPE       CLUSTER-IP    PORT(S)        AGE
#   nginx-svc   NodePort   10.96.x.x     80:31xxx/TCP   10s

# Docker Desktop 会把 NodePort 绑到宿主机 localhost，直接浏览器开：
#   http://localhost:31xxx
```

**预期输出**：浏览器看到 nginx 欢迎页。**这是 Docker Desktop 最方便的一点**——不用配 ingress、不用 extraPortMappings，NodePort 直接通 localhost。

---

## 2. 熟手层：YAML 写法与常用操作速查

### 2.1 一个完整的 YAML（Pod + Service + Deployment）

本地开发最常用的三件套，存成 `app.yaml`：

```yaml
# --- Deployment：管副本和滚动更新（比裸 Pod 更适合开发）---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {app: web}
spec:
  replicas: 2
  selector:
    matchLabels: {app: web}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
      - name: web
        image: nginx:1.27
        ports:
        - containerPort: 80
        resources:                      # 单节点集群建议都写，防一个 Pod 吃满
          requests: {cpu: 100m, memory: 128Mi}
          limits:   {cpu: 500m, memory: 256Mi}
---
# --- Service：ClusterIP，集群内访问 ---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector: {app: web}
  ports:
  - port: 80
    targetPort: 80
---
# --- 想从宿主机浏览器访问，用 NodePort 或 LoadBalancer ---
apiVersion: v1
kind: Service
metadata:
  name: web-np
spec:
  type: NodePort                        # 或 LoadBalancer（Docker Desktop 也支持）
  selector: {app: web}
  ports:
  - port: 80
    targetPort: 80
    nodePort: 30080                     # 可指定（30000-32767），不写则自动分配
```

```bash
kubectl apply -f app.yaml
kubectl get deploy,svc
```

**预期输出**：`deployment.apps/web created`、两个 Service 创建成功；`kubectl get pods` 看到 2 个 `web-xxxxx` 副本。

### 2.2 常用操作速查表

| 想做的事 | 命令 | 备注 |
|---|---|---|
| 看节点 | `kubectl get nodes -o wide` | 单节点只有 `docker-desktop` |
| 看所有 Pod（含 kube-system） | `kubectl get pods -A` | 能看到 Docker Desktop 自带的控制面组件 |
| 看日志 | `kubectl logs <pod> [-c 容器]` | `-f` 跟随 |
| 进容器 | `kubectl exec -it <pod> -- sh` | 镜像没 sh 就试 `bash` |
| 端口转发（不想用 NodePort） | `kubectl port-forward svc/web 8080:80` | 宿主机 `localhost:8080` → Service 80 |
| 应用配置 | `kubectl apply -f app.yaml` | 改了重 apply 即滚动更新 |
| 删除 | `kubectl delete -f app.yaml` | 按 YAML 删 |
| 看 API 资源 | `kubectl api-resources` | 本集群支持哪些资源 |
| 切 context | `kubectl config use-context docker-desktop` | 多集群时切换 |

### 2.3 apiserver 怎么连（这个环境特有）

Docker Desktop 已经把 kubeconfig 写好了，默认 `~/.kube/config`。它长这样（关键三样）：

```yaml
clusters:
- cluster:
    server: https://127.0.0.1:58087   # ← 新版(kind)：随机高位端口；老版为 https://kubernetes.docker.internal:6443
    certificate-authority-data: <base64 CA>           # Docker Desktop 自动生成
  name: docker-desktop
users:
- user:
    client-certificate-data: <base64>                 # 客户端证书，自动生成
    client-key-data: <base64>
  name: docker-desktop
contexts:
- context: {cluster: docker-desktop, user: docker-desktop}
  name: docker-desktop
current-context: docker-desktop
```

**要点**：
- **server 地址因引擎而异**（见 §0.1）：新版 kind 是 `127.0.0.1:<随机端口>`、**重建集群端口会变**；老版是固定的 `kubernetes.docker.internal:6443`。**curl 时永远从 kubeconfig 动态读**：`APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')`，别写死。
- 老版专属：`kubernetes.docker.internal` 是 Docker Desktop 在 **hosts 文件**里指向 `127.0.0.1` 的本地域名。**若被安全软件/系统优化工具删掉**，会报 TLS 握手超时——需手动补回 `127.0.0.1 kubernetes.docker.internal`。新版 kind 不用这个域名。
- **apiserver 只监听宿主机 `127.0.0.1`**。所以**别的机器/虚拟机没法直连这个集群**（除非改 `--bind-address` 和 kubeconfig 的 server，见 §3.2）。
- 凭证全自动：你**不需要**像自建集群那样 `kubeadm kubeconfig user` 或生成证书。

**用 curl 直调 apiserver 的四种方式**（地址一律用 `$APISERVER`；完整 URL/凭证/CRUD 模板见 [apiserver操作使用](apiserver操作使用.md) §2）：

#### 方式一：`kubectl proxy`（最省事，开发首选）

不用手动解证书，代理帮你带凭证：

```bash
kubectl proxy
#   Starting to serve on 127.0.0.1:8001

# 另开一个终端，无凭证、纯 http 直连代理：
curl -s http://127.0.0.1:8001/api/v1/namespaces/default/pods | jq .

# 建 Pod
curl -s -X POST http://127.0.0.1:8001/api/v1/namespaces/default/pods \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"apiVersion":"v1","kind":"Pod","metadata":{"name":"curl-pod"},
 "spec":{"containers":[{"name":"c","image":"nginx:1.27"}]}}
JSON
```

代理只监听 `127.0.0.1:8001`，安全。

#### 方式二：直接 curl apiserver（解 kubeconfig 里的证书）

Docker Desktop 已自动生成客户端证书，你只需把 kubeconfig 里 base64 的三样解出来：

```bash
APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')
echo "$APISERVER"
#   老版 → https://kubernetes.docker.internal:6443
#   新版(kind) → https://127.0.0.1:58087（随机，重建会变）

# 解出 CA / 客户端证书 / 私钥
CA=$(mktemp); CRT=$(mktemp); KEY=$(mktemp)
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$CA"
kubectl config view --raw -o jsonpath='{.users[0].user.client-certificate-data}'  | base64 -d > "$CRT"
kubectl config view --raw -o jsonpath='{.users[0].user.client-key-data}'         | base64 -d > "$KEY"

# GET 版本（验证连通）
curl -s --cacert "$CA" --cert "$CRT" --key "$KEY" "$APISERVER/version" | jq .

# GET Pod 列表
curl -s --cacert "$CA" --cert "$CRT" --key "$KEY" \
  "$APISERVER/api/v1/namespaces/default/pods" | jq .

# 用完删临时证书文件（别留在 /tmp）
rm -f "$CA" "$CRT" "$KEY"
```

> ⚠️ **别写死 `6443`**：新版 kind 端口是随机的，写死必连不上（报 `Failed to connect ... port 6443`）。地址永远从 kubeconfig 读。

#### 方式三：`curl -k`（仅调试连通性）

```bash
APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')
curl -k "$APISERVER/version"
```

`-k` 跳过证书校验，**只用于确认"通不通"**，别当常态——它放弃了防中间人。

#### 方式四：ServiceAccount token（给程序/脚本用）

前三种用的是**你的用户身份**（客户端证书），token 方式用的是 **ServiceAccount 身份**——适合塞进脚本、CI、或 Pod 里的程序。分两步：**拿 token** + **给权限**。

**① 拿 token**：

```bash
# 建一个专用 ServiceAccount（别用 default，权限好收敛）
kubectl -n default create sa my-sa

# 签发一个短期 token（默认 1 小时，1.24+ 推荐方式）
kubectl -n default create token my-sa
#   eyJhbGciOiJSUzI1NiIsImtpZCI6...   ← 直接打印出来

# 想要长期 token，建一个绑 SA 的 Secret：
kubectl -n default apply -f - <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: my-sa-token
  annotations:
    kubernetes.io/service-account.name: my-sa
type: kubernetes.io/service-account-token
YAML

# 从 Secret 取出长期 token
TOKEN=$(kubectl -n default get secret my-sa-token -o jsonpath='{.data.token}' | base64 -d)
echo "$TOKEN"
```

**② 给权限（RBAC）**——新 SA 默认啥都干不了，要先授权：

```bash
# 给 my-sa 绑定 default 命名空间的 view 角色（只读）
kubectl -n default create rolebinding my-sa-view \
  --clusterrole=view --serviceaccount=default:my-sa
```

**③ 用 token curl**：

```bash
TOKEN=$(kubectl -n default create token my-sa)   # 或从 Secret 取长期 token
APISERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')

# GET Pod 列表（Authorization 头带 Bearer token，CA 用 kubeconfig 里的）
CA=$(mktemp)
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$CA"

curl -s --cacert "$CA" \
  -H "Authorization: Bearer $TOKEN" \
  "$APISERVER/api/v1/namespaces/default/pods" | jq .
#   {"kind":"PodList", ...}

# 没授权时会 403，授权后正常返回。验证 token 属于谁：
curl -s --cacert "$CA" -H "Authorization: Bearer $TOKEN" \
  "$APISERVER/api/v1/namespaces/default/pods" | jq -r '.metadata.selfLink' 2>/dev/null
rm -f "$CA"
```

> **token vs 证书怎么选**：证书 = 你的身份（管理员），权限大、长期有效；token = SA 身份，权限按 RBAC 收敛、可随时撤销（`kubectl create token` 签发的是短期的，过期自动失效）。**脚本/CI 优先用短期 token**，权限最小化。详见 [apiserver操作使用](apiserver操作使用.md) §2.2。

**预期输出**：`/version` 返回 `{"major":"1","minor":"3x",...}`；Pod 列表返回 `{"kind":"PodList",...}`。凭证**不用你生成**——Docker Desktop 已写好，方式二只是解出来用。token 方式要先建 SA + RBAC 才有权限。apiserver 只监听本机 `127.0.0.1`，curl 只能在宿主机跑。

---

## 3. 高手层：网络配置与这个环境的坑

### 3.1 网络拓扑：流量怎么到 Pod

```text
宿主机浏览器
  │  http://localhost:30080        ← NodePort 被绑到 localhost
  ▼
Docker Desktop 的网络代理/端口映射
  │
  ▼
节点容器（容器/VM 内；节点名 docker-desktop 老版 / desktop-control-plane 新版）
  ├─ kube-proxy 规则：NodePort 30080 → DNAT 到 Pod
  └─ Pod（ClusterIP 网络，如 10.1.0.x / 192.168.49.x）
```

**关键认知**：
- **NodePort / LoadBalancer → localhost 通**：Docker Desktop 自动把这类 Service 端口绑到宿主机回环地址。**绝大多数场景直接 `localhost:<port>` 就能访问**。
- **节点 INTERNAL-IP 从宿主机 ping 不通**：`kubectl get nodes -o wide` 看到的节点 IP（如 `192.168.49.2`）是 **Docker 内部 NAT 虚拟网段**，没进宿主机路由表。别硬拼 `NODE_IP:NodePort` 访问——**用 `localhost`**。这是最高频的坑。
- **LoadBalancer 无需 MetalLB**：Docker Desktop 内置了 LoadBalancer 支持（直接代理到 localhost），不像 kind 那样要装 MetalLB。

### 3.2 想让别的机器访问这个集群（进阶，一般不需要）

因为 apiserver 默认只听 `127.0.0.1`，跨机器访问要两步：

```bash
# ① 在 Docker Desktop 高级设置里改 kube-apiserver 启动参数（Edit Kubernetes yaml）：
#      --bind-address=0.0.0.0
#      --advertise-address=<宿主机局域网IP>
# ② 改 ~/.kube/config 的 server 为 https://<宿主机局域网IP>:<原端口>，再把这份 kubeconfig
#    拷到目标机器（同时要能解析/路由到该地址）。
#    原端口从本机 kubeconfig 读：kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}'
#    （老版是 6443；新版 kind 是随机端口，且重建集群后会变，需重新同步）
```

> ⚠️ **安全提醒**：把 apiserver 暴露到 `0.0.0.0` 等于把集群管理接口开到局域网，本地开发集群尚可，**千万别在生产/有敏感数据时这么干**。

### 3.3 四类情况全覆盖

**① 正常用法**：见 §1、§2。单节点跑单副本/少量副本应用完全够用。

**② 边界情况**：

| 边界 | 表现 | 处理 |
|---|---|---|
| 资源不够 | Pod `Pending`、`FailedScheduling: Insufficient cpu/memory` | 单节点资源有限，调低 `resources.requests`，或在 Docker Desktop Settings → Resources 加大 CPU/内存 |
| 单点故障 | 节点挂 = 整个集群挂 | 这是单节点固有属性，别指望"调度到别的节点" |
| 版本不匹配 | `kubectl version` 客户端/服务端差太多，报错 | 服务端版本由 Docker Desktop 定，要么升 Docker Desktop，要么换匹配的 kubectl |
| 多集群冲突 | `kubectl` 连错集群 | `kubectl config get-contexts` 看，`use-context docker-desktop` 切回 |

**③ 出错处理（这个环境高频报错）**：

| 报错 | 含义 | 定位/修法 |
|---|---|---|
| 一直卡 `Starting` / TLS 握手超时（**老版**） | `kubernetes.docker.internal` 解析失败或 6443 被占 | 查 hosts 有无 `127.0.0.1 kubernetes.docker.internal`；`lsof -iTCP:6443 -sTCP:LISTEN` 查是否被占；重置网络栈 |
| `Failed to connect ... port 6443`（**新版 kind**，高频） | **拿老版的固定地址 6443 去连，但新版端口是随机的** | 地址别写死，从 kubeconfig 读：`kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}'`（见 §0.1/§2.3） |
| `Unable to connect to the server`（kubectl 也连不上） | Docker Desktop 没起、K8s 没启用/没就绪、或 context 不对 | 确认 Docker Desktop 在跑且 K8s 状态为 Running；`kubectl config current-context` |
| Pod 一直 `Pending` | 资源不足（单节点 CPU/内存到顶） | Settings → Resources 加配额；或调低 requests |
| `ImagePullBackOff` | 本地没这个镜像且拉不到 | 检查镜像名/tag；私有镜像要先 `docker login` 或建 imagePullSecret |
| NodePort 访问不通 | 拼了 `NODE_IP:port` 而不是 localhost | **改用 `localhost:<NodePort>`**（见 §3.1） |
| 服务端/客户端版本不兼容 | 升级 Docker Desktop 后 | 版本随 Docker Desktop；必要时 Reset Kubernetes Cluster 重新部署 |
| 重启/重建集群后 kubectl 或 curl 突然连不上（**新版**） | **apiserver 随机端口变了**，kubeconfig 里还是旧端口 | 重新读 `$APISERVER`；kubectl 通常自动更新 kubeconfig，自己写的脚本要改成动态读 |

**④ 什么时候不该用它**：

- **别当生产或准生产**：单节点、随 Docker Desktop 启停、版本被锁死、无高可用。它只为**本地开发/测试/学习**。
- **要模拟多节点调度、节点亲和、真实负载均衡** → 用 kind（多节点）或 minikube，本环境只有一个节点测不出来。
- **要测跨节点网络策略、节点故障转移** → 同上，单节点测不了。
- **要长期跑、断电重启后自愈** → 这个环境依赖 Docker Desktop 起来；重启后若端口变了，`kind export kubeconfig` 那套是 kind 的，这里通常 `docker-desktop` context 会自动恢复，异常时 Reset Kubernetes Cluster 重来。

### 3.4 集群管理操作（Docker Desktop 侧）

| 操作 | 在哪 | 说明 |
|---|---|---|
| 启停集群 | Settings → Kubernetes → Start/Stop | 不想用时停掉，省资源 |
| 重置集群 | Settings → Kubernetes → Reset Kubernetes Cluster | 清掉所有工作负载，恢复初始 |
| 查看系统容器 | Settings → Kubernetes → Show system containers | 用 `docker ps` 能看到 kube-apiserver/etcd 等容器 |
| 删集群 | 取消勾选 Enable Kubernetes + Apply | 彻底移除 |

> 重置/删集群会**丢掉所有已部署的 Pod/Service/ConfigMap 等**，本地 `app.yaml` 记得留着，重置后重新 `kubectl apply`。

---

## 4. 验证与参考

### 4.1 代码/文档回核

本专题覆盖 Docker Desktop 内置 K8s 的**两种引擎形态**（见 §0.1）：

- **老版（kubeadm 引擎）**：节点 `docker-desktop`、固定地址 `kubernetes.docker.internal:6443`、context `docker-desktop`。对齐 Docker 官方文档与官方博客《How Kubernetes works under the hood》说明。
- **新版（kind 引擎）**：节点 `desktop-control-plane`、地址 `https://127.0.0.1:<随机高位端口>`、context 仍为 `docker-desktop`。**已实测**（v1.36.1，节点名 `desktop-control-plane`、端口 `58087`、curl `/version` 返回 200）：新版是 kind，端口随机且重建会变，`kubernetes.docker.internal` 域名不再使用。

两种形态通用的部分（context `docker-desktop`、NodePort/LoadBalancer 绑 localhost、凭证全自动、apiserver 只监听 127.0.0.1）一致。**本机实际版本/端口/网段一律以 `kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}'`、`kubectl get nodes`、`kubectl version` 实际输出为准**。

### 4.2 官方参考链接

| 主题 | 链接 |
|---|---|
| Docker Desktop Kubernetes 总览（启用/集群类型/操作） | [Explore the Kubernetes view](https://docs.docker.com/desktop/use-desktop/kubernetes/) |
| Docker Desktop Kubernetes 底层原理（证书/端点/context 命名） | [How Kubernetes works under the hood with Docker Desktop](https://www.docker.com/blog/how-kubernetes-works-under-the-hood-with-docker-desktop/) |
| Service 类型与访问（NodePort/LoadBalancer/ClusterIP） | [Service](https://kubernetes.io/docs/concepts/services-networking/service/) |
| 资源管理（requests/limits，避免单节点被吃满） | [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) |
| kubectl 命令总览 | [kubectl Cheat Sheet](https://kubernetes.io/docs/reference/kubectl/cheatsheet/) |
| apiserver 访问/凭证（通用，补本专题 §2.3） | [Controlling Access to the Kubernetes API](https://kubernetes.io/docs/concepts/security/controlling-access/) |

### 4.3 关联文档

- 通用 apiserver 操作（凭证/URL/建资源/监听）：[apiserver操作使用](apiserver操作使用.md)
- kubeadm 引导（Docker Desktop 内置集群就是它起的）：[19-kubeadm集群引导](../sourceReader/19-kubeadm集群引导.md)
- Service 流量如何到 Pod：[kube-proxy 规则生成](kube-proxy规则生成.md)、[16-kube-proxy网络代理](../sourceReader/16-kube-proxy网络代理.md)
- 一个 Pod 从创建到运行的完整链路：[02-kubectl-run全路径走读](../sourceReader/02-kubectl-run全路径走读.md)

[总目录](../sourceReader/README.md)
