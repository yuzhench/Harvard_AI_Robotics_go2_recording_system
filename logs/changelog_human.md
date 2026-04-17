# Changelog — Human Readable

This log tracks what was asked, what was done, and why, in plain language.
Most recent entries are at the top.

---

## 2026-04-07 — Project Bootstrap

**What was asked:**
用户希望为 Unitree Go2 EDU 机器狗建立一个数据收集系统，需要收集机器人内部的 IMU 数据、关节角度、接触力等信息，同时需要与已有的 `camera_record_pipeline` 项目（4 个 Intel RealSense D435I 外置相机）进行时间对齐的同步录制。用户还要求建立两种日志系统：一种人类可读的（即本文件），一种技术细节日志。

**What was done:**
1. 读取了 `camera_record_pipeline` 的全部源代码，理解了其架构：
   - FastAPI 服务器（`server.py`）通过 `/start` 和 `/stop` REST API 控制录制
   - 每个相机有独立的 `Recorder` 实例，保存 RGB（MP4）+ 深度（NPZ）+ 内参（JSON）
   - 时间戳使用 `time.time()`（Unix 时间），保存在 `depth.npz` 的 `timestamps` 字段中
   - 项目已有 4 相机同步录制的能力

2. 在 `go2_record_pipeline/` 下建立了项目骨架：
   - `README.md` — 项目说明，包括架构图、数据格式、快速开始指南
   - `PLAN.md` — 分阶段实现计划（M1-M7），含 Go2 数据模型说明（12 关节顺序、IMU 字段等）
   - `logs/changelog_human.md` — 本文件
   - `logs/changelog_technical.md` — 技术细节日志
   - `go2_bridge/` — 准备放 Go2 SDK 桥接代码的目录
   - `data/` — 录制数据的输出目录
   - `scripts/` — 主控脚本目录

**Key design decisions:**
- **同步方案**：由一个"主控脚本"（`scripts/master_record.py`）同时触发相机 pipeline 的 `/start` API 和 Go2 数据采集，两者都用 host PC 的 `time.time()` 作为时间基准，不依赖机器人内部时钟。
- **数据格式**：机器人数据存成 NPZ（NumPy 压缩格式），与相机 depth.npz 保持一致，便于后处理对齐。
- **SDK 选择**：使用 `unitree_sdk2_python` 而非 ROS2，延迟更低，不需要额外的 ROS 环境。

**What's next:**
- 实现 `go2_bridge/go2_data_collector.py`（Phase 2 of PLAN.md）
- 实现 `scripts/master_record.py` 主控脚本（Phase 3）

---

## 2026-04-07 — utility/ 模块实现

**What was asked:**
用户希望先把 Go2 端的采集代码写好（不含同步逻辑），采用 `utility/` 文件夹结构，每个传感器模态一个 Python 文件：`imu.py` 和 `egocentric_camera.py`。

**What was done:**
1. 创建 `utility/imu.py`：
   - 订阅 `rt/lowstate`，采集 IMU（四元数、陀螺仪、加速度、RPY）、12个关节角度/速度/力矩、4个足部接触力
   - 数据存为三个 NPZ 文件：`imu.npz`、`joints.npz`、`contacts.npz`
   - 没有 SDK 时自动切换到 mock 模式（100 Hz 合成数据）

2. 创建 `utility/egocentric_camera.py`：
   - 使用 `Go2VideoClient.GetImageSample()` 获取前置摄像头 JPEG 帧
   - 保存为 `ego_rgb.mp4` + `ego_timestamps.npy`
   - 没有 SDK 时自动切换到 mock 模式

3. 创建 `scripts/test_collectors.py`：
   - 可以在没有机器人的情况下用 mock 模式先测试流程
   - 接受 `--interface` 和 `--duration` 参数

**现在可以先用 mock 模式测试：**
```bash
cd go2_record_pipeline
python scripts/test_collectors.py
```

**接机器人后的真机测试：**
```bash
python scripts/test_collectors.py --interface eth0
```

**What's next:**
- 电池充好后接网线测试真机通信
- 验证 `Go2VideoClient` 的实际 API（可能需要微调）
- 实现 master controller 与 camera_record_pipeline 同步

---
