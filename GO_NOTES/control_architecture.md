# 录制控制架构

**笔记本（主控）** ↔ **Jetson daemon（watchdog）** ↔ **Go2 本体** 三层控制结构。
主控发出一条开始指令，笔记本和 Jetson 同时启动录制；主控发出一条停止指令，
两边同时停止并各自存盘；录制结束手动 rsync 合并数据。
daemon 开机自启、永不退出，支持反复录制。

---

## 1. 系统架构图

```
┌──────────────────────── 笔记本 (G14) ────────────────────────┐
│                                                              │
│   laptop/master_record.py   ── 主控 CLI                       │
│         │                                                    │
│         ├──→ POST http://localhost:8000/start|/stop          │
│         │     camera_record_pipeline FastAPI                 │
│         │     (4× RealSense, 本地)                            │
│         │                                                    │
│         └──→ POST http://<jetson-ip>:8010/start|/stop        │
│               Jetson record_daemon FastAPI                   │
│                                                              │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP over phswifi3 / Tailscale
┌──────────────────────────┼───────────────────────────────────┐
│                          ▼                                   │
│                Jetson record_daemon (systemd 管理)            │
│                                                              │
│   开机自启 → ChannelFactoryInitialize(0, "eth0") 一次          │
│              监听 :8010, 常驻                                 │
│                                                              │
│     POST /start → Recorder.start()                           │
│                     ├── IMUCollector.start()        (500 Hz) │
│                     ├── EgoCameraCollector.start()  (30 Hz)  │
│                     └── LidarCollector.start()     (~11 Hz)  │
│                                                              │
│     POST /stop  → Recorder.stop() + save() NPZ/MP4 到本地盘  │
│                                                              │
│                          │ DDS over eth0                      │
│                          ▼                                   │
│                      Go2 本体 (192.168.123.1)                 │
└──────────────────────────────────────────────────────────────┘
```

**设计原则**：
- 一个进程包含 HTTP server、全部 collectors 与 DDS 通信
- 多线程，不开子进程（uvicorn worker + DDS callback + camera capture 共享同一 Python 进程）
- DDS 在进程启动时初始化一次（`ChannelFactoryInitialize` 的每进程一次限制）
- 数据分别落盘：camera 文件存笔记本，robot 文件存 Jetson；`<task>/<date>/<time>/`
  三级路径对称，rsync 后天然合并

---

## 2. 文件结构

```
go2_record_pipeline/
│
├── utility/                        # 采集器库（in-process，线程封装在类内部）
│   ├── imu.py                      # LowState (IMU + joints + foot)   500 Hz
│   ├── egocentric_camera.py        # Go2 前相机                        30 Hz
│   └── lidar.py                    # rt/utlidar/cloud                  ~11 Hz
│
├── jetson/                         # Jetson 端（daemon 服务）
│   ├── config.py                   # DATA_ROOT, TASKS, PORT, INTERFACE
│   ├── recorder.py                 # Recorder 类，编排三个 collector
│   ├── record_daemon.py            # FastAPI :8010 入口，由 systemd 启动
│   ├── record_daemon.service       # systemd unit 文件
│   └── install_daemon.sh           # 一键安装 systemd 服务
│
├── laptop/                         # 笔记本端（主控 CLI 和客户端工具）
│   ├── master_record.py            # 主控 CLI (R=录, S=停, Q=退)
│   └── remote_control_client.py    # Go2 动作遥控客户端
│
├── scripts/                        # 独立工具脚本（debug / 辅助）
│   ├── test_collectors.py          # 不走 daemon 直接测 collector
│   ├── remote_control_server.py    # Go2 动作遥控服务端（在 Jetson 上跑）
│   ├── keyboard_control.py
│   └── setup_jetson_wifi.sh
│
├── unitree_sdk2_python/            # SDK submodule
├── GO_NOTES/                       # 架构 / 运维笔记
└── data/                           # 本地测试输出（生产数据在 /home/GO2_DATA）
```

**分层职责**：

| 层 | 目录 | 角色 |
|---|---|---|
| 采集库 | `utility/` | 传感器订阅 + buffer + 写盘，无状态的 Python 类 |
| 编排 | `jetson/recorder.py` | 把 collectors 组装为一次录制会话 |
| 服务 | `jetson/record_daemon.py` | FastAPI 暴露 HTTP 接口，systemd 守护 |
| 主控 | `laptop/master_record.py` | CLI 同时驱动 camera pipeline 和 Jetson daemon |
| Debug | `scripts/` | 不依赖 daemon 的快速测试入口 |

---

## 3. 数据路径

**录制期间**，两台机器各写各的盘，路径完全对称：

```
笔记本：/home/GO2_DATA/<task>/<MM_DD_YYYY>/<HH_MM_SS>/
          ├── cam1/  cam2/  cam3/  cam4/          (4× RealSense)
          └── prompt.txt                           (camera pipeline 写入)

Jetson：/home/GO2_DATA/<task>/<MM_DD_YYYY>/<HH_MM_SS>/
          ├── imu.npz                              500 Hz  quaternion/gyro/accel/rpy
          ├── joints.npz                           500 Hz  q/dq/tau (N×12)
          ├── contacts.npz                         500 Hz  foot_force (N×4)
          ├── ego_rgb.mp4                          30 Hz   H.264
          ├── ego_timestamps.npy
          ├── lidar/000000.npy ...                 ~11 Hz  (N, 4) float32
          ├── lidar_timestamps.npy
          └── lidar_meta.json
```

**录制后手动 rsync**，Jetson 数据合并进笔记本的同一 session 目录：

```
   笔记本 /home/GO2_DATA/task1/04_15_2026/14_30_00/
   ┌─────────────┐            rsync             ┌──────────────────┐
   │ cam1/       │      ←──────────────────     │ imu.npz          │
   │ cam2/       │                              │ joints.npz       │
   │ cam3/       │   合并到同一 session 目录     │ contacts.npz     │
   │ cam4/       │                              │ ego_rgb.mp4      │
   │ prompt.txt  │                              │ lidar/           │
   └─────────────┘                              └──────────────────┘
                                                   Jetson /home/GO2_DATA/
                                                   task1/04_15_2026/14_30_00/
```

rsync 命令（在笔记本上执行）：

```bash
rsync -av unitree@<jetson-ip>:/home/GO2_DATA/ /home/GO2_DATA/
```

因为两边 `<task>/<date>/<time>` 三级路径一致，rsync 自动把 Jetson 的文件填到
笔记本已有的 session 目录里，不覆盖 cam 文件。

**首次安装时的权限设置**（Jetson 上一次性执行）：

```bash
sudo mkdir -p /home/GO2_DATA
sudo chown -R unitree:unitree /home/GO2_DATA
```

---

## 4. HTTP API 契约

Jetson daemon 的接口与 camera_record_pipeline 对称，`master_record.py` 用相同的
`requests.post()` 模式调用两边。

```
POST http://<jetson-ip>:8010/start
     body : {"task": "task1", "prompt": "Pick up the cup"}
     200  : {"status": "started",
             "session_dir": "/home/GO2_DATA/task1/04_15_2026/14_30_00"}
     400  : {"detail": "Unknown task: foo"}
     409  : {"detail": "Already recording"}

POST http://<jetson-ip>:8010/stop
     200  : {"status": "stopped",
             "session_dir": "...",
             "elapsed_seconds": 12.5,
             "samples": {"imu": 6250, "ego_frames": 375, "lidar_scans": 138}}
     409  : {"detail": "Not recording"}

GET  http://<jetson-ip>:8010/status
     200  : {"recording": true|false,
             "elapsed": 12.5,
             "samples": {...},
             "uptime_s": 3600,
             "current_session": "..." | null,
             "interface": "eth0"}
```

字段与 camera_record_pipeline 的 `/start /stop /status` 对齐，
仅 `samples` 的 key 不同（camera 是 `frame_count`，Jetson 是 `imu / ego_frames / lidar_scans`）。

---

## 5. 笔记本到 Jetson 的通信方式

三条可用路径：

```
┌──────────────┐                              ┌──────────────┐
│   笔记本     │ ─── 1. phswifi3 直连 ─────→  │   Jetson     │
│              │     10.100.206.170:8010      │              │
│  master_     │                              │  record_     │
│  record.py   │ ─── 2. Tailscale ──────────→ │  daemon      │
│              │     100.112.18.112:8010      │              │
│              │                              │              │
│              │ ─── 3. 网线 (USB dongle) ──→ │              │
│              │     192.168.123.18:8010      │              │
└──────────────┘                              └──────────────┘
```

| 路径 | 使用场景 | Jetson IP |
|---|---|---|
| **phswifi3 直连** | 笔记本和 Jetson 都在 lab WiFi | `10.100.206.170`（DHCP，可能变） |
| **Tailscale** | 跨网络 / 笔记本离开 lab | `100.112.18.112`（固定） |
| **网线** | 笔记本经 USB dongle 接 Go2 | `192.168.123.18` |

任一路径都同时支持 HTTP 和 SSH —— 同一个 IP 可以 `ssh unitree@...` 也可以
`curl http://.../status`。master_record.py 通过 `--host` 参数切换路径：

```bash
python laptop/master_record.py                          # config 里的默认 IP
python laptop/master_record.py --host 100.112.18.112    # 手动指定
```

---

## 6. 端口

| 服务 | 机器 | 端口 |
|---|---|---|
| camera_record_pipeline | 笔记本 | **8000** |
| record_daemon | Jetson | **8010** |

端口是每台机器独立的命名空间，`localhost:8000` 与 `10.100.x.x:8000` 是不同地址。
选用不同端口出于以下目的：

- 日志中 `:8000` 和 `:8010` 一眼可辨来源机器
- 若将来经反向代理 / SSH tunnel 暴露到同一 IP，不会冲突
- 保留"两个服务跑在同一台机器"的可能性（例如 daemon 和 camera pipeline
  将来都装在 Jetson 或都装在笔记本上）

---

## 7. 进程架构

`record_daemon` 是**一个 Python 进程**，由 systemd 守护。所有录制逻辑跑在这
一个进程的多个线程中；不产生子进程。

```
systemd (PID 1)
   │
   └── record_daemon python process  ← 由 systemd 管理的唯一进程
         │
         ├── [thread] uvicorn main loop         (事件循环)
         ├── [thread] uvicorn HTTP worker × N   (收 /start /stop /status)
         ├── [thread] DDS receiver              (CycloneDDS 内部起)
         │             └── 收 rt/lowstate, rt/utlidar/cloud
         └── [thread] ego camera capture loop   (egocentric_camera.py 起)
```

选择单进程多线程的理由：

- **DDS 只初始化一次**：`ChannelFactoryInitialize` 每个进程只能调用一次，
  放在 daemon 启动时执行，所有录制复用
- **内存共享**：所有 collector 的 buffer 就是 Python 对象，`save()` 直接
  `np.savez_compressed` 写盘，无跨进程 IPC
- **失败恢复简单**：进程挂掉后 systemd 按 `RestartSec=5` 自动重启，整进程
  重新上线，状态一致
- **uvicorn 自动管线程**：`@app.post("/start")` 装饰的函数跑在 uvicorn 的
  worker 线程池里，不需要手写 socket.accept / threading

---

## 8. systemd 基础

systemd 是 Linux 的 PID 1，开机第一个用户态进程，统一管理所有系统服务
（`NetworkManager`、`sshd`、`tailscaled` 等都是它管的）。

**systemd 通过扫描目录发现服务**，按优先级从高到低：

```
/etc/systemd/system/          管理员自定义，优先级最高
/run/systemd/system/          运行时临时生成
/usr/lib/systemd/system/      apt 等包管理器装的默认
```

**`.service` 文件是对进程的声明式配置**：用哪个用户、何时启动、崩溃策略、
日志处理等。

**关键命令**：

```bash
sudo systemctl daemon-reload                # 重扫目录（新建或修改 .service 后必须执行）
sudo systemctl enable  record_daemon        # 开机自启（创建 multi-user.target.wants/ 符号链接）
sudo systemctl start   record_daemon        # 立即启动一次
sudo systemctl enable --now record_daemon   # 上述两者合并
sudo systemctl restart record_daemon        # 重启（代码更新后用）
sudo systemctl stop    record_daemon        # 停止
systemctl status  record_daemon             # 查看状态
journalctl -u record_daemon -f              # 实时跟踪日志
journalctl -u record_daemon -n 100          # 最近 100 行日志
```

`enable` 与 `start` 相互独立：`start` 立即启动一次，`enable` 注册开机自启。

---

## 9. daemon 运行用户

daemon 运行在 `unitree` 用户下，不使用 root：

- conda 环境 `go2` 安装在 `unitree` 用户下
- `/home/GO2_DATA` 通过 chown 授予 `unitree` 写权限
- 遵循最小权限原则

**systemd unit 文件** (`jetson/record_daemon.service`)：

```ini
[Unit]
Description=Go2 Record Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=unitree
Group=unitree
WorkingDirectory=/home/unitree/go2_record_pipeline
Environment="PATH=/home/unitree/miniconda3/envs/go2/bin:/usr/bin"
ExecStart=/home/unitree/miniconda3/envs/go2/bin/python -m jetson.record_daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

systemd 不会自动 `conda activate`，`ExecStart` 必须写 conda 环境中 Python 的
**绝对路径**。路径格式：`<CONDA_ROOT>/envs/go2/bin/python`。

**安装命令需要 sudo**（因为要写 `/etc/systemd/` 和操作 systemctl），
但 daemon 本身以 `unitree` 身份运行：

```bash
sudo cp jetson/record_daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now record_daemon
```

**数据目录权限**（首次安装执行）：

```bash
sudo mkdir -p /home/GO2_DATA
sudo chown -R unitree:unitree /home/GO2_DATA
```

---

## 10. master_record.py 工作流

CLI 界面：

```
┌─────────────────────────────────────────┐
│  Go2 + Camera Record Master             │
├─────────────────────────────────────────┤
│  Task:   task1                          │
│  Prompt: Pick up the red cup            │
│  Jetson: 10.100.206.170:8010  (idle)    │
│  Camera: localhost:8000       (idle)    │
│                                         │
│  [T] Change task   [P] Edit prompt      │
│  [R] Record        [S] Stop             │
│  [Q] Quit                               │
└─────────────────────────────────────────┘
```

按键行为：

```
[R] Record
  ├── POST http://localhost:8000/start   body={"task", "prompt"}
  ├── POST http://<jetson>:8010/start    body={"task", "prompt"}
  └── 任一端失败 → 回滚已启动的那一端

[S] Stop
  ├── POST http://<jetson>:8010/stop
  ├── POST http://localhost:8000/stop
  └── 打印摘要（时长、各流样本数）

[Q] Quit
  └── 若仍在录制 → 先触发 Stop，再退出
```

---

## 11. 端到端录制流程

```
┌── Jetson 开机自动完成 ───────────────────────────────────┐
│   • phswifi3 自动连接（profile autoconnect=true）         │
│   • eth0 接入 Go2 子网 192.168.123.18                     │
│   • record_daemon 自启，监听 :8010                        │
└──────────────────────────────────────────────────────────┘
                             │
                             ▼
┌── 笔记本开启两个服务 ────────────────────────────────────┐
│   终端 1: cd ~/camera_record_pipeline && ./run.sh         │
│   终端 2: python laptop/master_record.py                  │
└──────────────────────────────────────────────────────────┘
                             │
                             ▼
┌── master_record CLI 中循环操作 ──────────────────────────┐
│   T  选 task                                              │
│   P  输入 prompt                                          │
│   R  开始录制  ──→ 两端同时 /start                        │
│          (机器人动作)                                     │
│   S  停止录制  ──→ 两端同时 /stop, 各自存盘              │
│   (重复 T/P/R/S 录多段，daemon 和 camera pipeline 不重启)│
│   Q  退出主控                                             │
└──────────────────────────────────────────────────────────┘
                             │
                             ▼
┌── 录制结束后手动合并 ───────────────────────────────────┐
│   rsync -av unitree@10.100.206.170:/home/GO2_DATA/       │
│              /home/GO2_DATA/                              │
└──────────────────────────────────────────────────────────┘
```

---

## 12. 采集频率

| 数据流 | 频率 | 来源 | 限速方式 |
|---|---|---|---|
| IMU + joints + foot_force | **500 Hz** | `rt/lowstate` | 无（原生推送） |
| 前相机 ego_rgb | **30 Hz** | `VideoClient.GetImageSample()` | `_capture_loop` 主动 `time.sleep` |
| LiDAR 点云 | **~11 Hz** | `rt/utlidar/cloud` | 无（硬件最大扫描率） |
| 4× RealSense | **30 Hz** | camera_record_pipeline | pipeline 内部控制 |

频率时间轴示意（同 1 秒内的相对密度）：

```
t=0                                                    t=1s
│                                                       │
IMU/joints/foot │││││││││││││││││││││││││││││││││││││││││...  500 Hz
ego_rgb / RSense │    │    │    │    │    │    │    │   ...   30 Hz
LiDAR            │      │      │      │      │      │    ~    11 Hz
```

**时间对齐方案**：所有数据流采用 host 的 `time.time()` 打时间戳。后期处理
以相机 30 Hz 时间戳为基准网格，其它流按最近邻对齐。

---

## 13. 常用操作速查

### Jetson 侧

```bash
# 服务状态与日志
systemctl status record_daemon
journalctl -u record_daemon -f
journalctl -u record_daemon -n 100

# 代码更新后重启服务
sudo systemctl restart record_daemon

# 停止服务（调试期间）
sudo systemctl stop record_daemon

# 直接测采集器（不走 daemon）
cd ~/go2_record_pipeline
conda activate go2
python scripts/test_collectors.py --interface eth0 --duration 3.0

# 直接调 daemon API
curl -X POST http://localhost:8010/start \
     -H 'Content-Type: application/json' \
     -d '{"task": "task1", "prompt": "test"}'
curl http://localhost:8010/status
curl -X POST http://localhost:8010/stop
```

### 笔记本侧

```bash
# 相机服务（终端 1）
cd ~/Desktop/Research/Harvard_AI/camera_record_pipeline
./run.sh

# 主控（终端 2）
cd ~/Desktop/Research/Harvard_AI/go2_record_pipeline
python laptop/master_record.py

# 跨网络时指定 Jetson 地址
python laptop/master_record.py --host 100.112.18.112

# 查询 Jetson daemon 状态
curl http://10.100.206.170:8010/status

# 录制后合并数据
rsync -av unitree@10.100.206.170:/home/GO2_DATA/ /home/GO2_DATA/
```

---

## 14. 进程 / 线程模型

### 14.1 层级关系

- **进程**：操作系统分配资源的单位，有独立内存空间。systemd 管理的
  `record_daemon` 是**一个进程**，开机启动、永不退出。
- **线程**：进程内部的执行流，共享进程的内存。
- 层级：`进程 > 线程`。一个进程可以有多个线程，线程不能独立存在。

### 14.2 daemon 进程内的完整数据流

```
┌─── record_daemon 进程（systemd 管）────────────────────────────┐
│                                                                │
│  开机时 → ChannelFactoryInitialize(0, "eth0")   ← DDS 初始化一次 │
│                                                                │
│  [主线程] uvicorn 事件循环 + HTTP worker 池 (:8010 监听)         │
│      │                                                         │
│      │  收到 POST /start {"task", "prompt"}                    │
│      ▼                                                         │
│  Recorder.start()                                              │
│      ├─→ IMUCollector.start()                                  │
│      │     └─ SDK 内部起 1 线程订阅 rt/lowstate                 │
│      │         回调 500 Hz 被触发 → buffer imu/joints/foot 三份 │
│      │                                                         │
│      ├─→ LidarCollector.start()                                │
│      │     └─ SDK 内部起 1 线程订阅 rt/utlidar/cloud            │
│      │         回调 ~11 Hz 被触发 → buffer lidar                │
│      │                                                         │
│      └─→ EgoCameraCollector.start()                            │
│            └─ 自定义 _capture_loop 在 1 个线程里跑              │
│                主动 30 Hz 拉 VideoClient.GetImageSample()       │
│                → buffer ego_rgb                                │
│                                                                │
│      收到 POST /stop                                           │
│      ▼                                                         │
│  Recorder.stop()                                               │
│      ├─→ 各 collector.stop() 停线程                             │
│      └─→ 各 collector.save(session_dir) 写盘                   │
│          写出: imu.npz, joints.npz, contacts.npz,             │
│                ego_rgb.mp4, ego_timestamps.npy,               │
│                lidar/*.npy, lidar_timestamps.npy,             │
│                lidar_meta.json                                │
│                                                                │
│      daemon 进程继续活着，等下一次 /start                       │
└────────────────────────────────────────────────────────────────┘
```

### 14.3 采集线程总览

采集部分一共 3 个线程，覆盖 5 种数据流：

| 线程 | 订阅 / 拉取 | 频率 | 产出的 buffer / 文件 |
|---|---|---|---|
| DDS receiver #1 | `rt/lowstate` | 500 Hz | `imu.npz`, `joints.npz`, `contacts.npz` |
| DDS receiver #2 | `rt/utlidar/cloud` | ~11 Hz | `lidar/*.npy`, `lidar_timestamps.npy` |
| camera capture loop | `VideoClient.GetImageSample()` RPC | 30 Hz | `ego_rgb.mp4`, `ego_timestamps.npy` |

uvicorn 的主线程和 HTTP worker 线程池负责收 HTTP 请求，由 uvicorn 自动管理。

### 14.4 线程的创建方

| 采集流 | 线程创建方 | 数据模式 |
|---|---|---|
| IMU + joints + foot | Unitree SDK（CycloneDDS 库） | DDS 推送式：注册 callback，数据自动到达 |
| LiDAR | Unitree SDK（CycloneDDS 库） | DDS 推送式 |
| 前相机 | 自定义（`egocentric_camera.py` 的 `_capture_loop`） | RPC 拉取式：主动发请求要帧 |

DDS 订阅的典型写法，不需要手写循环：

```python
subscriber = ChannelSubscriber("rt/lowstate", LowState_)
subscriber.Init(on_lowstate_callback, 10)   # SDK 自动起线程，Go2 发包就调 callback
```

相机是 RPC 请求-响应模式，必须自起循环：

```python
def _capture_loop(self):
    interval = 1.0 / 30
    while self._running:
        t0 = time.monotonic()
        code, data = self._client.GetImageSample()   # 主动拉一帧
        # ... decode JPEG, append to buffer ...
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))

threading.Thread(target=self._capture_loop, daemon=True).start()
```

### 14.5 IMU / joints / foot 共用同一个 topic

IMU、joints、foot_force 来自同一个 DDS topic `rt/lowstate`，是同一条 `LowState_`
消息的不同字段：

```
LowState_
├── imu_state.quaternion / .gyroscope / .accelerometer / .rpy   → imu.npz
├── motor_state[0..11].q / .dq / .tau_est                       → joints.npz
└── foot_force[0..3]                                            → contacts.npz
```

一个 subscriber、一个 callback 线程，一次 callback 同时填三个 buffer。
`utility/imu.py::IMUCollector` 承担三类数据的采集：

```python
def _on_lowstate(self, msg):        # 被 500 Hz 调用
    self._imu_quat.append(msg.imu_state.quaternion)
    self._joint_q.append([msg.motor_state[i].q for i in range(12)])
    self._foot_force.append(list(msg.foot_force[:4]))
```

`.save()` 时分别写成三个 NPZ 文件。

### 14.6 Collector 的统一接口

每个 collector 对外暴露三个方法，内部线程完全封装：

```python
class XCollector:
    def start(self):  ...    # 启动采集（内部起 DDS 订阅 / capture 线程）
    def stop(self):   ...    # 停止采集（内部 join 或取消订阅）
    def save(dir):    ...    # 把内存 buffer 写盘
```

上层 `Recorder` 和 `record_daemon` 只调用这三个方法，不关心内部线程数量与
使用的底层库。

---

## 16. 笔记本与 Jetson 通过 phswifi3 互通

### 16.1 笔记本连 phswifi3（WPA2 802.1X PEAP + MSCHAPv2）

phswifi3 是 WPA2-Enterprise，需要**用户名 + 密码**两个字段。系统默认的 GNOME
WiFi 对话框要求验证服务器证书，通过 `nmcli` 可以用 `802-1x.system-ca-certs no`
显式关闭证书验证，仅用用户名密码连接。

```bash
# 先找到笔记本 WiFi 接口名
nmcli device status
# 找 TYPE 为 wifi 的那一行的 DEVICE（本机是 wlp3s0）

# 创建 profile（和 Jetson 上使用的同一套命令）
sudo nmcli connection add type wifi ifname wlp3s0 \
  con-name phswifi3 \
  ssid phswifi3 \
  wifi-sec.key-mgmt wpa-eap \
  802-1x.eap peap \
  802-1x.phase2-auth mschapv2 \
  802-1x.identity 'kz1024' \
  802-1x.password 'kairesearch20' \
  802-1x.system-ca-certs no \
  connection.autoconnect yes

sudo nmcli connection up phswifi3
```

这与 `jetson/setup_jetson_wifi.sh` 中对 Jetson 的处理完全对称。连接成功后
profile 保存在 `/etc/NetworkManager/system-connections/phswifi3.nmconnection`，
开机自动重连。

### 16.2 Linux WiFi 接口命名约定

| 命名模式 | 含义 | 常见于 |
|---|---|---|
| `wlan0 / wlan1` | 旧版 BSD 风格，顺序编号 | Jetson、Raspberry Pi、老发行版 |
| `wlpXsY` | "WiFi + PCIe 总线 X + 槽位 Y"的 predictable naming | 现代笔记本（此机是 `wlp3s0`） |
| `wlxXXXXXXXX` | "WiFi + 十六进制 MAC 地址" | USB WiFi 适配器 |

查看方法：`nmcli device status` 看 TYPE=wifi 的行；或 `iw dev` 看 `Interface` 字段。

### 16.3 子网掩码 `/22` 与地址池

phswifi3 的 IPv4 配置是 `10.100.204.0/22`。`/22` 表示 IP 地址的前 22 位
（共 32 位）为网络号，剩余 10 位为主机号。

```
IP 32 位拆分:
  00001010 . 01100100 . 110011 00 . 00000000
  └──── 前 22 位 (网络号) ────┘ └── 后 10 位 (主机号) ──┘
     phswifi3 的地址区块         每台设备占一个编号

可用地址数: 2^10 = 1024
地址范围  : 10.100.204.0 ~ 10.100.207.255
网关      : 10.100.204.1
```

### 16.4 DHCP 分配机制

笔记本连上 WiFi 后，由 phswifi3 网关 (`10.100.204.1`) 上运行的 DHCP 服务器
从地址池中挑一个未占用的 IP 分发给客户端：

```
 笔记本                                              DHCP 服务器
   │                                                     │
   ├── DHCPDISCOVER (广播"谁给我 IP?")  ───────────────→  │
   │                                                     │
   │                                                扫描地址池
   │                                                查 MAC 是否有预留
   │                                                选中一个空闲 IP
   │                                                     │
   │ ←───────────  DHCPOFFER (给你 10.100.207.204)  ─────┤
   │                                                     │
   ├── DHCPREQUEST (我要这个 IP)  ────────────────────→   │
   │                                                     │
   │ ←───────────  DHCPACK (有效期 3600 秒)  ───────────┤
   │                                                     │
   ip addr 出现 inet 10.100.207.204
```

**Lease 续租**：`valid_lft` 过期前客户端会自动发 DHCPREQUEST 续租，通常
分到同一个 IP（企业 DHCP 对已知 MAC 做 sticky lease）。查看剩余时间：

```bash
ip addr show wlp3s0 | grep valid_lft
```

### 16.5 同子网互通

笔记本和 Jetson 同时连 phswifi3 后，都落在 `10.100.204.0/22` 里：

```
  phswifi3 子网 10.100.204.0/22  (1024 个地址)
 ┌──────────────────────────────────────────────┐
 │                                              │
 │  10.100.204.1    网关                         │
 │  10.100.206.170  Jetson wlan0                 │
 │  10.100.207.204  笔记本 wlp3s0                │
 │  ... 其他同网段设备 ...                        │
 │                                              │
 └──────────────────────────────────────────────┘
```

两台设备在同一个 L2 广播域内，通信路径：

```
笔记本 wlp3s0                    Jetson wlan0
(10.100.207.204)                (10.100.206.170)
     │                                │
     │ ARP 广播查 Jetson 的 MAC       │
     ├────────────────────────────→   │
     │                                │
     │  ←──── Jetson 回应 MAC ─────── │
     │                                │
     │ 直接二层帧发过去                │
     ├────────────────────────────→   │
```

**不经过公网、不穿 WAN 路由器**，仅通过 phswifi3 的 AP 做二层转发。因此
phswifi3 直连的延迟 < Tailscale，能满足 master_record.py 的 HTTP 调用。

### 16.6 验证笔记本与 Jetson 互通

```bash
# 笔记本上
ip route
# 期望看到: default via 10.100.204.1 dev wlp3s0
#          10.100.204.0/22 dev wlp3s0 ... src 10.100.x.y

ip addr show wlp3s0 | grep inet
# 期望看到 inet 10.100.x.y/22

ping -c 3 10.100.206.170                # Jetson 的 phswifi3 IP
ssh unitree@10.100.206.170              # 直接 SSH
curl http://10.100.206.170:8010/status  # daemon 接口（启动后）
```

ping 通 + SSH 可连 = phswifi3 直连就绪，`master_record.py` 不需要 `--host` 参数
覆盖，默认地址就能工作。

---

## 17. 相关笔记

- 网络拓扑和链路解析：[network_topology.md](network_topology.md)
- 笔记本 go2-link profile 配置：[laptop_network_setup.md](laptop_network_setup.md)
- Jetson phswifi3 自动连接：[setup_wifi_profile.md](setup_wifi_profile.md)
