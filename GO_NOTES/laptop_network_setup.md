# 笔记本（G14）— Go2 网线连接设置

目标：让笔记本通过 USB 以太网 dongle 稳定连到 Go2 内部网（`192.168.123.0/24`），
用来 SSH 进 Jetson（`192.168.123.18`），同时不影响 WiFi 走公网。

**一次配好永久有效**，插拔 dongle、重启笔记本都会自动恢复。

---

## 1. 网络拓扑

```
┌─────────────────┐                    ┌──────────────────┐
│  笔记本 G14     │                    │  Go2 (Jetson)    │
│                 │                    │                  │
│  wlp3s0 ── WiFi ─── 公网（走互联网）│  wlan0 ── WiFi ── phswifi3 / 公网
│                 │                    │                  │
│  enxa0cec87a92b1│── USB→网线 ───────→│  eth0            │
│  192.168.123.100│                    │  192.168.123.18  │
│                 │        DDS / SSH   │  (板载 PCIe)     │
└─────────────────┘                    └──────────────────┘
```

**关键原则**：
- `wlp3s0`（WiFi）负责公网（default route）
- `enxa0cec87a92b1`（USB 有线）**只负责** `192.168.123.0/24` 子网，不抢 default route
- 两个接口各走各的，互不干扰

---

## 2. 一次性设置命令

### 2.1 找到 USB 以太网 dongle 的接口名

```bash
ip link show | grep -E "enx|eth"
```

本机是 `enxa0cec87a92b1`（`enx` 开头 = ethernet + MAC 地址命名，USB 插拔不变）。
下面的命令都用这个名字；如果换 dongle MAC 变了，同步改一下。

### 2.2 创建 NetworkManager profile（静态 IP）

```bash
sudo nmcli connection add \
  type ethernet \
  ifname enxa0cec87a92b1 \
  con-name go2-link \
  ipv4.method manual \
  ipv4.addresses 192.168.123.100/24 \
  ipv4.never-default yes \
  ipv6.method disabled \
  connection.autoconnect yes
```

### 2.3 激活

```bash
sudo nmcli connection up go2-link
```

### 2.4 验证

```bash
ip addr show enxa0cec87a92b1     # 应该有 inet 192.168.123.100/24
ip route                          # default 应该只有一条，dev wlp3s0
ping -c 3 192.168.123.18          # Jetson，应该通
ping -c 3 8.8.8.8                 # 公网，也应该通（走 wlp3s0）
```

---

## 3. 参数含义（简短版）

| 参数 | 作用 |
|---|---|
| `type ethernet` | 有线连接 |
| `ifname enxa0cec87a92b1` | 绑定到这个 dongle |
| `con-name go2-link` | profile 名字，随便起但要好记 |
| `ipv4.method manual` | 静态 IP，不跑 DHCP（Go2 子网没 DHCP 给笔记本用） |
| `ipv4.addresses 192.168.123.100/24` | 笔记本在 Go2 子网里的 IP |
| `ipv4.never-default yes` | **关键**：这个接口永远不当 default route，公网流量继续走 WiFi |
| `ipv6.method disabled` | 关 IPv6，避免 NM 浪费时间跑 SLAAC |
| `connection.autoconnect yes` | 插上 dongle 自动激活 |

**为什么要 `never-default`**：如果没有这个，NM 会把 dongle 当成一条"普通"的网卡
并尝试给它装 default route。Go2 不是公网网关，流量走到 Go2 就死了，
你笔记本就上不了网。

---

## 4. 日常使用

### SSH 进 Jetson（网线路径）

```bash
ssh unitree@192.168.123.18
```

### SSH 进 Jetson（WiFi 路径，不依赖网线）

```bash
# 通过 Tailscale，100% 稳定
tailscale status                   # 找 Jetson 的 100.x.x.x
ssh unitree@100.112.18.112         # Jetson 的 Tailscale IP
```

**日常建议**：
- **开发/调试**：用 Tailscale，信号再弱都能进去，不受网线状态影响
- **跑 DDS 数据采集（IMU/joints/contacts）**：必须有网线到 Go2，因为 DDS 通信走 `192.168.123.x`
- **两个可以同时存在**，互不冲突

---

## 5. 常见坑

### 坑 1：`ip addr add` 的 IP 会消失

**千万不要用**这个方法：
```bash
sudo ip addr add 192.168.123.100/24 dev enxa0cec87a92b1   # ❌
```

它只往内核内存加地址，不持久化。**NetworkManager 定期刷新时会把它清掉**，
典型症状就是"SSH 用一会儿就掉线"。用上面的 `nmcli connection add` 方法才正确。

### 坑 2：shell 里的中文引号 `" "`

输入法切到中文时会自动把 `"` 变成 `“”`，nmcli 会报：
```
Error: unknown connection '"Wired'.
```

**解决**：敲命令前先切英文输入法，或者用反斜杠转义空格：
```bash
sudo nmcli connection modify go2-link ipv4.addresses 192.168.123.100/24   # 无空格的名字，最省心
# 如果 profile 名有空格：
sudo nmcli connection modify Wired\ connection\ 1 ...
```

**建议**：创建 profile 时起**不带空格**的名字（`go2-link`），以后敲命令不用引号。

### 坑 3：ping 报 `Destination Host Unreachable`

这是 **ARP 失败**，意思是笔记本找不到目标 IP 对应的 MAC。可能原因：

1. Jetson 的 eth0 没 IP（NM 把它搞掉了） → 需要通过 Tailscale 进 Jetson 跑 `sudo nmcli connection up "Wired connection 1"` 恢复
2. Go2 关机了 / 没供电 → 检查 Go2 电源
3. 网线松了 → `ip link show enxa0cec87a92b1` 看 `LOWER_UP` 在不在

**诊断顺序**：
```bash
# 笔记本上
ip addr show enxa0cec87a92b1             # IP 在不在
ip link show enxa0cec87a92b1             # LOWER_UP 在不在

# 通过 Tailscale 进 Jetson，看它的 eth0
ssh unitree@<jetson-tailscale-ip>
ip addr show eth0                         # 有没有 192.168.123.18
```

### 坑 4：两条 default route

如果某次看到：
```
default via 10.x.x.1       dev wlp3s0  metric 600
default via 192.168.123.1  dev enxa0cec87a92b1  metric 100    ← 不该有
```

说明 `ipv4.never-default yes` 没生效。检查：
```bash
nmcli -f ipv4.never-default connection show go2-link
# 应该输出 yes
```

如果是 `no`，重新 modify + down/up：
```bash
sudo nmcli connection modify go2-link ipv4.never-default yes
sudo nmcli connection down go2-link && sudo nmcli connection up go2-link
```

---

## 6. 修改 / 撤销

### 改 IP 或子网

```bash
sudo nmcli connection modify go2-link ipv4.addresses 192.168.123.101/24
sudo nmcli connection down go2-link && sudo nmcli connection up go2-link
```

### 临时停用（不想让笔记本自动连 Go2 子网）

```bash
sudo nmcli connection down go2-link
# 或彻底关闭 autoconnect：
sudo nmcli connection modify go2-link connection.autoconnect no
```

### 彻底删除

```bash
sudo nmcli connection delete go2-link
# 等于 rm /etc/NetworkManager/system-connections/go2-link.nmconnection
```

---

## 7. SSH 提速：加 keepalive（可选但推荐）

在 `~/.ssh/config` 里加一块：

```
Host go2
    HostName 192.168.123.18
    User unitree
    ServerAliveInterval 15
    ServerAliveCountMax 4
    TCPKeepAlive yes

Host go2-tailscale
    HostName 100.112.18.112
    User unitree
    ServerAliveInterval 15
    ServerAliveCountMax 4
    TCPKeepAlive yes
```

然后直接 `ssh go2`（走网线）或 `ssh go2-tailscale`（走 WiFi/Tailscale）。

- **`ServerAliveInterval 15`**：每 15 秒发一个心跳
- **`ServerAliveCountMax 4`**：连续 4 次没回（60 秒）才算断
- 好处：把"SSH 静悄悄冻住"变成明确的 "Broken pipe"，知道该重连

---

## 8. 速查：一键检查全套状态

```bash
echo "=== laptop interfaces ===" && ip -br addr
echo "=== laptop routes ===" && ip route
echo "=== go2-link status ===" && nmcli connection show go2-link | grep -E "autoconnect|ipv4\.(method|addresses|never-default)|GENERAL.STATE"
echo "=== ping jetson (cable) ===" && ping -c 2 -W 1 192.168.123.18
echo "=== ping 8.8.8.8 (wifi)  ===" && ping -c 2 -W 1 8.8.8.8
```

有这些输出就能一眼判断：网线、WiFi、路由、profile 是否都正常。

---

**相关文件**：
- 本机 profile：`/etc/NetworkManager/system-connections/go2-link.nmconnection`（root 可读）
- 对应的 Jetson 端设置：`GO_NOTES/setup_wifi_profile.md`
