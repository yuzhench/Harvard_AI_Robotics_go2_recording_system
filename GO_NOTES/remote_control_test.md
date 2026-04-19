# 笔记本无线遥控 Go2 站起/趴下 测试

用 `laptop/remote_control_client.py` + `scripts/remote_control_server.py`
两个脚本验证 **笔记本 → Jetson → Go2** 的遥控链路。

与录制 pipeline **相互独立**：
- 录制 pipeline（`record_daemon`）只**订阅**传感器数据（只读 DDS topic）
- 遥控脚本通过 `SportClient` **发送**运动命令
- 两者是 Jetson 上的**不同进程**，可以同时运行，互不干扰

## 1. 链路结构

```
┌─── 笔记本 ─────────────────┐
│  remote_control_client.py  │
│  (TCP client)              │
└──────────┬─────────────────┘
           │  TCP 9876  (WiFi 同网段 / Tailscale)
┌──────────▼─────────────────────────────────────┐
│  Jetson                                        │
│  remote_control_server.py (TCP 0.0.0.0:9876)   │
│    │                                           │
│    │ 解析按键 → 调 SportClient.StandUp/Down    │
│    ▼                                           │
│  Unitree SDK (DDS over eth0)                   │
└──────────┬─────────────────────────────────────┘
           │  DDS (rt/api/sport/request 等)
┌──────────▼─────────┐
│  Go2 主控板         │
│  (192.168.123.1)    │
│  执行运动规划        │
└─────────────────────┘
```

## 2. 运行前检查

| 项目 | 要求 |
|---|---|
| Go2 摆放 | 平整空旷地面（不是桌边 / 沙发） |
| 周围空间 | 2 m 内无人无物 |
| Go2 状态 | Sport Mode（官方遥控器或 App 已解锁，不在"Damping"） |
| 手机 App | **不要同时发运动命令**（会与 SportClient 抢） |
| 紧急停止 | 官方遥控器在手边 |
| 笔记本网络 | 与 Jetson 同网段（phswifi3 / Tailscale / USB ethernet 任一） |

## 3. 启动流程

### 3.1 Jetson 端：启 server

```bash
ssh unitree@10.100.206.170
cd ~/Desktop/go2_record_pipeline
conda activate go2
python scripts/remote_control_server.py --interface eth0
```

**期望输出**：
```
[*] Initializing Go2 SDK on interface 'eth0' ...
[*] SportClient ready.
[*] Listening on 0.0.0.0:9876  (waiting for laptop...)
```

终端挂起等待。保持这个 SSH session 不关。

**如遇 TLS block 错误**：
```
ImportError: ... libgomp.so.1: cannot allocate memory in static TLS block
```
用 `LD_PRELOAD` 预加载：
```bash
LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1 \
  python scripts/remote_control_server.py --interface eth0
```

### 3.2 笔记本端：连 client

**新开一个终端**（server 那个保持运行）：

```bash
cd ~/Desktop/Research/Harvard_AI/go2_record_pipeline
conda activate cameras   # 或任何装了 requests 的 env，其实此脚本无需 requests
python laptop/remote_control_client.py --host 10.100.206.170
```

**期望输出**：
```
[*] Connecting to 10.100.206.170:9876 ...
[*] Connected!
========================================
  Go2 Wireless Remote Control
========================================
  1   →   StandDown (crawl/lie)
  2   →   StandUp
  Q   →   Quit
========================================
```

可选的 `--host` 取值：

| 场景 | `--host` |
|---|---|
| 笔记本 + Jetson 都在 phswifi3 | `10.100.206.170` (Jetson DHCP IP，可能变) |
| 跨网络 / 笔记本不在 lab | `100.112.18.112` (Jetson Tailscale IP，固定) |
| 笔记本插 USB ethernet 到 Go2 | `192.168.123.18` |

## 4. 按键说明

client 切换到原始键盘模式，按键**立即生效**，不需要回车。

| 键 | 动作 | 建议 |
|---|---|---|
| **`2`** | StandUp（站起来） | **第一次测试用这个** —— 从趴/蹲到站，位移小 |
| **`1`** | StandDown（趴下 / crawl） | 等站稳后再按 |
| **`Q`** | 退出 client（server 继续活着） | 完成测试后按这个 |

## 5. 成功判据

按一次 `2` 后应同时出现：

- **client 终端**：`[>>] StandUp sent`
- **server 终端**：`[+] Connected: <laptop-ip>:xxxxx` + `[CMD] StandUp`
- **client 终端**：`[Go2] OK: StandUp`
- **Go2 物理动作**：站起来

任一项缺失就停下排查，**不要盲目继续按键**。

## 6. 排错

| 症状 | 根因 | 处理 |
|---|---|---|
| client 报 `Connection refused` | server 未启动 / 端口不对 | 确认 server 终端在 `Listening ...` 状态 |
| client 报 `Connection timed out` | 笔记本到 Jetson 网络不通 | `ping <host>` 先验证；换 `--host` 试别的路径 |
| client 报 `No route to host` | 同上 | 同上 |
| server 打印 `[CMD] StandUp` 但 Go2 不动 | Go2 不在 Sport Mode / 被 App 锁定 | 用遥控器或 App 切到 Sport Mode |
| server 报 `[ClientStub] send request error` | SDK 发 DDS 失败；多数是 `--interface` 错 | 必须是 `eth0`（Jetson 到 Go2 那张）；`ping 192.168.123.1` 验证 eth0 通 |
| 按键无反应 | client 终端没有焦点 | 点一下 client 终端窗口再按键 |
| server 刚启动就退出 | DDS 初始化失败 / LD_PRELOAD 缺 | 加 `LD_PRELOAD` 重启；`ip addr show eth0` 检查 IP |

## 7. 与 record_daemon 共存

两个进程可以同时运行：

```
Jetson:
  ┌── systemd 管的 record_daemon 进程 ──┐
  │   订阅 rt/lowstate / rt/utlidar/cloud │ ← 只读
  │   用 ChannelFactoryInitialize 过 1 次 │
  └────────────────────────────────────────┘

  ┌── 手动启动的 remote_control_server 进程 ──┐
  │   SportClient.StandUp/Down              │ ← 发命令
  │   用 ChannelFactoryInitialize 过 1 次    │
  └───────────────────────────────────────────┘
```

两个进程各自是一个 DDS domain participant，订阅/发布不同的 topic，
对 Go2 主控板来说是两个独立的 client。**不需要停 record_daemon**
来跑遥控测试。

唯一要避免的是**同时跑多个遥控命令源**（比如手机 App + 本脚本 +
键盘 keyboard_control.py），会在 Go2 主控侧竞争 SportClient 的命令队列。

## 8. 结束流程

1. 在 client 终端按 `Q` → client 退出
2. 回到 server 终端 `Ctrl-C` → server 退出
3. Go2 如果是站着的，用官方遥控器/App 让它趴下再断电

## 9. 相关文件

| 文件 | 位置 | 运行端 |
|---|---|---|
| `remote_control_server.py` | `scripts/` | Jetson |
| `remote_control_client.py` | `laptop/` | 笔记本 |
| 控制总架构 | `GO_NOTES/control_architecture.md` | — |
| 网络拓扑 | `GO_NOTES/network_topology.md` | — |
