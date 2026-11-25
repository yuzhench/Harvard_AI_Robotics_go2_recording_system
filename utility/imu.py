"""
imu.py — Collect IMU + joint states + foot contact from Go2 EDU.

Subscribes to rt/lowstate via unitree_sdk2_python (DDS/CycloneDDS) and
appends every sample to <save_dir>/lowstate.bin while recording. The file is
a flat array of LOWSTATE_DTYPE records; scripts/finalize_session.py splits
it into imu.npz / joints.npz / contacts.npz offline.

Usage:
    from utility.imu import IMUCollector

    collector = IMUCollector(network_interface="eth0")
    collector.start(Path("data/task1/session/first_person"))
    # ... robot is moving ...
    collector.stop()
"""

import struct
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .streaming import AppendFile, PauseGate, RateMeter

try:
    from unitree_sdk2py.core.channel import (
        ChannelSubscriber,
        ChannelFactoryInitialize,
    )
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("[IMUCollector] WARNING: unitree_sdk2py not found — real recording unavailable")


# Joint names matching motor_state[0..11]
JOINT_NAMES = [
    "FR_hip", "FR_thigh", "FR_calf",
    "FL_hip", "FL_thigh", "FL_calf",
    "RR_hip", "RR_thigh", "RR_calf",
    "RL_hip", "RL_thigh", "RL_calf",
]

FOOT_NAMES = ["FR", "FL", "RR", "RL"]

RAW_FILENAME = "lowstate.bin"

# One record per LowState message. Timestamp is time.time() on packet
# arrival (host clock).
LOWSTATE_DTYPE = np.dtype([
    ("timestamp",     "<f8"),
    ("quaternion",    "<f4", (4,)),    # (w, x, y, z)
    ("gyroscope",     "<f4", (3,)),    # rad/s
    ("accelerometer", "<f4", (3,)),    # m/s²
    ("rpy",           "<f4", (3,)),    # rad
    ("q",             "<f4", (12,)),   # rad
    ("dq",            "<f4", (12,)),   # rad/s
    ("tau",           "<f4", (12,)),   # N·m
    ("foot_force",    "<f4", (4,)),    # [FR, FL, RR, RL]
])
_RECORD = struct.Struct("<d53f")
assert _RECORD.size == LOWSTATE_DTYPE.itemsize

# Samples are packed in the DDS callback and handed to the disk in batches.
_FLUSH_INTERVAL_S = 0.5


class IMUCollector:
    """
    Subscribes to rt/lowstate and streams to disk:
      - IMU: quaternion (w,x,y,z), gyroscope (rad/s), accelerometer (m/s²), rpy (rad)
      - Joints: angle, velocity, torque for all 12 joints
      - Foot contact forces: 4 values
      - Timestamp: time.time() on packet arrival (host clock)
    """

    def __init__(self, network_interface: str = "eth0", mock: bool = False):
        self.network_interface = network_interface
        self.mock = mock
        self._error = None
        self._lock = threading.Lock()
        self._running = False
        self._subscriber = None
        self._mock_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._file: Optional[AppendFile] = None

        self._pending = bytearray()     # packed records not yet on disk
        self._sample_count = 0
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._rate = RateMeter()
        self._record_from = 0.0
        self._gate = PauseGate()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self, save_dir: Path, record_from: float = 0.0):
        """Open <save_dir>/lowstate.bin and begin streaming LowState data.

        Samples that arrive before `record_from` (Unix time) are discarded.
        """
        if self._running:
            return
        if not self.mock and not SDK_AVAILABLE:
            raise RuntimeError("IMUCollector: required driver is unavailable; mock mode must be explicit")
        self._error = None
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._pending = bytearray()
            self._sample_count = 0
            self._first_ts = None
            self._last_ts = None
        self._rate.reset()
        self._record_from = record_from
        self._gate.reset()
        self._file = AppendFile(save_dir / RAW_FILENAME)

        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_guard, daemon=True)
        self._writer_thread.start()

        if self.mock:
            self._start_mock()
            return

        # Subscribe once and keep it for the life of the process (the callback
        # ignores messages while not recording). Subscribing again on every
        # start() would deliver each message once per subscriber.
        if self._subscriber is None:
            subscriber = ChannelSubscriber("rt/lowstate", LowState_)
            subscriber.Init(self._on_lowstate, 10)
            self._subscriber = subscriber
            print(f"[IMUCollector] Subscribed to rt/lowstate on {self.network_interface}")

    def stop(self) -> dict:
        """Stop streaming, flush what is left, close the file."""
        if not self._running:
            return {"samples": 0}
        with self._lock:
            self._running = False
        if self._mock_thread is not None:
            self._mock_thread.join()
            self._mock_thread = None
        if self._writer_thread is not None:
            self._writer_thread.join()
            self._writer_thread = None
        try:
            self._flush()
        except Exception as exc:
            self._error = self._error or exc
        if self._file is not None:
            try:
                self._file.close()
            except Exception as exc:
                self._error = self._error or exc
            self._file = None

        if self._error is not None:
            raise RuntimeError(f"Disk writer failed: {self._error}") from self._error
        n = self._sample_count
        duration = (
            round(self._last_ts - self._first_ts - self._gate.gap_within(self._first_ts, self._last_ts), 2)
            if n > 1 and self._first_ts is not None else 0.0
        )
        actual_hz = round((n - 1) / duration, 1) if duration > 0 else 0.0
        print(f"[IMUCollector] Stopped. {n} samples, {duration}s, ~{actual_hz} Hz")
        return {"samples": n, "duration_s": duration, "hz": actual_hz}

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def recent_hz(self) -> float:
        """Samples received in the last second. Zero when not recording."""
        return self._rate.hz() if self._running else 0.0

    def pause(self):
        """Drop data until resume(); files stay open."""
        self._gate.pause(time.time())

    def resume(self):
        self._gate.resume(time.time())

    # ------------------------------------------------------------------
    # DDS callback
    # ------------------------------------------------------------------

    def _on_lowstate(self, msg: "LowState_"):
        if not self._running:
            return

        ts = time.time()
        imu = msg.imu_state
        self._append(
            ts,
            imu.quaternion, imu.gyroscope, imu.accelerometer, imu.rpy,
            [msg.motor_state[i].q       for i in range(12)],
            [msg.motor_state[i].dq      for i in range(12)],
            [msg.motor_state[i].tau_est for i in range(12)],
            msg.foot_force[:4],
        )

    def _append(self, ts, quat, gyro, accel, rpy, q, dq, tau, foot):
        self._rate.tick(ts)
        if ts < self._record_from or self._gate.paused:
            return
        record = _RECORD.pack(ts, *quat, *gyro, *accel, *rpy, *q, *dq, *tau, *foot)
        with self._lock:
            if not self._running or self._error is not None:
                return
            if len(self._pending) >= _RECORD.size * 5000:
                self._error = RuntimeError("IMU disk backlog exceeded 5000 samples")
                return
            self._pending += record
            self._sample_count += 1
            if self._first_ts is None:
                self._first_ts = ts
            self._last_ts = ts

    # ------------------------------------------------------------------
    # Disk writer
    # ------------------------------------------------------------------

    def _writer_guard(self):
        try:
            self._writer_loop()
        except Exception as exc:
            self._error = exc
            print(f"[Collector] Disk writer failed: {exc}")

    def _writer_loop(self):
        while self._running:
            time.sleep(_FLUSH_INTERVAL_S)
            self._flush()

    def _flush(self):
        with self._lock:
            if not self._pending:
                return
            data, self._pending = self._pending, bytearray()
        if self._file is not None:
            self._file.write(data)

    # ------------------------------------------------------------------
    # Mock mode (no SDK / no robot)
    # ------------------------------------------------------------------

    def _start_mock(self):
        print("[IMUCollector] Mock mode: generating synthetic data at 100 Hz")
        self._mock_thread = threading.Thread(target=self._mock_loop, daemon=True)
        self._mock_thread.start()

    def _mock_loop(self):
        interval = 1.0 / 100.0  # 100 Hz mock
        while self._running:
            t = time.time()
            self._append(
                t,
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 0.01 * np.sin(t)],
                [0.0, 0.0, -9.81],
                [0.0, 0.0, 0.0],
                [0.0] * 12, [0.0] * 12, [0.0] * 12,
                [10.0, 10.0, 10.0, 10.0],
            )
            time.sleep(interval)
