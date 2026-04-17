# Changelog — Technical

Tracks code changes, API decisions, file modifications, and technical rationale.
Most recent entries are at the top.

Format per entry:
```
## YYYY-MM-DD — <short title>
Files: <created/modified>
Changes: <what changed at code level>
Rationale: <why this approach>
Issues/Notes: <known issues, TODOs, open questions>
```

---

## 2026-04-07 — utility/ collectors implemented

**Files created:**
- `utility/__init__.py`
- `utility/imu.py` — `IMUCollector`
- `utility/egocentric_camera.py` — `EgocentricCameraCollector`
- `scripts/test_collectors.py` — sanity test script

**IMUCollector design:**
- Subscribes to `rt/lowstate` via `ChannelSubscriber`
- Callback `_on_lowstate()` appends to deques under lock (non-blocking)
- Buffers: timestamps, imu_quat(N,4), imu_gyro(N,3), imu_accel(N,3), imu_rpy(N,3), joint_q(N,12), joint_dq(N,12), joint_tau(N,12), foot_force(N,4)
- `save(dir)` → imu.npz, joints.npz, contacts.npz (np.savez_compressed)
- Mock mode at 100 Hz when SDK unavailable

**EgocentricCameraCollector design:**
- Uses `Go2VideoClient.GetImageSample()` → returns `(code, jpeg_bytes)`
- Decodes JPEG with `cv2.imdecode`, resizes to target resolution if needed
- Capture loop runs at `TARGET_FPS=30` in a daemon thread
- `save(dir)` → ego_rgb.mp4 (mp4v codec) + ego_timestamps.npy
- Mock mode generates synthetic BGR frames

**Interface contract (both collectors):**
```python
collector.start()          # initialize + begin buffering
collector.stop()           # stop buffering, keep data in memory
collector.save(Path(...))  # flush to disk, returns info dict
```

**Known uncertainty:**
- `Go2VideoClient.GetImageSample()` return signature needs verification on real hardware
  — assumed `(int code, bytes data)` based on SDK examples; may differ
- Go2 camera native resolution: assumed 1280×720, needs confirmation

---

## 2026-04-07 — Project Bootstrap

**Files created:**
- `README.md`
- `PLAN.md`
- `logs/changelog_human.md`
- `logs/changelog_technical.md` (this file)
- `go2_bridge/` (empty dir)
- `data/` (empty dir)
- `scripts/` (empty dir)

**Analysis of camera_record_pipeline:**

```
camera_record_pipeline/
├── backend/
│   ├── camera.py      RealSenseCamera, MockCamera, CameraManager
│   ├── recorder.py    Recorder — per-camera MP4+NPZ writer
│   ├── server.py      FastAPI app, /start /stop REST endpoints
│   ├── config.py      TASKS list, DEFAULT_FPS/WIDTH/HEIGHT, DATA_ROOT
│   └── utility.py     stats, pointcloud helpers
└── frontend/index.html
```

Key API contract (for integration):

```http
POST /start
Content-Type: application/json
{"task": "task1", "prompt": "..."}

Response 200:
{"status": "started", "session_dir": "data/task1/04_07_2026/14_32_05", "cameras": [1,2,3,4]}

POST /stop
Response 200:
{"status": "stopped", "session_dir": "...", "elapsed_seconds": 12.4, "cameras": {...}}
```

Session directory format: `DATA_ROOT/<task>/<MM_DD_YYYY>/<HH_MM_SS>/`
- `DATA_ROOT` defaults to `"data"` (relative to server CWD)
- Absolute path returned in `/start` response

Per-camera output (in session_dir):
```
cam{N}/
├── rgb.mp4          # H.264, bgr8, (fps × width × height)
├── depth.npz        # arrays: depth(N,H,W) uint16, timestamps(N,) float64
└── intrinsics.json  # fx,fy,ppx,ppy,distortion,depth_scale,serial,name
```

Timestamp source: `time.time()` set in `camera.py:_capture_loop()` line 129.

**Go2 EDU SDK interface (planned):**

```python
# unitree_sdk2_python — DDS subscriber pattern
from unitree_sdk2py.core.channel import ChannelSubscriber
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

sub = ChannelSubscriber("rt/lowstate", LowState_)
sub.Init(callback_fn, 10)  # queue depth=10
```

LowState_ fields accessed:
```python
msg.imu_state.accelerometer   # [3] float32
msg.imu_state.gyroscope       # [3] float32
msg.imu_state.quaternion      # [4] float32 (w,x,y,z)
msg.imu_state.rpy             # [3] float32

msg.motor_state[i].q          # joint angle rad
msg.motor_state[i].dq         # joint velocity rad/s
msg.motor_state[i].tau_est    # torque N·m

msg.foot_force[i]             # float32, 4 values: FR,FL,RR,RL
```

**Synchronization design:**

```
t0 = time.time()   # recorded by master controller on /start

Go2 data:  ts[i] = time.time() at SDK callback arrival (host clock)
Camera data: ts[j] = time.time() at frame capture (same host clock)

Alignment: for each robot sample ts_r, find camera frame with min |ts_c - ts_r|
Max expected desync: < 5ms (same host, single machine clock)
```

**Buffer strategy for Go2DataCollector:**
- Lists pre-allocated with `deque(maxlen=100000)` to avoid unbounded growth
- On `stop()`, convert to numpy arrays and `np.savez_compressed()`
- Thread: SDK callback is on DDS thread → append to deque with lock

**Known issues / open questions:**
- `unitree_sdk2_python` package name may vary by install method (pip vs build from source)
- Go2 IP address: `192.168.123.161` (Go2 AP mode) vs DHCP when connected to router
- SDK version: v1.0 uses `unitree_legged_sdk`, v2.0 uses `unitree_sdk2py` — need to confirm
- Internal camera stream: `rt/utlidar/camera_data` topic — message type unknown, needs testing
- Data directory: should robot data be co-located with camera data under same session_dir, or in separate tree? → Decision: co-located under same session_dir for easier loading

---
