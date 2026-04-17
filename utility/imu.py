"""
imu.py — Collect IMU + joint states + foot contact from Go2 EDU.

Subscribes to rt/lowstate via unitree_sdk2_python (DDS/CycloneDDS).
Buffers all samples in memory, flushes to NPZ on stop().

Usage:
    from utility.imu import IMUCollector

    collector = IMUCollector(network_interface="eth0")
    collector.start()
    # ... robot is moving ...
    collector.stop()
    collector.save(Path("data/task1/session/robot"))
"""

import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

try:
    from unitree_sdk2py.core.channel import (
        ChannelSubscriber,
        ChannelFactoryInitialize,
    )
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("[IMUCollector] WARNING: unitree_sdk2py not found — running in mock mode")


# Joint names matching motor_state[0..11]
JOINT_NAMES = [
    "FR_hip", "FR_thigh", "FR_calf",
    "FL_hip", "FL_thigh", "FL_calf",
    "RR_hip", "RR_thigh", "RR_calf",
    "RL_hip", "RL_thigh", "RL_calf",
]

FOOT_NAMES = ["FR", "FL", "RR", "RL"]


class IMUCollector:
    """
    Subscribes to rt/lowstate and buffers:
      - IMU: quaternion (w,x,y,z), gyroscope (rad/s), accelerometer (m/s²), rpy (rad)
      - Joints: angle, velocity, torque for all 12 joints
      - Foot contact forces: 4 values (N)
      - Timestamp: time.time() on packet arrival (host clock)

    All data stored as deques, converted to numpy arrays on save().
    """

    def __init__(self, network_interface: str = "eth0"):
        self.network_interface = network_interface
        self._lock = threading.Lock()
        self._running = False
        self._subscriber = None
        self._mock_thread = None

        # Buffers — one entry per LowState callback
        self._timestamps   = deque()
        self._imu_quat     = deque()   # (w, x, y, z)
        self._imu_gyro     = deque()   # (gx, gy, gz) rad/s
        self._imu_accel    = deque()   # (ax, ay, az) m/s²
        self._imu_rpy      = deque()   # (roll, pitch, yaw) rad
        self._joint_q      = deque()   # (12,) rad
        self._joint_dq     = deque()   # (12,) rad/s
        self._joint_tau    = deque()   # (12,) N·m
        self._foot_force   = deque()   # (4,) N

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self):
        """Initialize DDS and begin buffering LowState data."""
        if self._running:
            return

        self._running = True
        self._clear_buffers()

        if not SDK_AVAILABLE:
            self._start_mock()
            return

        self._subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self._subscriber.Init(self._on_lowstate, 10)
        print(f"[IMUCollector] Subscribed to rt/lowstate on {self.network_interface}")

    def stop(self):
        """Stop buffering. Data remains in memory until save() is called."""
        self._running = False
        if self._mock_thread is not None:
            self._mock_thread.join(timeout=2.0)
            self._mock_thread = None
        print(f"[IMUCollector] Stopped. Buffered {len(self._timestamps)} samples.")

    def save(self, save_dir: Path):
        """
        Flush all buffered data to NPZ files in save_dir.

        Outputs:
            imu.npz      — quaternion, gyro, accel, rpy, timestamps
            joints.npz   — q, dq, tau, timestamps  (shape: N × 12)
            contacts.npz — foot_force, timestamps  (shape: N × 4)
        """
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            ts         = np.array(self._timestamps,  dtype=np.float64)
            imu_quat   = np.array(self._imu_quat,    dtype=np.float32)  # (N, 4)
            imu_gyro   = np.array(self._imu_gyro,    dtype=np.float32)  # (N, 3)
            imu_accel  = np.array(self._imu_accel,   dtype=np.float32)  # (N, 3)
            imu_rpy    = np.array(self._imu_rpy,     dtype=np.float32)  # (N, 3)
            joint_q    = np.array(self._joint_q,     dtype=np.float32)  # (N, 12)
            joint_dq   = np.array(self._joint_dq,    dtype=np.float32)  # (N, 12)
            joint_tau  = np.array(self._joint_tau,   dtype=np.float32)  # (N, 12)
            foot_force = np.array(self._foot_force,  dtype=np.float32)  # (N, 4)

        np.savez_compressed(
            save_dir / "imu.npz",
            timestamps  = ts,
            quaternion  = imu_quat,
            gyroscope   = imu_gyro,
            accelerometer = imu_accel,
            rpy         = imu_rpy,
        )

        np.savez_compressed(
            save_dir / "joints.npz",
            timestamps  = ts,
            q           = joint_q,
            dq          = joint_dq,
            tau         = joint_tau,
            joint_names = np.array(JOINT_NAMES),
        )

        np.savez_compressed(
            save_dir / "contacts.npz",
            timestamps  = ts,
            foot_force  = foot_force,
            foot_names  = np.array(FOOT_NAMES),
        )

        n = len(ts)
        duration = round(float(ts[-1] - ts[0]), 2) if n > 1 else 0.0
        actual_hz = round(n / duration, 1) if duration > 0 else 0.0
        print(f"[IMUCollector] Saved {n} samples, {duration}s, ~{actual_hz} Hz → {save_dir}")
        return {"samples": n, "duration_s": duration, "hz": actual_hz}

    @property
    def sample_count(self) -> int:
        return len(self._timestamps)

    # ------------------------------------------------------------------
    # DDS callback
    # ------------------------------------------------------------------

    def _on_lowstate(self, msg: "LowState_"):
        if not self._running:
            return

        ts = time.time()
        imu = msg.imu_state

        with self._lock:
            self._timestamps.append(ts)
            self._imu_quat.append(list(imu.quaternion))      # [w, x, y, z]
            self._imu_gyro.append(list(imu.gyroscope))       # [gx, gy, gz]
            self._imu_accel.append(list(imu.accelerometer))  # [ax, ay, az]
            self._imu_rpy.append(list(imu.rpy))              # [roll, pitch, yaw]

            q   = [msg.motor_state[i].q         for i in range(12)]
            dq  = [msg.motor_state[i].dq        for i in range(12)]
            tau = [msg.motor_state[i].tau_est   for i in range(12)]
            self._joint_q.append(q)
            self._joint_dq.append(dq)
            self._joint_tau.append(tau)

            self._foot_force.append(list(msg.foot_force[:4]))

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
            with self._lock:
                self._timestamps.append(t)
                self._imu_quat.append([1.0, 0.0, 0.0, 0.0])
                self._imu_gyro.append([0.0, 0.0, 0.01 * np.sin(t)])
                self._imu_accel.append([0.0, 0.0, -9.81])
                self._imu_rpy.append([0.0, 0.0, 0.0])
                self._joint_q.append([0.0] * 12)
                self._joint_dq.append([0.0] * 12)
                self._joint_tau.append([0.0] * 12)
                self._foot_force.append([10.0, 10.0, 10.0, 10.0])
            time.sleep(interval)

    def _clear_buffers(self):
        with self._lock:
            self._timestamps.clear()
            self._imu_quat.clear()
            self._imu_gyro.clear()
            self._imu_accel.clear()
            self._imu_rpy.clear()
            self._joint_q.clear()
            self._joint_dq.clear()
            self._joint_tau.clear()
            self._foot_force.clear()
