# Jetson WiFi 自动连接 —— 配置笔记

这份笔记整理了 Jetson（装在 Go2 上）如何通过一个 shell 脚本一次性生成 NetworkManager profile，
从而在每次开机时自动连接 lab 的 **phswifi3** 企业 WiFi（WPA2-Enterprise / 802.1X），
以及相关的 `nmcli` 命令、配置文件格式和开机自动连的底层原理。

对应脚本：`scripts/setup_jetson_wifi.sh`

---

## 目录

1. [整体思路](#1-整体思路)
2. [Profile 是什么，存在哪](#2-profile-是什么存在哪)
3. [Shell 脚本逐段讲解](#3-shell-脚本逐段讲解)
4. [nmcli 命令参数详解](#4-nmcli-命令参数详解)
5. [变量展开 vs unset：密码到底去哪了](#5-变量展开-vs-unset密码到底去哪了)
6. [开机自动连的原理（时间线）](#6-开机自动连的原理时间线)
7. [常见问题 FAQ](#7-常见问题-faq)
8. [验证 / 调试命令速查](#8-验证--调试命令速查)

---

## 1. 整体思路

**目标**：Jetson 开机 + 插上 WiFi adapter → 不需要任何人工操作，自动连上 `phswifi3`，
不再需要每次都先用网线 SSH 进去手动配置。

**核心事实**：NetworkManager 本来就有开机自动连的机制，只要一个 profile 标记了
`autoconnect=true`，NM 启动时就会自动尝试。我们**不需要写任何守护进程、systemd unit 或 cron**。

**所以脚本只做一件事**：一次性生成 `phswifi3` 这个 profile 文件（幂等，重跑无副作用），
顺便处理 eth0 的路由修复。之后的所有自动连行为都交给 NetworkManager 本身。

---

## 2. Profile 是什么，存在哪

### 存储位置

所有 NetworkManager 的 connection profile 都存在：

```
/etc/NetworkManager/system-connections/<name>.nmconnection
```

每个 profile 一个文件。查看：

```bash
sudo ls -l /etc/NetworkManager/system-connections/
```

典型输出：
```
-rw------- 1 root root 412 ... Wired connection 1.nmconnection
-rw------- 1 root root 356 ... Yuzhen的iPhone.nmconnection
-rw------- 1 root root 589 ... phswifi3.nmconnection
```

**权限 600（`-rw-------`）**：只有 root 能读写，因为里面存着明文 WiFi 密码。
普通用户连文件内容都看不到。

### 文件格式（INI）

```bash
sudo cat /etc/NetworkManager/system-connections/phswifi3.nmconnection
```

大致长这样：

```ini
[connection]
id=phswifi3
uuid=a1b2c3d4-...
type=wifi
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=phswifi3

[wifi-security]
key-mgmt=wpa-eap

[802-1x]
eap=peap;
identity=你的用户名
password=你的明文密码
phase2-auth=mschapv2

[ipv4]
method=auto

[ipv6]
method=auto
```

**关键点**：文件里存的是**明文实际值**（`password=真实密码`），不是变量引用。
这是 NM 配置的**唯一事实来源**（source of truth）。
`nmcli` 命令本质上就是一个编辑这些文件的前端工具，底层都是在读写这个目录。

---

## 3. Shell 脚本逐段讲解

完整脚本见 `scripts/setup_jetson_wifi.sh`。下面按章节解释。

### 3.1 脚本头 + 严格模式

```bash
#!/usr/bin/env bash
set -euo pipefail
```

- **`#!/usr/bin/env bash`** — 告诉系统用 bash 解释。用 `env` 而非 `#!/bin/bash`
  是为了跨发行版兼容（有些系统 bash 不在 `/bin/`）。
- **`set -euo pipefail`** — bash "严格模式"，强烈推荐的惯用法：
  - `-e`：任何命令失败就立刻退出，不再继续（否则错误会被悄悄忽略）
  - `-u`：用到未定义的变量就报错（防拼写错误，`$PHS_USRE` 不会悄悄变空串）
  - `-o pipefail`：管道里任何一个命令失败整条管道就算失败
    （默认只看管道最后一个命令的返回值）

这三个加一起能挡住"脚本看起来跑成功了，其实中间出错了"这种最坑的情况。

### 3.2 配置常量

```bash
SSID="phswifi3"
EAP_METHOD="peap"
PHASE2_AUTH="mschapv2"
AUTOCONNECT_PRIORITY=10
```

把"可能要改的值"集中在脚本最上面，换 lab 或换 SSID 只改这几行，不用满脚本找。

| 变量 | 含义 |
|---|---|
| `SSID` | 要连的 WiFi 名字 |
| `EAP_METHOD=peap` | 802.1X 外层认证方法。企业 WiFi 99% 是 PEAP，备选 TTLS |
| `PHASE2_AUTH=mschapv2` | PEAP 里面套的内层认证，同样 99% 是 MSCHAPv2 |
| `AUTOCONNECT_PRIORITY=10` | 自动连优先级。**数字越大越优先**。默认 profile 是 0；设成 10 是为了当 phswifi3 和 iPhone 热点都在范围时，优先连 lab 网 |

### 3.3 前置检查

```bash
if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root. Re-run with: sudo bash $0" >&2
  exit 1
fi
```

- **`$EUID`** = effective user ID，root 是 0
- **为什么要 root**：`nmcli connection add` 要写 `/etc/NetworkManager/system-connections/`（mode 600，root only）
- **`>&2`** — 把错误信息输出到 stderr 而不是 stdout（惯例，方便和正常输出分开）

```bash
if ! command -v nmcli >/dev/null 2>&1; then
  echo "nmcli not found. Install network-manager first." >&2
  exit 1
fi
```

- **`command -v nmcli`** 查 nmcli 在不在 PATH 里
- **`>/dev/null 2>&1`** 把所有输出扔掉，只要返回值

```bash
if ! ip link show wlan0 >/dev/null 2>&1; then
  echo "wlan0 not found. Is the USB WiFi adapter plugged in?" >&2
  exit 1
fi
```

确认 `wlan0` 接口存在（即 WiFi adapter 插着并被内核识别了）。
提前拦住"忘插 adapter 就跑脚本"的情况。

### 3.4 交互式读凭据

```bash
read -rp "phswifi3 username: " PHS_USER
read -rsp "phswifi3 password: " PHS_PASS
echo
```

**`read`** 是 bash 内置的读输入命令：

- **`-r`** raw 模式：不把反斜杠 `\` 当转义字符（密码里可能有 `\`）
- **`-p "提示"`** 显示提示字符串
- **`-s`** silent 模式：**不回显**输入（密码输入不在屏幕显示）

读密码后要手动 `echo` 一个换行，因为 `-s` 连回车都不换行。

**为什么交互式而不是命令行传参？**

1. **安全**：`./setup.sh myuser mypass` 会把密码留在 `~/.bash_history`
2. **脚本可提交到 git**：凭据不在脚本里，这个 `.sh` 文件可以 commit 到仓库不泄密

### 3.5 删除旧 profile（幂等）

```bash
if nmcli -t -f NAME connection show | grep -qx "$SSID"; then
  echo "Existing '${SSID}' profile found — deleting and recreating..."
  nmcli connection delete "$SSID" >/dev/null
fi
```

**幂等（idempotent）**：脚本跑一次和跑十次效果一样，不会报错也不会产生重复。
场景：你第一次跑创建了 profile，后来密码改了想重跑脚本更新 —— 直接
`nmcli connection add` 会报 `already exists`，所以先检查并删除。

- **`nmcli -t -f NAME connection show`**
  - `-t` terse 模式，去掉表头和对齐空格（脚本友好）
  - `-f NAME` 只要 NAME 这一列
- **`grep -qx "$SSID"`**
  - `-q` quiet，只看返回值不打印
  - `-x` 整行完全匹配（防止 "phswifi" 误伤 "phswifi3"）

### 3.6 创建 profile（核心）

```bash
nmcli connection add type wifi ifname wlan0 \
  con-name "$SSID" \
  ssid "$SSID" \
  wifi-sec.key-mgmt wpa-eap \
  802-1x.eap "$EAP_METHOD" \
  802-1x.phase2-auth "$PHASE2_AUTH" \
  802-1x.identity "$PHS_USER" \
  802-1x.password "$PHS_PASS" \
  802-1x.system-ca-certs no \
  connection.autoconnect yes \
  connection.autoconnect-priority "$AUTOCONNECT_PRIORITY" >/dev/null

unset PHS_PASS
```

这段是脚本的核心，具体每个参数的含义见第 4 节。
`>/dev/null` 吞掉"successfully added" 的成功消息让输出更干净。
`unset PHS_PASS` 是小小的安全卫生习惯，见第 5 节。

### 3.7 eth0 路由修复

```bash
ETH_CON=$(nmcli -t -f NAME,DEVICE connection show | awk -F: '$2=="eth0"{print $1; exit}')
```

**自动检测 eth0 对应的 profile 名**。分解：

- `nmcli -t -f NAME,DEVICE connection show` 输出形如：
  ```
  Wired connection 1:eth0
  phswifi3:wlan0
  tailscale0:tailscale0
  ```
- `awk -F:` 用冒号分隔字段
- `'$2=="eth0" {print $1; exit}'` 第 2 列是 eth0 就打印第 1 列然后退出
- `$( ... )` 把命令输出捕获为变量

不写死 `"Wired connection 1"` 的原因：不同 Ubuntu/L4T 版本默认名可能是
`Wired connection 1` / `eth0` / `有线连接 1`，自动检测更稳。

```bash
if [[ -n "$ETH_CON" ]]; then
  CURRENT=$(nmcli -g ipv4.never-default connection show "$ETH_CON" || echo "")
  if [[ "$CURRENT" != "yes" ]]; then
    echo "Setting 'ipv4.never-default=yes' on '${ETH_CON}'..."
    nmcli connection modify "$ETH_CON" ipv4.never-default yes
    ...
```

- **`-n`** 字符串非空则为真
- **`-g <property>`** 只输出这一个属性的值（`-t -f` 的简写，脚本专用）
- **`|| echo ""`** 查询失败就用空串兜底，防止 `set -u` 触发
- 只有当前不是 `yes` 才改 —— **幂等**，已经对的就跳过

```bash
if nmcli -t -f NAME,DEVICE connection show --active | grep -q "^${ETH_CON}:eth0$"; then
  nmcli connection down "$ETH_CON" >/dev/null || true
  nmcli connection up   "$ETH_CON" >/dev/null || true
fi
```

**为什么要 down/up？** 因为 `nmcli connection modify` **只改磁盘配置**，不会自动
影响正在跑着的路由表。要让 `ipv4.never-default` 真正从路由表撤掉那条 default route，
必须走一次 down → up 的完整激活流程。

**为什么先判断 `--active`？** 如果 eth0 当前没连着（Jetson 离开 Go2 放桌上），
profile 不活跃，down/up 反而会报错。`--active` 只列当前活跃的连接。

**`|| true`** 保险：万一 down/up 出错，`set -e` 会让整个脚本崩，
而 phswifi3 主 profile 已经创建成功，我们不希望因为 eth0 折腾失败导致整个脚本失败。

---

## 4. nmcli 命令参数详解

### 4.1 创建 profile 的 `nmcli connection add`

| 参数 | 含义 |
|---|---|
| `type wifi` | 这是个 WiFi 连接（相对 ethernet / bluetooth / vpn） |
| `ifname wlan0` | 绑定到 wlan0 这个设备 |
| `con-name "$SSID"` | profile 的**显示名字**（NM 内部标识） |
| `ssid "$SSID"` | 要连的**实际 WiFi 名字**（空中广播的字符串） |
| `wifi-sec.key-mgmt wpa-eap` | 安全类型 = WPA-EAP (WPA-Enterprise)。普通家庭是 `wpa-psk` |
| `802-1x.eap peap` | EAP 外层方法 = PEAP |
| `802-1x.phase2-auth mschapv2` | PEAP 内层 = MSCHAPv2（用户名+密码） |
| `802-1x.identity` | 用户名 |
| `802-1x.password` | 密码 |
| `802-1x.system-ca-certs no` | 不强制验证服务器证书（见下面的说明） |
| `connection.autoconnect yes` | **开机自动连** |
| `connection.autoconnect-priority 10` | 优先级，数字越大越优先 |

**`con-name` vs `ssid` 的区别**：

- `ssid` = 空中广播的那个字符串（你手机看到的 WiFi 名）
- `con-name` = NM 里这个 profile 的标识，和 SSID 可以不一样

你可以给同一个 SSID 创建多个 profile（比如家里/公司两套不同密码），
每个 profile 的 con-name 不同但 ssid 相同。常见做法是让 con-name 和 ssid 一致
方便记忆。

**`802-1x.system-ca-certs no` 是安全妥协**：

正常情况下，客户端应该验证 RADIUS 服务器的 CA 证书，防止被假 AP 套密码
（有人在附近架一个同名的 AP 等你连上去）。但没有 IT 给的 CA 证书文件时，
先这样跑通。之后找 IT 要 CA 证书再加上：

```bash
sudo nmcli connection modify phswifi3 802-1x.ca-cert /etc/ssl/certs/<cert>.pem
sudo nmcli connection modify phswifi3 802-1x.system-ca-certs yes
```

### 4.2 其他常用 nmcli 命令

```bash
# 列所有保存的 profile
nmcli connection show

# 只看当前活跃的
nmcli connection show --active

# 看某个 profile 的详细配置
nmcli connection show phswifi3

# 修改某个属性
sudo nmcli connection modify phswifi3 <property> <value>

# 激活/断开
sudo nmcli connection up phswifi3
sudo nmcli connection down phswifi3

# 删除（等价于 rm 对应的 .nmconnection 文件）
sudo nmcli connection delete phswifi3

# 扫描 WiFi
sudo nmcli dev wifi list
sudo nmcli dev wifi rescan

# 禁止某 profile 自动连
sudo nmcli connection modify "Yuzhen的iPhone" connection.autoconnect no
```

---

## 5. 变量展开 vs unset：密码到底去哪了

**疑问**：脚本在 `nmcli ... password "$PHS_PASS"` 之后 `unset PHS_PASS`，
那以后系统连 WiFi 的时候变量不是没了吗？

**答案**：不会有问题。因为 **变量展开发生在命令执行前**。

### 时间线

```bash
PHS_PASS="MySecret123"
nmcli connection add ... 802-1x.password "$PHS_PASS"
unset PHS_PASS
```

**第 1 步**：bash 解析第二行，**在调用 nmcli 之前**，把 `$PHS_PASS` 替换成真实值。
这叫 *parameter expansion*。传给 nmcli 的 argv 是：

```
argv[...] = "802-1x.password"
argv[...] = "MySecret123"     ← 真实字符串，不是 "$PHS_PASS"
```

nmcli 眼里根本没有"变量"这个概念，它只看见一堆字符串参数。

**第 2 步**：nmcli 拿到明文 `"MySecret123"`，写进磁盘：

```
/etc/NetworkManager/system-connections/phswifi3.nmconnection
```

文件里长这样（**字面量，不是变量引用**）：

```ini
[802-1x]
password=MySecret123
```

**第 3 步**：`unset PHS_PASS` 执行。这时候：
- 磁盘上的配置文件：**没变**（已经写完了）
- nmcli 进程：**已经结束**，它持有的拷贝随进程消失
- 脚本内存里的 `$PHS_PASS` 变量：被清掉

**第 4 步**：开机 / 连 WiFi 时，NM 从磁盘读 `password=MySecret123` 用，
**完全不关心 shell 脚本**（脚本早跑完退出了）。

### 比喻

你往快递员手里塞了一张纸条，上面写着"密码是 MySecret123"。
快递员拿走后，你把原来那个记密码的小本子烧了。
这完全不影响快递员手里那张纸条 —— 它是个**拷贝**，已经独立存在了。

- `$PHS_PASS` = 你的小本子
- `nmcli connection add` = 塞给快递员
- `unset PHS_PASS` = 烧小本子
- `.nmconnection` 文件 = 快递员的纸条

### 什么时候 unset 会破事？

只有当你**之后还在同一个脚本里**想用这个变量才会出事：

```bash
PHS_PASS="MySecret123"
nmcli connection add ... "$PHS_PASS"
unset PHS_PASS                              # ← 清掉
# ...20 行后...
nmcli connection modify ... "$PHS_PASS"     # ← 这里会炸：unbound variable
```

我们的脚本把 `unset` 放在最后，后面不再用这个变量，所以安全。

### 对比：单双引号的展开

也是"即时"的：

```bash
name="world"
echo "hello $name"        # → hello world     （双引号里展开）
echo 'hello $name'        # → hello $name     （单引号里不展开，字面量）
```

脚本里用双引号 `"$PHS_PASS"`，就是让它在 nmcli 执行前展开。
如果错写成单引号 `'$PHS_PASS'`，nmcli 会真的去连一个密码叫 "$PHS_PASS"
字面六个字符的 WiFi，当然认证失败。

---

## 6. 开机自动连的原理（时间线）

```
t=0     Jetson 开机 / 加电
        ↓
        内核启动，加载 USB 驱动
        ↓
        ath9k_htc 驱动识别 AR9271 → /sys 里出现 wlan0
        ↓
        systemd 启动 NetworkManager.service
        ↓
        NM 读取 /etc/NetworkManager/system-connections/ 下所有 profile
        ↓
        发现 phswifi3   标记了 autoconnect=true, priority=10
        发现 Yuzhen的iPhone 标记了 autoconnect=true, priority=0
        ↓
        NM 让 wlan0 扫描空中所有 AP 的广播
        ↓
        根据扫描结果匹配：
          - phswifi3 在范围 → 优先级 10，最高，选它
          - Yuzhen的iPhone 在范围 → 优先级 0，备选
          - 都不在 → 继续后台扫描
        ↓
        选中 phswifi3，开始 WiFi 握手（用 profile 里的凭据）
        ↓
        握手成功 → DHCP 拿 IP → 公网连通
t≈15s   整个过程完成
```

**关键点**：

- **NM 是 system service**，开机就在后台跑，不依赖用户登录或 SSH
- **SSID 不在范围不会报错**，NM 会持续后台扫描（约每 2-3 分钟一次），
  一旦目标 SSID 出现就自动连。可以把 Jetson 装箱带回家一周，再到 lab 开机
  就会自动连，不用人干预
- **USB 枚举慢几秒没关系**，NM 监听 udev 事件，wlan0 一出现就对它应用 profile
- **同时多个候选**：看 `autoconnect-priority`，大的赢

---

## 7. 常见问题 FAQ

**Q1: profile 会一直留着吗？**
A: 是的。`/etc/NetworkManager/system-connections/phswifi3.nmconnection` 文件
持久存在，除非你手动 `nmcli connection delete` 或 `rm`。重启、断电、断网都不影响。

**Q2: 我怎么让某个 profile 不再自动连（但不删除）？**
```bash
sudo nmcli connection modify "Yuzhen的iPhone" connection.autoconnect no
```
profile 还在，但开机不会再尝试它。想连时手动 `nmcli connection up ...`。

**Q3: 我怎么彻底删除一个 profile？**
```bash
sudo nmcli connection delete phswifi3
# 等价于 rm /etc/NetworkManager/system-connections/phswifi3.nmconnection
```

**Q4: 我怎么实时看开机自动连的过程？**
开机后立刻 SSH 进去：
```bash
journalctl -u NetworkManager -b 0
```
`-b 0` 是"本次启动以来的日志"，能看到 NM 每一步：扫描、发现、握手、DHCP、成功。

**Q5: 脚本改过一次，重跑会怎样？**
安全。脚本是幂等的：检测到已存在的 `phswifi3` profile 会先删除再重建；
eth0 修复检测到已经是 `never-default=yes` 就跳过。随便跑几次都没副作用。

**Q6: 我人不在 Jetson 旁边也能先写好这个脚本吗？**
可以。脚本只在 Jetson 上跑。你可以在笔记本上 rsync 部署到 Jetson，然后
SSH 进去 `sudo bash scripts/setup_jetson_wifi.sh` 一次就搞定。

**Q7: 需要 CA 证书吗？**
严格说应该有（防假 AP）。没有 CA 证书的情况下脚本用 `802-1x.system-ca-certs no`
跳过验证，先跑通再说。拿到证书之后再补：
```bash
sudo nmcli connection modify phswifi3 802-1x.ca-cert /etc/ssl/certs/<cert>.pem
sudo nmcli connection modify phswifi3 802-1x.system-ca-certs yes
```

**Q8: 用户名要不要带域名后缀？**
要看 IT 的要求。常见三种格式：
1. `yuzhen`（裸用户名）
2. `yuzhen@partners.org`（UPN 格式）
3. `PARTNERS\yuzhen`（Windows 域格式，bash 里要写 `"PARTNERS\\yuzhen"`）

**先试裸用户名**，认证失败再依次换。

---

## 8. 验证 / 调试命令速查

### 看我当前连的是什么 WiFi

```bash
iwgetid -r                              # 纯 SSID，脚本友好
nmcli connection show --active          # 所有活跃连接
iw dev wlan0 link                       # 连接详情（信号、频率、速率）
```

### 看所有保存的 profile

```bash
nmcli connection show
sudo ls -l /etc/NetworkManager/system-connections/
```

### 看某个 profile 的详细配置

```bash
nmcli connection show phswifi3
# 敏感字段要 sudo 才能看
sudo nmcli --show-secrets connection show phswifi3 | grep -i password
```

### 看 profile 文件原文

```bash
sudo cat /etc/NetworkManager/system-connections/phswifi3.nmconnection
```

### 手动激活 / 断开 / 删除

```bash
sudo nmcli connection up phswifi3
sudo nmcli connection down phswifi3
sudo nmcli connection delete phswifi3
```

### 扫描 WiFi 列表

```bash
sudo nmcli dev wifi rescan
nmcli -f IN-USE,SSID,CHAN,SIGNAL,SECURITY dev wifi list
```

**channel ≤ 14** = 2.4GHz，**channel ≥ 36** = 5GHz。
AR9271 只能看到 2.4GHz 频段的 AP。

### 看路由表

```bash
ip route                                # 当前路由表
ip route get 8.8.8.8                    # 某个目的 IP 实际会走哪
ip route get 192.168.123.161            # Go2 走哪
```

理想状态：
- 只有一条 `default`，`dev wlan0`
- 一条 `192.168.123.0/24 dev eth0`（Go2 子网）
- 没有 `default ... dev eth0`（eth0 没抢公网）

### 看 NetworkManager 日志

```bash
# 实时 tail
journalctl -u NetworkManager -f

# 本次开机以来
journalctl -u NetworkManager -b 0

# 只看最近 1 小时
journalctl -u NetworkManager --since "1 hour ago"
```

### 测试连通性

```bash
ping -c 3 8.8.8.8                       # 公网，应该走 wlan0
ping -c 3 192.168.123.161               # Go2，应该走 eth0
```

---

## 附：部署流程（rsync + 运行）

在笔记本上写好/改好脚本后：

```bash
# 1. 笔记本上 rsync 到 Jetson（把路径换成你实际的 Jetson IP/hostname）
rsync -avz scripts/setup_jetson_wifi.sh unitree@<jetson-ip>:/tmp/

# 2. SSH 进 Jetson（先用网线或 iPhone 热点）
ssh unitree@<jetson-ip>

# 3. Jetson 上跑脚本
sudo bash /tmp/setup_jetson_wifi.sh
# 按提示输入 phswifi3 用户名和密码

# 4. 立即激活一次验证
sudo nmcli connection up phswifi3

# 5. 确认
nmcli connection show --active
ip route
ping -c 3 8.8.8.8
```

成功后，以后 Jetson **开机就自动连 phswifi3**，不用再折腾。

---

**相关文件**：
- 脚本本身：`scripts/setup_jetson_wifi.sh`
- 生成的 profile：`/etc/NetworkManager/system-connections/phswifi3.nmconnection`（仅 Jetson 上，root 可读）
