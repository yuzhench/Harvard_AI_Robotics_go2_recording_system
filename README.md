# Go2 Record Pipeline

Synchronized first-person data collection for the **Unitree Go2 EDU**
quadruped. Runs on the on-board Jetson as an HTTP daemon and streams
IMU / joints / foot contact / ego RGB-D / LiDAR to disk, in lockstep
with the laptop-side [camera_record_pipeline](https://github.com/yuzhench/Harvard_AI_Robotics_cameras_recording_system)
(4× third-person RealSense D435I).

```
┌──────────── Laptop ─────────────┐         ┌──────────── Jetson (on Go2) ──────────┐
│  Browser UI  ────HTTP────▶      │  WiFi   │   record_daemon :8010                 │
│  camera_record_pipeline :8000   ├◀──────▶─┤   ├── IMU / joints / contacts (DDS)   │
│  4× RealSense (third-person)    │         │   ├── Ego RealSense D435I (USB3)      │
└─────────────────────────────────┘         │   └── LiDAR (DDS)                     │
                ↕ chrony NTP                │              ↕ CycloneDDS → Go2 main  │
                (clock sync < 5 ms)         └───────────────────────────────────────┘
```

All streams share one **Unix epoch timeline** (`time.time()` on each host,
aligned via chrony). Post-hoc alignment is a straightforward timestamp
interpolation — no per-stream calibration needed.

---

## What this repo contains

| Path | Purpose |
|---|---|
| `jetson/record_daemon.py` | FastAPI service on the Jetson. Exposes `/start`, `/stop`, `/status`, `/resync_clock`. |
| `jetson/recorder.py` | Session orchestrator (idle → recording → saving state machine). |
| `jetson/config.py` | `DATA_ROOT`, DDS interface, port, task list, protocol version. |
| `utility/imu.py` | 500 Hz DDS subscriber on `rt/lowstate`. |
| `utility/egocentric_camera.py` | On-robot RealSense D435I collector (USB3, 30 Hz). |
| `utility/lidar.py` | Go2 LiDAR point-cloud collector. |
| `scripts/keyboard_control.py` | Manual robot teleoperation (arrow keys). |
| `GO_NOTES/` | Detailed Chinese notes — architecture, chrony setup, network topology. |

---

## Quick start

This runs **on the Jetson**, controlled **from a laptop**.

### 1. Deploy code to Jetson

```bash
# From laptop, with passwordless SSH already set up
rsync -avz ~/Desktop/Research/Harvard_AI/go2_record_pipeline/ \
           unitree@<jetson_ip>:~/Desktop/go2_record_pipeline/
```

### 2. Install on Jetson (first time only)

```bash
ssh unitree@<jetson_ip>
cd ~/Desktop/go2_record_pipeline
conda activate go2        # or python 3.10+ venv
pip install -e unitree_sdk2_python/
pip install -r requirements.txt
```

Also install `pyrealsense2` for the ego camera — on aarch64 Jetson this
must be **built from source** (no PyPI wheel exists for ARM). See
[GO_NOTES/control_architecture.md](GO_NOTES/control_architecture.md) §19 neighbors.

### 3. Start the daemon on Jetson

```bash
sudo systemctl start record_daemon        # production
# or for interactive debugging:
python -m jetson.record_daemon
```

### 4. Control from laptop

Point your `camera_record_pipeline` backend at this Jetson via
`JETSON_URL=http://<jetson_ip>:8010` and use the web UI's Start / Stop
buttons. Done — data lands on both machines simultaneously.

---

## Data output

Each recording creates one session directory. After `rsync`-ing the
Jetson's `DATA_ROOT` to the laptop, a full merged session looks like:

```
<DATA_ROOT>/<task>/<MM_DD_YYYY>/<HH_MM_SS>/
├── first_person/          ← this repo writes here (on Jetson)
│   ├── imu.npz            ← timestamps, quaternion, gyroscope, accelerometer, rpy
│   ├── joints.npz         ← timestamps, q (Nx12), dq (Nx12), tau (Nx12)
│   ├── contacts.npz       ← timestamps, foot_force (Nx4) [FR, FL, RR, RL]
│   ├── ego_cam/
│   │   ├── rgb.mp4            H.264 video
│   │   ├── rgb_timestamps.npy (N,) float64 Unix epoch
│   │   ├── depth.npz          {depth: (N,H,W) uint16, timestamps: (N,) float64}
│   │   └── intrinsics.json
│   └── lidar/...
└── third_person/          ← written by camera_record_pipeline on laptop
    └── cam{0..3}/...
```

**Key invariant**: every `.npy` / `.npz` timestamp field in both halves
is on the same Unix-epoch timeline. Align by interpolation, no
calibration constants required.

---

<details>
<summary><b>Time synchronization (chrony, clock < 5 ms)</b></summary>

Both laptop(s) run `chrony` as NTP servers; the Jetson runs `chrony`
as a client pointed at them. Measured offset is typically **< 100 μs**
on LAN, target was **< 5 ms**.

The UI has a **⟳ Resync** button that triggers
`sudo systemctl restart chrony` on the Jetson, forcing an immediate
iburst re-sync. Recommended before each recording session.

Jetson config uses `makestep 0.001 3`:
- First 3 updates after restart: step (instant jump) if offset > 1 ms
- After that: slew only — **clock never jumps during recording**, so
  all timestamps stay monotonic.

Full setup and rationale: **[GO_NOTES/control_architecture.md §19](GO_NOTES/control_architecture.md)**.

</details>

<details>
<summary><b>HTTP API</b></summary>

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/start` | Begin recording. Body: `{"task": "task1", "prompt": "..."}` |
| `POST` | `/stop` | Stop all collectors and flush to disk. Returns sample counts per stream. |
| `POST` | `/resync_clock` | Restart chrony, wait for convergence, return measured offset. |
| `GET` | `/status` | Current state (idle / recording / saving), live sample counts, uptime, protocol version. |

Protocol version must match `camera_record_pipeline` (currently `4`).
See `jetson/config.py` for tasks and `DATA_ROOT`.

</details>

<details>
<summary><b>Architecture: state machine and concurrency</b></summary>

One `Recorder` instance lives for the daemon's lifetime. It owns three
collectors (IMU, ego camera, LiDAR). On `/start` the collectors spin up
their own capture threads; on `/stop` they stop and flush to disk
(LiDAR `.npz` compression is the slow path, up to 30 s).

State machine:

```
[idle] ──/start──▶ [recording] ──/stop──▶ [saving] ──(flush done)──▶ [idle]
```

`/start` rejected with 409 while recording. `/stop` rejected with 409
while idle. Force-stop via the UI's reset button calls `/stop` and
ignores 409.

Laptop-side `camera_record_pipeline` uses a 60 s timeout for `/stop`
because Jetson save can be long. If it times out, the UI shows a yellow
warning (the save is likely still running) rather than a red error.

</details>

<details>
<summary><b>Deployment & daemon management</b></summary>

```bash
# Inspect daemon logs (if systemd-managed)
ssh unitree@<jetson_ip> "journalctl -u record_daemon -f"

# Restart after a code change
rsync -avz go2_record_pipeline/ unitree@<jetson_ip>:~/Desktop/go2_record_pipeline/
ssh unitree@<jetson_ip> "sudo systemctl restart record_daemon"

# Pull collected data back to laptop
rsync -avzP unitree@<jetson_ip>:/home/unitree/GO2_DATA/ ~/GO2_DATA/
```

Passwordless sudo for `systemctl restart chrony` must be granted to the
`unitree` user for the UI Resync button to work:

```
# /etc/sudoers.d/chrony-restart
unitree ALL=(root) NOPASSWD: /usr/bin/systemctl restart chrony
```

</details>

<details>
<summary><b>Timezone gotcha</b></summary>

Jetson ships with `Asia/Shanghai` (Unitree factory default). This does
**not** affect `time.time()` — chrony aligns UTC epoch regardless — but
it does affect directory names generated via `datetime.now()`, causing
session folders on the Jetson to land on a different date than the
laptop's. Fix:

```bash
sudo timedatectl set-timezone America/New_York
sudo systemctl restart record_daemon   # Python caches TZ at process start
```

</details>

---

## Requirements

- Unitree Go2 EDU (EDU variant required — only it exposes `unitree_sdk2` APIs)
- On-robot Jetson (tested on Orin NX)
- Python 3.10+, conda env `go2`
- `cyclonedds==0.10.2` (exact version — bundled by `unitree_sdk2_python`)
- `pyrealsense2` built from source for aarch64 (for the ego camera)

---

## Related

- [camera_record_pipeline](https://github.com/yuzhench/Harvard_AI_Robotics_cameras_recording_system) — laptop-side third-person cameras
- [GO_NOTES/control_architecture.md](GO_NOTES/control_architecture.md) — full Chinese architecture notes, time-sync setup, network topology
