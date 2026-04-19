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

## 18. 录制信号流与 UI 协作

### 18.1 阶段 0 — 静态状态（启动但未录）

```
┌── 笔记本 ─────────────────┐     ┌── Jetson ─────────────┐
│  camera_record_pipeline  │     │  record_daemon        │
│  FastAPI :8000 常驻       │     │  FastAPI :8010 常驻    │
│    _session_active=False │     │    state="idle"        │
│  4× RealSense 直播预览    │     │  (DDS 已初始化一次)   │
└──────────────────────────┘     └───────────────────────┘
              ▲                              ▲
              │ HTTP :8000                   │ HTTP :8010
              │                              │ (经 :8000 的代理)
      ┌───────┴──────────────┐               │
      │       浏览器          │───────────────┘
      │  每 2 秒:             │
      │    GET /status        │
      │    GET /jetson/status │
      │  Robot 行:🟢 idle      │
      │  Start 可点 / Stop 灰 │
      └───────────────────────┘
```

### 18.2 阶段 1 — 点击 ▶ Start

```
浏览器
  1. 读 UI: task, prompt, data_root, use_jetson 复选框
  2. localStorage 保存偏好
  3. POST http://localhost:8000/start
       body: {task, prompt, data_root, use_jetson}
  ▼
camera_record_pipeline (/start)
  4. 校验 task 在 TASKS 列表；否则 400
  5. 检查 _session_active；已 True 则 409
  6. 若 use_jetson && JETSON_URL: POST <jetson>:8010/start   ← 先通知 Jetson (fail-fast)
      │
      ▼
  record_daemon (/start)
    7. mkdir -p /home/unitree/GO2_DATA/<task>/<date>/<time>/
    8. Recorder.start() 启动三个 collector
        ├── IMUCollector — ChannelSubscriber rt/lowstate          (500 Hz)
        ├── EgoCameraCollector — _capture_loop 线程拉 VideoClient (30 Hz)
        └── LidarCollector — ChannelSubscriber rt/utlidar/cloud   (~11 Hz)
    9. state = "recording"，返回 200 {session_dir}
      ▼
  10. 接到 Jetson 200 → 启动本地 4 相机 recorder (try/except 内)
       失败则自动 POST <jetson>:8010/stop 回滚
  11. _session_active = True, _session_used_jetson = forward_jetson
  12. 返回 200 {session_dir, cameras, jetson_session_dir, used_jetson}
  ▼
浏览器
  13. UI 切到录制态:
      Start 灰 / Stop 活 / rec-dot 红闪 / rec-label "Recording"
      rec-elapsed 计时器启动
      data-root + use-jetson 锁定
  14. toast: "Recording started — task1 + robot"
```

### 18.3 阶段 2 — 录制期间（多源并行 + UI 轮询）

**Jetson record_daemon 进程内并发的线程**：

| 线程 | 来源 | 频率 | 写入的 buffer |
|---|---|---|---|
| DDS receiver #1 | SDK 内部 | 500 Hz | imu + joints + foot_force |
| DDS receiver #2 | SDK 内部 | ~11 Hz | lidar 点云 |
| ego camera capture loop | 自写 | 30 Hz | ego_rgb JPEG |

**笔记本 camera_record_pipeline 进程内**：

| 异步任务 | 作用 |
|---|---|
| frame_broadcast_loop (30 Hz) | 每相机 get_frames → write_frame 到 Recorder |
| WebSocket /ws/stream | 推实时预览给浏览器 |
| WebSocket /ws/orientation | 推 IMU 方向给浏览器 |

**浏览器每 2 秒轮询**：

| 请求 | 更新 |
|---|---|
| `GET /status` | 每相机面板显示 "FPS: 30 (live 27.3)" 带颜色分级 |
| `GET /jetson/status` (经代理) | Robot 行显示 "recording (Xs · imu N · ego M · lidar K)" |
| `GET /stats` (每 5 秒) | 任务统计 chips |

**兜底同步**：`fetchStatus` 中若连续 2 次"后端 idle 但 UI 为 Recording" → 自动重置 UI 到 idle。

### 18.4 阶段 3 — 点击 ■ Stop

```
浏览器
  1. 立即视觉反馈（不等网络）:
       Stop → "⏳ Saving…" disabled
       rec-dot 移除 recording 类
       rec-label → "Saving…"
  2. POST /stop
  ▼
camera_record_pipeline (/stop)
  3. 若 _session_active=False → 409
  4. _session_active = False; used_jetson = _session_used_jetson
  5. asyncio.gather 并行:
      ┌── 本地停 ─────────────────────────────┐
      │ 每个 cam recorder.stop():             │
      │   cv2.VideoWriter.release()           │
      │   写 rgb_timestamps.npy              │
      │   若 actual_fps 偏离超 5% →           │
      │     ffmpeg remux 修正 rgb.mp4 帧率    │
      │ 存 depth.npz                          │
      └───────────────────────────────────────┘
      ┌── Jetson 停 (used_jetson=True 时) ───┐
      │ POST <jetson>:8010/stop              │
      │   → Recorder.stop() + save() 全部    │
      │   → 返回 samples, elapsed            │
      └───────────────────────────────────────┘
  6. 合并结果 + 更新 stats.json
     返回 200 {session_dir, elapsed_seconds, cameras, jetson, jetson_error}
  ▼
浏览器 stop handler
  7. 成功路径:
      Start 活 / Stop 灰 / rec-label "Idle"
      输入框解锁
      toast "Saved — 12.5s · robot: imu 6250 · ego 375 · lidar 138"
      (若 jetson_error 存在 toast 变红)
      fetchStats() + fetchJetsonStatus()
  8. 409 路径 (后端已 idle):
      当成成功处理，UI 同步到 idle
      toast "UI re-synced (backend was already idle)"
  9. 其他错误:
      UI 回到 Recording 让用户重试
```

### 18.5 阶段 4 — ⏹ Force-stop 紧急按钮

当 UI 和 Jetson 状态脱钩（Robot 行显示 recording 但 camera 已 idle）时的一键救援：

```
浏览器
  POST http://localhost:8000/jetson/stop
  ▼
camera_record_pipeline (/jetson/stop)
  不管 _session_active, 直接 POST <jetson>:8010/stop
  ▼
record_daemon (/stop)
  全部 collector.stop() + save(), 返回 200
  ▼
浏览器 toast:
  "Robot force-stopped (12.5s, imu N / ego M / lidar K)"
  或 "Robot was already idle"
```

### 18.6 UI 状态机

| UI 元素 | idle | 录制中 | Saving | 错误 |
|---|---|---|---|---|
| **Start 按钮** | 可点 | disabled | disabled | (按情况) |
| **Stop 按钮** | disabled | 可点 | "⏳ Saving…" disabled | 可点重试 |
| **rec-dot** | 灰 | 红闪 | 灰 | — |
| **rec-label** | "Idle" | "Recording" | "Saving…" | — |
| **rec-elapsed** | "" | MM:SS | 定格 | — |
| **data-root 输入框** | 可改 | 锁定 | 锁定 | — |
| **use-jetson 复选框** | 可改 | 锁定 | 锁定 | — |
| **Robot 行** | 🟢 idle | 🔴 recording (数字跳) | — | 🔘 unreachable |
| **每相机 FPS** | `30` | `30 (live 27.3)` 颜色分级 | — | — |
| **Force-stop 按钮** | 恒可用 | 恒可用 | 恒可用 | 恒可用 |

### 18.7 三层安全网

1. **Stop 收到 409 时的 UI 同步**：识别为"后端已 idle"并把 UI 切到 idle，而非强刷回 Recording
2. **2 秒轮询兜底**：`fetchStatus` 连续 2 次检测到"后端 idle / UI Recording" 自动重置
3. **Force-stop 按钮**：无条件发 Jetson `/stop`，修复任何剩余的脱钩状态

### 18.8 数据落盘路径

```
笔记本: <Save Directory>/<task>/<MM_DD_YYYY>/<HH_MM_SS>/
          ├── cam1/  cam2/  cam3/  cam4/
          │   ├── rgb.mp4                    H.264 视频
          │   ├── rgb_timestamps.npy         每帧 Unix 时间戳
          │   ├── depth.npz                  深度帧 + 时间戳
          │   └── intrinsics.json
          └── prompt.txt

Jetson: /home/unitree/GO2_DATA/<task>/<MM_DD_YYYY>/<HH_MM_SS>/
          ├── imu.npz       joints.npz     contacts.npz
          ├── ego_rgb.mp4   ego_timestamps.npy
          ├── lidar/000000.npy ...
          ├── lidar_timestamps.npy
          └── lidar_meta.json

录后手动 rsync:
  rsync -av unitree@<jetson>:/home/unitree/GO2_DATA/ <Save Directory>/
  → 两边合并进同一个 <task>/<date>/<time>/ 目录
```

---

## 19. 时间同步（chrony）

### 19.1 目标与方案

所有采集流（camera、IMU、joints、ego_cam、LiDAR）都用**宿主机 `time.time()`** 打时间戳。
笔记本和 Jetson 分别在两台机器上打时间戳，要求两台机器的系统时钟对齐误差
**< 5 ms**，否则后处理时 timestamp-interpolation 对齐会失败。

使用 **chrony**：笔记本当 NTP server，Jetson 当 client。实测稳定后偏差在
**10 μs 量级**，比目标高两个数量级。

### 19.2 笔记本侧（NTP server）

**每一台**会被 Jetson 使用的 laptop 都要装 chrony 并做这套相同的配置。当前
有两台：`yuzhench_laptop` (10.100.207.204) 和 `lab_laptop` (10.100.204.36)。

```bash
sudo apt install -y chrony
# Ubuntu 22.04+ 默认可能装了 systemd-timesyncd，会和 chrony 互斥：
sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
sudo systemctl enable --now chrony
```

`/etc/chrony/chrony.conf` 在默认配置基础上加这几行：

```conf
# 允许 Jetson 所在的 WiFi /24 和 Go2 AP /24 来查询
allow 10.100.206.0/24
allow 192.168.123.0/24

# 即使笔记本自己上游 NTP 掉了也继续给 Jetson 提供时间
local stratum 10
```

```bash
sudo systemctl restart chrony
```

如果启用了 UFW：`sudo ufw allow 123/udp`（默认 Ubuntu 桌面 UFW 不启用，不用管）。

**两台 laptop 的配置完全对称**——它们互相不通讯，不会冲突，也不构成环路（各自
独立向公网 pool 取时间）。Jetson 同时问两台，自己决定用哪台（见 19.7）。

### 19.3 Jetson 侧（NTP client，Plan B 稳妥策略）

`/etc/chrony/chrony.conf` 完整替换为：

```conf
# ======================================================
# Time sources — lab /22 内所有可能的 laptop
# chrony 自动挑能通的那个当主源
# ======================================================
server 10.100.207.204 iburst minpoll 4 maxpoll 6          # yuzhench_laptop
server 10.100.204.36  iburst prefer minpoll 4 maxpoll 6   # lab_laptop (prefer)

# ======================================================
# Sync behavior (Plan B: restart-to-resync, aggressive)
# ======================================================
# 开机/restart 后前 3 次允许瞬间 step（即使偏差只有 1 ms 也跳）→ restart
# 即刻对齐到 μs 级；3 次配额用完后自动降级为全程 slew，录制期间不会出现
# 时间跳变。每次录制前点一下 UI 的 "⟳ Resync" 按钮（或 SSH 跑
# `sudo systemctl restart chrony`）重置配额即可。
makestep 0.001 3

# 开机时如果 RTC 与任一 server 差 < 30s，先跳过去再进入常规流程
initstepslew 30 10.100.207.204 10.100.204.36

# 加快样本收敛
maxupdateskew 100.0

# 定期把系统时间写回硬件 RTC，下次冷启动起点更准
rtcsync

# ======================================================
# Defaults
# ======================================================
driftfile /var/lib/chrony/chrony.drift
keyfile   /etc/chrony/chrony.keys
logdir    /var/log/chrony
```

### 19.4 日常工作流

每次**开始录制前**，在 Jetson 上跑：

```bash
sudo systemctl restart chrony
```

效果：
- `iburst` 几秒内连发 4 个包锁定主源
- 前 3 次 update 允许瞬间 step → 绝对对齐（无论偏差多大）
- 进入常态后只 slew，不再 step → 录制数据里的时间戳单调递增，不会跳变

也可以从笔记本远程一条触发（前提：免密 ssh + 免密 sudo systemctl）：

```bash
ssh unitree@<jetson_ip> "sudo systemctl restart chrony && sleep 3 && chronyc tracking | head"
```

### 19.5 验证命令

```bash
chronyc sources -v     # 看主源（带 *）、候补源（带 +/?）
chronyc tracking       # 看当前偏差和漂移
```

`chronyc tracking` 输出里关键看两行：

| 字段 | 含义 | 达标值 |
|---|---|---|
| `Reference ID` | 当前主源（hex 形式的 IP 或反向 DNS 名） | 应该是某台 laptop |
| `System time` | 相对主源时间的偏差 | < 0.005 s（5 ms） |

Partners 网络上两台 laptop 有反向 DNS：
- `10.100.207.204` → `g14.partners.org`（yuzhench laptop）
- `10.100.204.36`  → `mengyu-msi-laptop.partners.org`（lab laptop）

所以 `chronyc sources` 里看到的是域名而不是 IP，不影响功能。

### 19.6 为什么选 `makestep 0.001 3`

`makestep` 有两个参数：**step 阈值** 和 **次数限制**。

阈值太大（如 `0.1` = 100 ms）意味着只有偏差 > 100 ms 才 step，否则只 slew。
slew 要几十秒到几分钟才收敛到亚毫秒，不适合"点按钮后立刻想录制"的工作流。

次数限制 `-1`（无限次）意味着运行中任何时候只要偏差超阈值都会 step。
录制过程中被 chrony 突然跳一下时钟，时间戳会出现非物理跳跃（如
`t_{n+1} - t_n = +400 ms`），下游对齐/插值脚本直接出错，数据无法事后修复。

**`makestep 0.001 3` 是两个维度都保守的折中**：
- 阈值 1 ms → restart 后哪怕只差 2-3 ms 也会直接 step，立刻对齐到 μs 级
- 次数 3 → 配额用完后自动降级为全程 slew，录制期间绝不跳变

日常工作流：每次开录前点 UI 的 "⟳ Resync" 按钮 → Jetson 重启 chrony →
3 次 step 配额刷新 → iburst 连发 4 包 → 立即 step 到 sub-ms → waitsync 确认收敛 →
返回 offset 显示。**录制中 chrony 只 slew，数据时间戳单调递增，干净。**

### 19.7 双 laptop 冗余与 `prefer`

chrony 并发向所有 server 发 NTP 包，各自打分（RTT、jitter、可达性等），在
"健康候选者"中选一个当主源（`^*`），其余标 `^+`（候补）或 `^-`（被排除）。
Server 行的**先后顺序不影响选择**，真正决定优先级的是关键字 `prefer`。

#### 选择规则

| 符号 | 含义 |
|---|---|
| `^*` | 当前主源，时间从它来 |
| `^+` | 候补，健康可用但当前未被选中 |
| `^-` | 能收到包但质量差，被排除在时间计算外 |
| `^?` | 不可达（Reach=0），从没收到过回包 |
| `^x` | false ticker，和其他源分歧太大被判错 |

带 `prefer` 的 server 只要健康就优先当主源。不健康时（如离线）自动
fallback 到其他 server。主源掉线在几分钟内自动切换。两个都离线时，chrony
靠 driftfile 中学到的频率补偿独立运行，几小时精度不崩。

#### 新加一台 server 后不生效的排查顺序

按顺序跑这几条，第一条出错就回到那一步修，不要跳：

**(1) server 端服务活着吗**（在那台 laptop 上）：
```bash
systemctl is-active chrony
sudo ss -ulnp | grep ':123'    # 应该 bind 到 0.0.0.0:123，不是 127.0.0.1:123
```
Ubuntu 默认装了 systemd-timesyncd 就会阻止 chrony 启动——必须先
`sudo systemctl disable --now systemd-timesyncd`。

**(2) allow 网段覆盖 Jetson 吗**（在那台 laptop 上）：
```bash
grep -E "^allow|^bindaddress|^local" /etc/chrony/chrony.conf
```
`allow 10.100.206.0/24` 必须有（或更宽的 `/22`）。只写 `allow 10.100.204.0/24`
**不覆盖** Jetson 所在的 `10.100.206.x`。

**(3) 网络能通 + 防火墙不挡**（在 Jetson 上）：
```bash
ping -c 3 <laptop_ip>
nc -uvz <laptop_ip> 123        # UDP 123
```
挡住时在 laptop 上 `sudo ufw allow 123/udp` 或检查 iptables。

**(4) chrony 暖机不够**（在 Jetson 上，手动加速）：
```bash
sudo chronyc -a 'burst 4/4 <laptop_ip>'
sleep 5
chronyc sources -v
```
`burst N/M` 让 chrony 立刻向指定 server 连发 N 个包（共 M 次机会），
跳过默认 `maxpoll` 的轮询间隔。新加 server 刚加入时常用这招跳过暖机期。

#### `prefer` 不是强制绑定

`prefer` 给的是**偏好**，不是绝对约束。chrony 每次打分时如果判定 `prefer` 源
临时不健康（高 jitter、包丢失、变成 false ticker 等），会**自动回落**到其他
健康源。这是特性不是 bug——保证同步不断。

具体条件：只要 `prefer` 源处于 "selectable" 状态（Reach 非全 0 且未被判错），
chrony 就用它；进入 unselectable 就切走，恢复后下次评分窗口会切回来。
观察到主源在两台之间切换，说明当前主源短暂被降级——通常 WiFi 抖动引起，
秒到分钟级自动恢复，不用手动干预。

如果想让 lab_laptop 真正"强绑定"（哪怕分歧也信它）：加 `trust` 关键字。
但这相当于告诉 chrony "相信它胜过其他一切"——只在它是**最权威时钟源**
（如 GPS、铯原子钟）时才加。laptop 本身也只是从公网 pool 转一手，没有
权威性，加 `trust` 反而可能放大错误。**不建议用 trust。**

### 19.8 UI "⟳ Resync" 按钮 + 免密 sudo

#### 按钮流程

前端右上角 "Clock sync" 一行，按钮触发：

```
Browser → POST /jetson/resync_clock (laptop backend)
       → POST /resync_clock         (Jetson daemon)
       → subprocess: sudo systemctl restart chrony
       → chronyc waitsync 15 0.005 100 1  (等收敛到 < 5 ms)
       → chronyc tracking → parse offset
       → return {offset_ms, reference, synced}
Browser 显示 "+0.12 ms · g14.partners.org"
```

按钮配色：< 1 ms 蓝绿、< 5 ms 淡绿、> 5 ms 黄色警告；失败红色。
所有消息进 Event Log 不会消失。

#### 免密 sudo 只在 Jetson 上配

`unitree` 用户（daemon 进程身份）要能免密 restart chrony：

```bash
# 在 Jetson 上
sudo visudo -f /etc/sudoers.d/chrony-restart
# 加一行：
unitree ALL=(root) NOPASSWD: /usr/bin/systemctl restart chrony
```

**laptop 不需要配**。lab_laptop 和 yuzhench_laptop 只是被动 NTP server，
它们的 chrony 常驻运行，没人会去 restart 它们。按钮只控制 Jetson 自己
那一端的 chrony。

#### 什么时候点

- 每次开录前按一次（3-5 秒的保险）
- Jetson 刚开机或刚切 WiFi 后
- 感觉时间戳对不上时（通常不会出现）

日常录制间隔短时只按一次就够。chrony 自己维持同步，不用反复按。

### 19.9 时区（与 chrony 无关但必须处理）

Jetson 出厂默认 `Asia/Shanghai`（Unitree 在中国组装）。chrony 同步的是
Unix epoch（UTC），跟时区无关，两边 `time.time()` 永远一致。**但是**
`recorder.py` 里 `datetime.now()` 返回**本地时间**，直接影响录制文件夹
命名：

- Jetson (CST +0800): `04_19_2026/07_03_xx/`
- Laptop (EDT -0400): `04_18_2026/19_03_xx/`

同一次录制会落到不同日期目录，rsync 合并时对不齐。

#### 修法

```bash
# 在 Jetson 上
sudo timedatectl set-timezone America/New_York
timedatectl        # 验证，Time zone 一行应该是 America/New_York (EDT)
```

立即生效，不需要重启系统/chrony。**但必须重启 record_daemon**——Python
进程在启动时读一次 `/etc/localtime` 并缓存，进程生命周期内不会重读：

```bash
sudo systemctl restart record_daemon
```

验证：
```bash
python3 -c "from datetime import datetime; print(datetime.now())"
# 应该是美东时间（EDT）
```

改时区前已经录制的 `04_19_2026/...` 文件夹是历史遗物，不会自动改名，
需要的话手动 `mv` 或删掉重录。

## 20. 全局示意图（用于 slide / report）

本节 6 张 ASCII 示意图是用来**参考画正式图**的骨架，不是最终图。放在
draw.io / Excalidraw 等工具里重绘后可以直接放进 presentation 或 report。

### 20.1 系统总览

> 目标：一眼看懂有几台机器、数据从哪里来到哪里去。

```
┌─────────────────────────── LAPTOP (yuzhench / lab) ───────────────────────────┐
│                                                                                │
│   ┌──────────────┐       ┌────────────────────────┐       ┌───────────────┐   │
│   │  Browser UI  │──HTTP─│ camera_record_pipeline │──USB──│ 4× RealSense  │   │
│   │ (index.html) │  WS   │   FastAPI backend      │       │  D435I (third-│   │
│   │              │       │   :8000                │       │  person view) │   │
│   └──────────────┘       └──────────┬─────────────┘       └───────────────┘   │
│                                     │                                          │
│                                     │  HTTP: /start, /stop,                    │
│                                     │        /resync_clock, /status            │
└─────────────────────────────────────┼──────────────────────────────────────────┘
                                      │ WiFi /22
                                      ▼
┌───────────────────────── JETSON on GO2 back ──────────────────────────────────┐
│                                                                                │
│   ┌───────────────────────┐                                                    │
│   │  record_daemon :8010  │                                                    │
│   │  FastAPI              │                                                    │
│   │  ┌─────────────────┐  │      ┌─────────────────────┐                       │
│   │  │    Recorder     │──┼──────│ IMU / joint / force │ ← DDS rt/lowstate     │
│   │  │  (state machine)│  │      │     collector       │   (~500 Hz)           │
│   │  │  idle → rec →   │  │      └─────────────────────┘                       │
│   │  │       saving    │  │      ┌─────────────────────┐                       │
│   │  │                 │──┼──────│ Ego RealSense D435I │ ← USB3                │
│   │  └─────────────────┘  │      │ (first-person view) │   RGB+Depth 30 Hz     │
│   │                       │      └─────────────────────┘                       │
│   │                       │      ┌─────────────────────┐                       │
│   │                       │──────│      LiDAR          │ ← DDS topic           │
│   │                       │      └─────────────────────┘                       │
│   └───────────────────────┘                                                    │
│                                                                                │
│                             ↕ DDS (CycloneDDS) over eth0                       │
└──────────────────────────────────┬─────────────────────────────────────────────┘
                                   │
                                   ▼
                            ┌──────────────┐
                            │  Go2 EDU     │
                            │  main board  │
                            │ 192.168.123. │
                            │       161    │
                            └──────────────┘
```

### 20.2 时间同步

> 目标：说明如何保证两台机器时间戳 < 5 ms 对齐（详见 §19）。

```
          ┌─── Internet NTP pool (stratum 1-2) ────┐
          │                                         │
          ▼                                         ▼
  ┌────────────────┐                     ┌────────────────┐
  │ yuzhench_laptop│                     │  lab_laptop    │
  │  10.100.207.204│                     │ 10.100.204.36  │
  │  chrony server │                     │  chrony server │
  │  allow /22     │                     │  allow /22     │
  │  stratum 3     │                     │  stratum 3     │
  └───────┬────────┘                     └────────┬───────┘
          │                                       │
          │  NTP / UDP 123                        │ NTP / UDP 123
          │  (4-timestamp exchange)               │
          └──────────────────┬────────────────────┘
                             │
                             ▼
                 ┌────────────────────────┐
                 │   Jetson (on Go2)      │
                 │  10.100.206.170        │
                 │  chrony client         │
                 │  stratum 4             │
                 │                        │
                 │  makestep 0.001 3      │
                 │  ├── first 3 updates:  │
                 │  │   step (瞬间跳)     │
                 │  └── after: slew only  │
                 │      (录制中单调)      │
                 └────────────────────────┘

UI "⟳ Resync" button flow:
 browser ─▶ laptop backend ─▶ Jetson daemon
                               │
                               ├─▶ sudo systemctl restart chrony
                               ├─▶ chronyc waitsync (wait until offset < 5ms)
                               └─▶ return {offset_ms, reference}
```

### 20.3 一次完整录制的时序

> 目标：展示 Start → Stop 里每台机器在做什么、并发关系。

```
Browser          Laptop Backend         Jetson Daemon          Cameras        Go2
  │                    │                     │                    │            │
  │──POST /start──────▶│                     │                    │            │
  │                    │──start 4 cams──────────────────────────▶ │            │
  │                    │──POST /start────────▶│                   │            │
  │                    │                     │──start collectors──────────────▶│
  │                    │                     │                                 │
  │◀─200 (session dir)─│◀──200 (session dir)─│                                 │
  │                    │                                                       │
  │   (recording, ~30 fps cameras, 500 Hz IMU, frames buffered in RAM)         │
  │                    │                                                       │
  │──POST /stop───────▶│                     │                                 │
  │                    │╲                    │                                 │
  │                    │ ╲──stop 4 cams + save NPZ ─ parallel ─▶│              │
  │                    │ ╱──POST /stop ────── parallel ─▶│                     │
  │                    │╱                                                      │
  │                    │                     │──stop collectors──────────────▶│
  │                    │                     │──save imu.npz / ego_cam / lidar│
  │                    │                     │  (NPZ compression: 5-30 s)     │
  │                    │◀─200 (samples dict)─│                                 │
  │◀─200 (merged)──────│                                                       │
  │                                                                            │
  │   (UI transitions: Recording → Saving → Idle)                              │
  │                                                                            │
  │  ── optional: click Sync button ───────────────────────────────────┐       │
  │                    │                                               │       │
  │                    │── rsync pull Jetson:/GO2_DATA → laptop ─────▶ │       │
  │                    │     (bwlimit 5 MB/s, partial, SSH keepalive)          │
```

### 20.4 session 目录结构（rsync 合并后）

> 目标：说明一次录制最终在磁盘上长什么样。

```
<DATA_ROOT>/
└── task1/                           ← 10 个任务之一
    └── 04_18_2026/                  ← 日期（美东时间，两台机器时区统一）
        └── 14_32_17/                ← 时间
            ├── first_person/        ← 来自 Jetson（rsync 拉过来）
            │   ├── imu.npz          ← timestamps, quat, gyro, accel, rpy
            │   ├── joints.npz       ← q, dq, tau (Nx12)
            │   ├── contacts.npz     ← foot_force (Nx4)
            │   ├── ego_cam/
            │   │   ├── rgb.mp4
            │   │   ├── rgb_timestamps.npy
            │   │   ├── depth.npz
            │   │   └── intrinsics.json
            │   └── lidar/
            │       └── ...
            │
            └── third_person/        ← 来自 laptop（本地保存）
                ├── cam0/
                │   ├── rgb.mp4
                │   ├── rgb_timestamps.npy
                │   └── depth.npz
                ├── cam1/  ...
                ├── cam2/  ...
                ├── cam3/  ...
                └── session_meta.json
```

**关键不变量**：`first_person/` 和 `third_person/` 两侧所有 `.npy` 时间戳
都是**同一条 Unix epoch 时间轴**（靠 chrony 对齐的 `time.time()`）。
后处理脚本插值对齐即可，无需额外校准。

### 20.5 网络拓扑（排障用）

> 目标：出问题时看这张图就能定位哪条链路。

```
                     ┌──────────────────┐
                     │  Lab WiFi /22    │
                     │ 10.100.204.0/22  │
                     │  SSID: phswifi3  │
                     └──────────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
          ▼                  ▼                  ▼
  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
  │ yuzhench    │    │ lab_laptop  │    │  Jetson     │
  │ 10.100.207. │    │ 10.100.204. │    │ 10.100.206. │
  │      204    │    │      36     │    │      170    │
  └─────────────┘    └─────────────┘    └──────┬──────┘
                                               │ eth0
                                               │ 192.168.123.x
                                               ▼
                                        ┌─────────────┐
                                        │  Go2 AP     │
                                        │192.168.123. │
                                        │     161     │
                                        └─────────────┘

用到的端口：
  8000/tcp  — laptop camera_record_pipeline（UI + API）
  8010/tcp  — Jetson record_daemon
  123/udp   — chrony NTP
  22/tcp    — SSH（rsync + 远程诊断）
  DDS (~7400/udp 动态) — Jetson ↔ Go2 CycloneDDS
```

### 20.6 录制状态机 + 容错层级

> 目标：说明系统"永远能回到 idle"的设计。

```
       ┌─────────────────────────────────────────────────┐
       │                                                 │
       │        [Idle] ──/start──▶ [Recording]           │
       │          ▲                     │                │
       │          │                     │ /stop          │
       │          │                     ▼                │
       │          └────────────────[Saving]              │
       │           /stop succeeds                        │
       │                                                 │
       │  Force-stop：/jetson/stop 可强制从任意态回 Idle │
       └─────────────────────────────────────────────────┘

三层失败处理（从轻到重）：
  L1: /stop 超时（Jetson 存盘慢）  → UI 黄色 warning，实际数据还在存
  L2: Jetson 不可达（WiFi 掉）     → UI 红色 error，本地相机仍然存盘成功
  L3: 本地相机 save 崩             → try/except 保护，异常进 Event Log 不丢
```

### 20.7 画正式图时顺便提的几个要点（不一定画图）

1. **时间戳单一来源**：所有流都用宿主机 `time.time()`（UTC epoch），从不依赖
   DDS / 相机硬件时钟。chrony 保证两台宿主机 `time.time()` 对齐。
2. **协议版本号 (`PROTOCOL_VERSION=4`)**：frontend / laptop backend / Jetson
   daemon 三方严格相等。mismatch 立刻红 banner + 禁用录制，避免半升级状态
   产生脏数据。
3. **控制平面 vs 数据平面分离**：HTTP 只传 start/stop/status（几 KB），数据
   从来不走 HTTP——相机/robot 数据全本地盘，WiFi 只承载控制信令。
4. **Event Log 常驻面板**：任何 success/warning/error 都写进 UI 底部可滚动
   日志，不因 toast 3 秒消失而看不到历史错误。

---

## 21. 相关笔记

- 网络拓扑和链路解析：[network_topology.md](network_topology.md)
- 笔记本 go2-link profile 配置：[laptop_network_setup.md](laptop_network_setup.md)
- Jetson phswifi3 自动连接：[setup_wifi_profile.md](setup_wifi_profile.md)
- 站起/趴下 遥控测试流程：[remote_control_test.md](remote_control_test.md)
