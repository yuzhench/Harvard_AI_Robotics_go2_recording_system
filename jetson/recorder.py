"""
recorder.py — Orchestrates IMU / ego camera / LiDAR collectors as a single
recording session.

Owns state (idle | recording), constructs the session directory under
DATA_ROOT using camera_record_pipeline's naming convention
(`<task>/<MM_DD_YYYY>/<HH_MM_SS>/`), and delegates start/stop/save to each
collector.

One Recorder instance is created at daemon startup and reused across all
recording sessions. The collectors themselves are also constructed once —
their internal DDS subscribers and capture threads come and go with
start()/stop().
"""

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from utility.imu import IMUCollector
from utility.egocentric_camera import EgocentricCameraCollector
from utility.lidar import LidarCollector

from .config import DATA_ROOT, INTERFACE, TASKS


class Recorder:
    """Session orchestrator. Not thread-safe across start/stop calls; the
    HTTP layer must serialize them (FastAPI uvicorn worker pool provides
    enough isolation for the simple 'single active session' model here)."""

    def __init__(self, interface: str = INTERFACE, data_root: str = DATA_ROOT):
        self.interface = interface
        self.data_root = Path(data_root)

        self.imu   = IMUCollector(network_interface=interface)
        self.ego   = EgocentricCameraCollector(network_interface=interface)
        self.lidar = LidarCollector(network_interface=interface)

        self._lock = threading.Lock()
        self._state: str = "idle"                      # "idle" | "recording"
        self._session_dir: Optional[Path] = None
        self._task: Optional[str] = None
        self._prompt: Optional[str] = None
        self._start_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    @property
    def elapsed(self) -> float:
        if self._start_ts is None:
            return 0.0
        return time.time() - self._start_ts

    def samples(self) -> dict:
        """Live per-stream sample counts. Safe to read from any thread."""
        return {
            "imu":         self.imu.sample_count,
            "ego_frames":  self.ego.frame_count,
            "lidar_scans": self.lidar.scan_count,
        }

    def start(self, task: str, prompt: str = "") -> Path:
        """Begin a new recording session.

        Raises:
            ValueError   — task is not in TASKS
            RuntimeError — already recording
        """
        with self._lock:
            if self._state != "idle":
                raise RuntimeError(f"Already recording (state={self._state})")
            if task not in TASKS:
                raise ValueError(f"Unknown task {task!r}. Valid tasks: {TASKS}")

            now = datetime.now()
            session_dir = (
                self.data_root
                / task
                / now.strftime("%m_%d_%Y")
                / now.strftime("%H_%M_%S")
            )
            session_dir.mkdir(parents=True, exist_ok=True)

            self._session_dir = session_dir
            self._task = task
            self._prompt = prompt
            self._start_ts = time.time()
            self._state = "recording"

        # Start collectors outside the lock — each spawns its own threads
        # (DDS receivers, camera capture loop) internally.
        self.imu.start()
        self.ego.start()
        self.lidar.start()

        return session_dir

    def stop(self) -> dict:
        """Stop all collectors, flush to disk, return a summary dict.

        Raises:
            RuntimeError — not currently recording
        """
        with self._lock:
            if self._state != "recording":
                raise RuntimeError("Not recording")
            session_dir = self._session_dir
            task = self._task
            prompt = self._prompt
            elapsed = self.elapsed

        # Stop & save outside the lock — save() may take non-trivial time
        # (NPZ compression, LiDAR per-scan .npy writes).
        self.imu.stop()
        self.ego.stop()
        self.lidar.stop()

        self.imu.save(session_dir)
        self.ego.save(session_dir)
        self.lidar.save(session_dir)

        samples = self.samples()

        with self._lock:
            self._state = "idle"
            self._session_dir = None
            self._task = None
            self._prompt = None
            self._start_ts = None

        return {
            "session_dir":     str(session_dir),
            "task":            task,
            "prompt":          prompt,
            "elapsed_seconds": round(elapsed, 2),
            "samples":         samples,
        }
