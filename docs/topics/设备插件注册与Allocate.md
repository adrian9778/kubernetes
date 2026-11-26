[上一篇](../sourceReader/20-CSI存储插件.md) · [总目录](../sourceReader/README.md) · [返回专题索引](../sourceReader/README.md#专题topics)

# 设备插件注册与 Allocate

> **层级**：跨篇机制纵深专题——把 kubelet **设备管理器（devicemanager）** 与**设备插件 gRPC 协议**讲透：三条 gRPC 通道（Register/ListAndWatch/Allocate）、设备分配进入 CRI config 的链路、与 CNI 的职责边界。
>
> **代码基准**：`master@782c049`（2026-11-19，工作区干净），Go 1.27.4。所有「符号+偏移」均在该基准上复核。
>
> **锚点记法**：函数内偏移以函数声明行为 **+0**，如 `m.Allocate+0`。
>
> **事实红线**：生产实测与代码推断分开标注；未实测项标 `[待运行验证]`。
>
> **与主线篇的关系**：[21-设备插件与网络边界](../sourceReader/21-设备插件与网络边界.md) 是浓缩版；本专题纵向展开三条 gRPC 通道与 Allocate→CRI 链路。

---

## 0. 结论速览

### 0.1 设备插件解决什么问题

让厂商**不改 kubelet 代码**就能暴露特殊硬件（GPU、FPGA、SR-IOV 网卡等）给 Pod。厂商实现 `DevicePlugin` gRPC 接口，向 kubelet 注册并推送设备列表；kubelet 分配设备给容器。

### 0.2 三条 gRPC 通道（核心）

| 通道 | 方向 | 模式 | 作用 |
|---|---|---|---|
| **Register** | 插件 → kubelet | 一元 | 插件主动向 kubelet 注册（上报 socket + resourceName） |
| **ListAndWatch** | kubelet → 插件 | 服务端流式长连接 | 插件持续推送设备列表与健康状态 |
| **Allocate** | kubelet → 插件 | 一元（按需） | kubelet 为容器分配具体设备 |

**关键方向反转：** Register 时**插件是 client、kubelet 是 server**；ListAndWatch/Allocate 时**kubelet 是 client、插件是 server**。

### 0.3 关键结论

1. **kubelet 不直接调 CNI**：`Allocate` 把设备传给 CRI，`RunPodSandbox` 把网络参数交给运行时，CNI 在运行时容器内生效。
2. **设备进入容器**：`GetDeviceRunContainerOptions` 产出 `DeviceRunContainerOptions{Devices, Mounts, Envs, Annotations, CDIDevices}` 进入 CRI。
3. **分配状态持久化**：`podDevices` 记录分配 + 写 checkpoint（kubelet 重启恢复）。
4. **ListAndWatch 故意用 `context.TODO()`**：使长连接不随单次操作取消。

---

## 1. 生产视角：对外行为

### 1.1 设备插件的生命周期

| 阶段 | 动作 | 可见结果 |
|---|---|---|
| 插件启动 | 向 `/var/lib/kubelet/plugins_registry/<socket>` Register | kubelet 建 endpoint + 启动 ListAndWatch |
| 设备上报 | 插件 ListAndWatch 推送设备列表 | 节点上报扩展资源容量（如 `nvidia.com/gpu`） |
| Pod 调度到节点 | kubelet admit 阶段 `Allocate` | `podDevices` 记录分配 + checkpoint |
| 创建容器 | `GetDeviceRunContainerOptions` | 设备/Mounts/Envs 进入 CRI |

### 1.2 可观测信号

- 节点 `status.allocatable` 出现扩展资源（如 `nvidia.com/gpu: 8`）。
- Pod 事件：设备分配失败（`FailedScheduling`/admit 失败）。

---

## 2. 开发视角：设备管理器骨架

### 2.1 `ManagerImpl`（devicemanager/manager.go）

`NewManagerImpl+0`（:147）→ `newManagerImpl+0`（:155，内部）。`Manager` 接口（types.go:38）定义 `Allocate`/`AllocatePod`/`GetDeviceRunContainerOptions`/`GetCapacity`/`UpdateAllocatedDevices`。

### 2.2 `Start`（manager.go `m.Start+0`，:399）

启动 gRPC server 监听 `/var/lib/kubelet/device-plugins/kubelet.sock`（供插件 Register），并恢复已分配设备（checkpoint）。

---

## 3. 开发视角：三条 gRPC 通道

### 3.1 通道 1：Register（插件 → kubelet）

**插件侧**起 gRPC server：`NewServer+0`（plugin/v1beta1/server.go :67）→ `s.Start+0`（:86），注册 `api.RegisterRegistrationServer`。
**kubelet 侧**实现 Register handler：`s.Register+0`（server.go :159）——校验 socket 路径、version，建立 endpoint 并启动 ListAndWatch。

**语义：** 插件**主动找 kubelet**（插件是 client）。注册后方向反转。

### 3.2 通道 2：ListAndWatch（kubelet → 插件，流式长连接）

- kubelet 调插件的 `ListAndWatch`（client.go :86），**服务端流式**——插件持续推送 `ListAndWatchResponse{Devices[]}`（设备 ID + 健康状态）。
- kubelet 阻塞 `Recv()` 接收，每次 response 经 `PluginListAndWatchReceiver+0`（manager.go :322）→ `genericDeviceUpdateCallback`（:326）更新节点容量与设备健康。
- **`context.TODO()`（:86，FIXME 注释）：** 故意不传可取消 context，使 ListAndWatch 不随单次操作取消。

### 3.3 通道 3：Allocate（kubelet → 插件，按需）

Pod 调谐到节点、admit 阶段，kubelet 调 `m.Allocate+0`（manager.go :425）→ 经 endpoint `e.allocate+0`（endpoint.go :109）调插件的 Allocate gRPC，拿到设备分配结果。`preStartContainer+0`（:121）在容器启动前做插件侧准备。

---

## 4. 开发视角：Allocate → CRI 链路

### 4.1 `Allocate`（manager.go `m.Allocate+0`，:425）

```text
Allocate(ctx, pod, container, operation)            m.Allocate+0
├── 按容器的扩展资源请求（extended resources）选设备
├── 调插件 endpoint.allocate(devs)                   e.allocate+0 (endpoint.go:109)
│     ↑ 插件返回 AllocateResponse（设备 ID、挂载、env、annotations）
├── 记录 podDevices（分配状态）
└── 写 checkpoint（持久化，kubelet 重启恢复）
```

### 4.2 `GetDeviceRunContainerOptions`（manager.go `m.GetDeviceRunContainerOptions+0`，:1085）

创建容器前，把已分配设备转成 CRI 可用的运行选项：
```text
GetDeviceRunContainerOptions(ctx, pod, container)   m.GetDeviceRunContainerOptions+0
└── 返回 DeviceRunContainerOptions{Devices, Mounts, Envs, Annotations, CDIDevices}
```

### 4.3 进入 CRI config（kuberuntime_container.go）

`DeviceRunContainerOptions` 经 `makeDevices`（kuberuntime_container.go `makeDevices+0`，:498）转成 CRI `runtimeapi.Device`，`makeCDIDevices+0`（:514）转 CDI 设备——进入 `CreateContainer` 的 CRI config。

---

## 5. 与 CNI 的职责边界

### 5.1 设备 vs 网络：两条独立链路

| | 设备（device plugin） | 网络（CNI） |
|---|---|---|
| 交互方 | kubelet ↔ 设备插件（gRPC） | 运行时 ↔ CNI 插件（exec） |
| 触发 | `Allocate`（admit 阶段） | `RunPodSandbox`（建 sandbox） |
| 进入 | CRI `CreateContainer` config 的 devices | CRI `RunPodSandbox` 的 PodSandboxConfig 网络参数 |

**kubelet 不直接调 CNI：** kubelet 只把网络参数（`DnsConfig`、`PortMappings`）写进 `RunPodSandbox` 的 `PodSandboxConfig`，CNI 由容器运行时（containerd/CRI-O）调用。

---

## 6. 全链路时序（GPU Pod 分配）

```text
GPU Pod 调度到节点
├── admit 阶段：m.Allocate (manager.go:425)
│    └── endpoint.allocate (endpoint.go:109) → 插件 Allocate gRPC
│         └── AllocateResponse（GPU 设备 ID + env + mounts）
│              └── podDevices 记录 + checkpoint
└── 创建容器：m.GetDeviceRunContainerOptions (manager.go:1085)
     └── DeviceRunContainerOptions{Devices...}
          └── makeDevices (kuberuntime_container.go:498) → CRI runtimeapi.Device
               └── CreateContainer config → 容器拿到 GPU
```

---

## 7. 验证与边界

### 7.1 代码回核

本专题所有「符号+偏移」均在 HEAD `782c049` 上复核：
- `NewManagerImpl+0`（manager.go:147）、`newManagerImpl+0`（:155）、`m.PluginListAndWatchReceiver+0`（:322）、`m.Start+0`（:399）、`m.Allocate+0`（:425）、`m.GetCapacity+0`（:511）、`m.UpdateAllocatedDevices+0`（:627）、`m.GetDeviceRunContainerOptions+0`（:1085）、`m.AllocatePod+0`（:1257）
- `newEndpointImpl+0`（endpoint.go:53）、`e.allocate+0`（:109）、`e.preStartContainer+0`（:121）
- 插件 server：`pkg/kubelet/cm/devicemanager/plugin/v1beta1/server.go` · `NewServer+0`、`s.Start+0`（:86）、`s.Register+0`（:159）
- CRI：`makeDevices+0`（kuberuntime_container.go:498）、`makeCDIDevices+0`（:514）

### 7.2 生产实测

**未实测** `[待运行验证]`。三条 gRPC 通道实际握手、Allocate 设备选择、checkpoint 恢复均为代码推断，未在真实 GPU 节点抓 gRPC/查 checkpoint 验证。

### 7.3 已知缺口

| 缺口 | 性质 | 说明 |
|---|---|---|
| topology hints | 未深挖 | `topology_hints.go` 的 NUMA 拓扑提示未展开 |
| device 健康与重注册 | 未深挖 | 插件断连重注册（`TestDevicePluginReRegistration`）细节未读 |
| checkpoint 格式 | 未深挖 | `checkpoint/checkpoint.go` 的持久化格式未读 |
| CDI 设备 | 未深挖 | `makeCDIDevices` 的 CDI 规范细节未展开 |

### 7.4 修改联动

- 改 Register 校验 → `s.Register`（server.go:159），同步本文 §3.1。
- 改分配 → `m.Allocate`/`e.allocate`，同步 §4.1。
- 改 CRI 集成 → `makeDevices`/`makeCDIDevices`，同步 §4.3。

**关联文档：** [21-设备插件与网络边界](../sourceReader/21-设备插件与网络边界.md)（浓缩版）、[kubelet同步循环与PLEG](kubelet同步循环与PLEG.md)（kubelet 主循环）、[api-server](api-server.md)（扩展资源上报）。

[总目录](../sourceReader/README.md)
