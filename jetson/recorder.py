"""
recorder.py — Orchestrates IMU / ego camera / LiDAR collectors as a single
recording session.

Owns state (idle | recording | saving, plus paused while recording), constructs the session directory
under DATA_ROOT using camera_record_pipeline's naming convention
(`<task>/<MM_DD_YYYY>/<HH_MM_SS>/`), and delegates start/stop to each
collector.

pause()/resume() keep the session open but drop all data in between: the
session is one folder whose timestamps jump over each pause, and the pauses
are listed in session_meta.json ("pauses": [[start, end], ...], Unix time).

Collectors write to disk while recording, so stop() drains pending writes and closes
files. What lands in the session directory is the raw stream layout;
scripts/finalize_session.py converts it to the final layout offline.

One Recorder instance is created at daemon startup and reused across all
recording sessions. The collectors themselves are also constructed once —
DDS subscribers persist, while capture and writer threads are restarted
for each session.
"""

import json
import re
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from utility.imu import IMUCollector
from utility.egocentric_camera import EgocentricCameraCollector
from utility.lidar import LidarCollector

from .config import DATA_ROOT, INTERFACE, SCREEN_TASK, START_DELAY_S, TASKS

SESSION_META_FILENAME = "session_meta.json"


class RecorderStateError(RuntimeError):
    """The requested lifecycle transition is not allowed."""


class RecordingError(RuntimeError):
    """A collector or session write failed."""


class Recorder:
    """Session orchestrator. start()/stop() may be called from any thread
    (HTTP workers, the screen link); the state check under the lock makes
    sure only one of two racing calls goes through."""

    def __init__(self, interface: str = INTERFACE, data_root: str = DATA_ROOT, mock: bool = False):
        self.interface = interface
        self.data_root = Path(data_root)

        self.imu   = IMUCollector(network_interface=interface, mock=mock)
        self.ego   = EgocentricCameraCollector(network_interface=interface, mock=mock)
        self.lidar = LidarCollector(network_interface=interface, mock=mock)

        self.operation_lock = threading.RLock()
        self.mock = mock
        self._lock = threading.Lock()
        self._state: str = "idle"                      # "idle" | "recording" | "saving"
        self._session_dir: Optional[Path] = None
        self._task: Optional[str] = None
        self._prompt: Optional[str] = None
        self._start_ts: Optional[float] = None
        self._paused_at: Optional[float] = None     # start of the current pause
        self._pauses: list = []                      # finished pauses of this session, [start, end]

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
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def elapsed(self) -> float:
        """Seconds of data kept: zero during the start delay, pauses left out."""
        if self._start_ts is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else time.time()
        return max(0.0, end - self._start_ts - sum(b - a for a, b in self._pauses))

    def samples(self) -> dict:
        """Live per-stream sample counts. Safe to read from any thread."""
        return {
            "imu":         self.imu.sample_count,
            "ego_frames":  self.ego.frame_count,
            "lidar_scans": self.lidar.scan_count,
        }

    def live_fps(self) -> dict:
        """Live per-stream fps (sliding 1s window). Zero when not recording."""
        return {
            "ego": round(self.ego.recent_fps, 1),
        }

    def live_hz(self) -> dict:
        """Live rate of every monitored stream (sliding 1s window).

        IMU, joints and foot forces arrive in the same rt/lowstate message,
        so they always share one rate.
        """
        lowstate = 0.0 if self.imu._error else round(self.imu.recent_hz, 1)
        return {
            "cam":   0.0 if self.ego._error else round(self.ego.recent_fps, 1),
            "lidar": 0.0 if self.lidar._error else round(self.lidar.recent_hz, 1),
            "imu":   lowstate,
            "joint": lowstate,
            "foot":  lowstate,
        }

    def start(self, task, prompt="", session_date=None, session_id=None):
        with self.operation_lock:
            return self._start(task, prompt, session_date, session_id)

    def _start(
        self,
        task: str,
        prompt: str = "",
        session_date: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Path:
        """Begin a new recording session.

        If `session_date` / `session_id` are provided (by the laptop over
        HTTP), validate their date/time formats and reject collisions so
        both hosts keep the same name. Otherwise use local `datetime.now()`.

        Raises:
            ValueError   — task is not in TASKS
            RuntimeError — already recording, or a collector failed to start
        """
        with self._lock:
            if self._state != "idle":
                raise RecorderStateError(f"Already recording (state={self._state})")
            if task not in TASKS and task != SCREEN_TASK:
                raise ValueError(f"Unknown task {task!r}. Valid tasks: {TASKS}")

            supplied_id = session_id is not None
            for value, pattern, label in (
                (session_date, r"[0-9]{2}_[0-9]{2}_[0-9]{4}", "session_date"),
                (session_id, r"[0-9]{2}_[0-9]{2}_[0-9]{2}", "session_id"),
            ):
                if value is not None and not re.fullmatch(pattern, value):
                    raise ValueError(f"Invalid {label}: {value!r}")
            if session_date is not None:
                datetime.strptime(session_date, "%m_%d_%Y")
            if session_id is not None:
                datetime.strptime(session_id, "%H_%M_%S")

            if session_date is None or session_id is None:
                now = datetime.now()
                session_date = session_date or now.strftime("%m_%d_%Y")
                session_id   = session_id   or now.strftime("%H_%M_%S")

            # Session layout (mirrors laptop side so rsync merges cleanly):
            #   <DATA_ROOT>/<task>/<date>/<time>/first_person/
            # Never reuse an existing folder: after a reboot without a time
            # source the clock can repeat, and two sessions must not share
            # (and overwrite) one directory.
            date_dir = self.data_root / task / session_date
            unique_id, n = session_id, 2
            if supplied_id and (date_dir / unique_id).exists():
                raise RecorderStateError("Requested session already exists; choose a new session ID")
            while (date_dir / unique_id).exists():
                unique_id = f"{session_id}_{n}"
                n += 1
            session_dir = date_dir / unique_id / "first_person"
            session_dir.mkdir(parents=True)

            self._session_dir = session_dir
            self._task = task
            self._prompt = prompt
            # Data is kept from this instant on; the collectors are started
            # now; slow hardware may not be ready by the cutoff.
            record_from = time.time() + START_DELAY_S
            self._start_ts = record_from
            self._paused_at = None
            self._pauses = []
            self._state = "recording"

        # Keep the operation lock while collectors start; the state lock is free.
        # Each collector spawns its own threads
        # (DDS receivers, camera capture loop, disk writer) internally.
        try:
            self._write_meta(session_dir, {
                "task": task, "prompt": prompt, "start_time": record_from,
                "clean_stop": False, "mock": self.mock,
            })
            self.imu.start(session_dir, record_from)
            self.ego.start(session_dir, record_from)
            self.lidar.start(session_dir, record_from)
        except Exception as e:
            # E.g. the RealSense is unplugged. Shut down whatever did start
            # and go back to idle so the next attempt is not locked out.
            self._stop_collectors()
            self._reset()
            raise RecordingError(f"Collector failed to start: {e}") from e

        return session_dir

    def pause(self):
        """Stop keeping data until resume(); the session stays open."""
        with self.operation_lock:
            with self._lock:
                if self._state != "recording":
                    raise RecorderStateError("Not recording")
                if self._paused_at is not None:
                    raise RecorderStateError("Already paused")
                now = time.time()
                if now < self._start_ts:
                    raise RecorderStateError("Recording has not begun yet")
                self._paused_at = now
            for collector in (self.imu, self.ego, self.lidar):
                collector.pause()

    def resume(self):
        with self.operation_lock:
            with self._lock:
                if self._state != "recording" or self._paused_at is None:
                    raise RecorderStateError("Not paused")
                for collector in (self.imu, self.ego, self.lidar):
                    collector.resume()
                self._pauses.append([self._paused_at, time.time()])
                self._paused_at = None

    def stop(self) -> dict:
        with self.operation_lock:
            return self._stop()

    def _stop(self) -> dict:
        """Stop all collectors, close their files, return a summary dict.

        State transitions: recording → saving → idle.

        Raises:
            RuntimeError — not currently recording
        """
        with self._lock:
            if self._state != "recording":
                raise RecorderStateError("Not recording")
            session_dir = self._session_dir
            task = self._task
            prompt = self._prompt
            start_ts = self._start_ts
            elapsed = self.elapsed
            pauses = [list(p) for p in self._pauses]
            if self._paused_at is not None:          # stopped while paused
                pauses.append([self._paused_at, time.time()])
            # "saving" covers the writer threads draining their queues.
            self._state = "saving"

        try:
            info = self._stop_collectors()
            samples = self.samples()
            errors = {name: item["error"] for name, item in info.items() if "error" in item}
            self._write_meta(session_dir, {
                "task":            task,
                "prompt":          prompt,
                "start_time":      start_ts,
                "elapsed_seconds": round(elapsed, 2),
                "pauses":          pauses,
                "samples":         samples,
                "collectors":      info,
                "clean_stop":      not errors,
                "mock":            self.mock,
            })
        finally:
            self._reset()

        if errors:
            raise RecordingError(f"Recording did not save cleanly: {errors}")
        return {
            "session_dir":     str(session_dir),
            "task":            task,
            "prompt":          prompt,
            "elapsed_seconds": round(elapsed, 2),
            "samples":         samples,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _stop_collectors(self) -> dict:
        info = {}
        collectors = {"imu": self.imu, "ego": self.ego, "lidar": self.lidar}
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {name: pool.submit(collector.stop) for name, collector in collectors.items()}
            for name, future in pending.items():
                try:
                    info[name] = future.result()
                except Exception as exc:
                    print(f"[Recorder] {name} failed to stop cleanly: {exc}")
                    info[name] = {"error": str(exc)}
        return info

    def _reset(self):
        with self._lock:
            self._state = "idle"
            self._session_dir = None
            self._task = None
            self._prompt = None
            self._start_ts = None
            self._paused_at = None
            self._pauses = []

    @staticmethod
    def _write_meta(session_dir: Path, meta: dict):
        target = session_dir / SESSION_META_FILENAME
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(meta, indent=2))
        temp.replace(target)
