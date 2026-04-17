# 网络拓扑 & 数据链路

这份笔记说明**笔记本、Jetson、Go2 本体**三者之间的网络如何连接，
以及 SSH 流量和 DDS 流量在物理链路上是怎么走的。

写这份笔记的起因：容易对"eth0"产生困惑 —— 感觉"笔记本通过网线 SSH 走 eth0"
和"Jetson 通过 DDS 控制 Go2 也走 eth0"重复了，其实它们是**同一张网卡的两种用途**。

---

## 1. 物理拓扑

**关键事实**：Go2 机器狗内部有一个**小型以太网交换机**，像家用路由器的 LAN 口一样。
挂在这个交换机上的"设备"有三个：

1. **Go2 主控板**（运动控制板，IP `192.168.123.1`）
2. **Jetson 模块的 eth0**（IP `192.168.123.18`）—— 装 Jetson 的时候就接在内部交换机上
3. **狗身上外部露出的一个网口** —— 你笔记本插 USB dongle 连过来的那个口

```
                     ┌─────────────────────────────────────┐
                     │            Go2 本体（狗）            │
                     │                                      │
                     │   Go2 主控板                         │
                     │    (192.168.123.1)                   │
                     │        │                             │
                     │        ▼                             │
                     │   ┌─────────────┐                    │
                     │   │ 内部 L2      │                   │
                     │   │ 交换机       │                   │
                     │   └──┬───────┬──┘                    │
                     │      │       │                       │
                     │      ▼       ▼                       │
                     │  [Jetson]  [狗身外部网口]            │
                     │   eth0                               │
                     │  192.168.123.18                      │
                     └─────────────┬────────────────────────┘
                                   │
                                   │ 你笔记本插的那根网线
                                   │
                                   ▼
                          ┌─────────────────┐
                          │   笔记本 G14    │
                          │ enxa0cec87a92b1 │
                          │ 192.168.123.100 │
                          └─────────────────┘
```

三个设备（**主控板、Jetson、笔记本**）都在**同一个 L2 广播域**上，也就是
**同一个 `192.168.123.0/24` 网络**里。它们互相可以看到，可以 ping，可以 ARP。

---

## 2. Jetson 身上只有**一张** eth0

这是**最容易混淆**的地方。Jetson 并不是"一个 eth0 用于 SSH，另一个 eth0 用于 DDS"，
它从头到尾只有一张板载 PCIe 以太网卡（别名 `enP8p1s0`）。

这张卡承担**两种不同的角色**：

| 角色 | 目标 IP | 作用 |
|---|---|---|
| **SSH 接收端** | `192.168.123.18`（自己）| 监听 22 端口，接受笔记本进来的 SSH 连接 |
| **DDS 发送端** | `192.168.123.1`（Go2 主控）| Unitree SDK 通过这张卡把控制命令送到 Go2 |

好比一家店门口的门 —— **同一扇门**，既让客人进来，也让快递出去。

---

## 3. 两种流量的完整链路

### 3.1 流量 A：笔记本 SSH 进 Jetson

```
笔记本 enxa0cec87a92b1 (192.168.123.100)
    │
    │ 以太网帧
    ▼
狗身外部网口
    │
    │ 进入 Go2 内部 L2 交换机
    ▼
交换机根据 MAC 地址查表，转发给 Jetson eth0
    │
    ▼
Jetson eth0 (192.168.123.18)
    │
    │ 内核收到 TCP 包，检查目标 IP = 自己
    ▼
sshd 进程 accept()，建立 SSH session
```

关键点：数据从笔记本出来，**经过 Go2 的内部交换机**到达 Jetson。Go2 主控板
也能看到这些包，但因为目标 MAC 不对（是 Jetson 的 MAC），交换机只会往 Jetson 那条线转发。

### 3.2 流量 B：Jetson DDS 控制 Go2

```
Jetson 上的 Unitree SDK
    │
    │ client.StandUp() → DDS publisher
    ▼
Jetson eth0 (192.168.123.18)
    │
    │ 以太网帧从 eth0 发出
    ▼
Go2 内部 L2 交换机
    │
    │ 按 MAC 转发给 Go2 主控板
    ▼
Go2 主控板 (192.168.123.1)
    │
    │ DDS subscriber 收到，触发运动规划
    ▼
电机驱动 → 狗真的站起来 / 趴下
```

关键点：DDS 包**没有经过笔记本**，完全是 Jetson 和 Go2 主控之间的内部对话。
就算笔记本的网线拔了，这条链路依然工作（只要 Jetson 开着电、内部交换机在）。

### 3.3 完整按键路径（按 "2" 让狗站起来）

合起来看从你按键到狗响应的完整路径：

```
你按 "2"
    │
    ▼
笔记本 client (Python)
    │
    │ TCP 发 "2\n"
    ▼
笔记本 tailscale0 (100.65.27.1)  或  enxa0cec87a92b1 (192.168.123.100)
    │                                       │
    │ 走 Tailscale 加密隧道                  │ 走狗内部交换机
    │ (公网/VPN 网络)                        │ (同一 L2)
    ▼                                       ▼
Jetson 某个网卡（tailscale0 或 eth0 收到 TCP）
    │
    ▼
remote_control_server.py 监听 0.0.0.0:9876，收到 "2"
    │
    │ 调用 client.StandUp()
    │ SDK 翻译为 DDS request
    ▼
Jetson eth0 (192.168.123.18)  ← 因为 SDK 被初始化在 eth0 上
    │
    │ 走 Go2 内部 L2 交换机
    ▼
Go2 主控板 (192.168.123.1)
    │
    │ 执行运动规划
    ▼
Go2 电机动起来 → 狗站起来
```

**注意 Jetson eth0 在路径中段出现**，它既收 TCP 又发 DDS —— 因为只有这一张卡。

---

## 4. 接口名字速查

不同机器上的接口名不同，**不要把不同机器的同名接口混起来**：

| 位置 | 接口名 | IP | 作用 |
|---|---|---|---|
| **笔记本** | `wlp3s0` | `10.x.x.x` (家里/lab WiFi) | 公网访问（走 default route） |
| **笔记本** | `enxa0cec87a92b1` | `192.168.123.100` | USB 以太网 dongle，连到 Go2 外部网口 |
| **笔记本** | `tailscale0` | `100.65.27.1` | Tailscale VPN，用于从公网进 Jetson |
| **Jetson** | `wlan0` | `10.100.x.x` (phswifi3) | 公网 + 给笔记本提供非网线路径 |
| **Jetson** | `eth0` (`enP8p1s0`) | `192.168.123.18` | 板载 PCIe，到 Go2 内部交换机 |
| **Jetson** | `tailscale0` | `100.112.18.112` | Tailscale VPN |
| **Go2 主控** | (内部，不可见) | `192.168.123.1` | DDS subscriber |

**重点**：
- 笔记本上的"网线"叫 `enxa0cec87a92b1`（USB dongle 的 MAC-based 名字），**不叫 eth0**
- Jetson 上的"网线"叫 `eth0`
- 虽然用户都习惯说"走网线"，底层硬件和驱动是不同的

---

## 5. 为什么 `--interface` 必须是 `eth0`

`remote_control_server.py --interface <X>` 这个参数**只影响 Unitree SDK 的 DDS 层**，
告诉 SDK "发 DDS 包用哪张卡"。代码里是这一行：

```python
ChannelFactoryInitialize(0, args.interface)
```

它**不是**告诉 server 监听哪张卡（server 永远监听 `0.0.0.0:9876`，所有卡都行）。

所以：

| 传什么 | 效果 |
|---|---|
| `--interface eth0` | ✅ DDS 包从 Jetson eth0 出去 → 到 Go2 内部交换机 → 到 Go2 主控板 |
| `--interface wlan0` | ❌ DDS 包从 Jetson wlan0 出去 → 到 phswifi3 → Go2 不在这个网里 → `send request error` |
| `--interface tailscale0` | ❌ DDS 包进 Tailscale 隧道 → 公网 → Go2 不在 tailnet 上 → 发不出去 |

**唯一正确答案**：`eth0`，因为 Go2 主控板**只存在于** Jetson 的 eth0 所在的那个 L2 网络里。

---

## 6. 小结（给自己一句话）

> Jetson 只有一张 eth0，它同时承担 "接收来自笔记本的 SSH" 和 "向 Go2 主控发 DDS" 两个角色。
> 因为笔记本、Jetson、Go2 主控都接在 **Go2 内部的同一个 L2 交换机**上，所以都是
> `192.168.123.0/24` 子网里的邻居。`--interface eth0` 是给 SDK 用的，和 SSH 走哪条路无关。

---

**相关文件**：
- 笔记本端的网络设置：`GO_NOTES/laptop_network_setup.md`
- Jetson 端的 WiFi 设置：`GO_NOTES/setup_wifi_profile.md`
