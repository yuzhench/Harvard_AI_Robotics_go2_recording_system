"""
lidar.py — Collect 4D LiDAR L1 point clouds from Go2 EDU.

Subscribes to rt/utlidar/cloud (sensor_msgs::PointCloud2) over DDS and
buffers each scan in memory. On save() writes one .npy per scan plus a
JSON sidecar with the original field layout.

Native rate for the Go2 L1 is ~11 Hz scans (~21,600 pts/s), so at ~11 Hz
each scan has ~2,000 points. The collector does not rate-limit — it
consumes every scan the DDS topic publishes.

Usage:
    from utility.lidar import LidarCollector

    collector = LidarCollector(network_interface="eth0")
    collector.start()
    # ... robot is moving ...
    collector.stop()
    collector.save(Path("data/task1/session/robot"))
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("[LidarCollector] WARNING: unitree_sdk2py not found — running in mock mode")


# Go2 EDU 4D LiDAR L1 native scan rate (hardware maximum).
# Source: Unitree 4D LiDAR-L1 User Manual v1.1 — 方位 11 Hz, 采样 21,600 pts/s.
NATIVE_SCAN_HZ = 11
LIDAR_TOPIC_DEFAULT = "rt/utlidar/cloud"


# sensor_msgs::PointField datatype codes → numpy dtypes
PF_INT8, PF_UINT8 = 1, 2
PF_INT16, PF_UINT16 = 3, 4
PF_INT32, PF_UINT32 = 5, 6
PF_FLOAT32, PF_FLOAT64 = 7, 8

_PF_NUMPY = {
    PF_INT8:    np.int8,
    PF_UINT8:   np.uint8,
    PF_INT16:   np.int16,
    PF_UINT16:  np.uint16,
    PF_INT32:   np.int32,
    PF_UINT32:  np.uint32,
    PF_FLOAT32: np.float32,
    PF_FLOAT64: np.float64,
}


@dataclass
class _ParsedScan:
    timestamp: float         # host clock (time.time())
    points: np.ndarray       # (N, 4) float32: [x, y, z, intensity]


class LidarCollector:
    """
    Subscribes to rt/utlidar/cloud and buffers point clouds in memory.

    Each scan is parsed to (N, 4) float32 [x, y, z, intensity] on arrival.
    If the DDS message has no 'intensity' field, the 4th column is zeros.
    """

    def __init__(
        self,
        network_interface: str = "eth0",
        topic: str = LIDAR_TOPIC_DEFAULT,
    ):
        self.network_interface = network_interface
        self.topic = topic

        self._lock = threading.Lock()
        self._running = False
        self._subscriber: Optional["ChannelSubscriber"] = None
        self._mock_thread: Optional[threading.Thread] = None

        self._scans: deque[_ParsedScan] = deque()
        self._frame_id: Optional[str] = None
        self._field_layout: Optional[list[dict]] = None
        self._parse_errors: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._clear_buffers()

        if not SDK_AVAILABLE:
            self._start_mock()
            return

        self._subscriber = ChannelSubscriber(self.topic, PointCloud2_)
        self._subscriber.Init(self._on_cloud, 5)
        print(f"[LidarCollector] Subscribed to {self.topic} on {self.network_interface}")

    def stop(self):
        self._running = False
        if self._mock_thread is not None:
            self._mock_thread.join(timeout=2.0)
            self._mock_thread = None
        print(
            f"[LidarCollector] Stopped. Buffered {len(self._scans)} scans "
            f"(parse errors: {self._parse_errors})."
        )

    def save(self, save_dir: Path):
        """
        Flush buffered scans to disk.

        Outputs (under save_dir):
            lidar/000000.npy, 000001.npy, ...   per-scan (N, 4) float32
            lidar_timestamps.npy                 (M,) float64 host timestamps
            lidar_meta.json                      topic, frame_id, field layout
        """
        save_dir = Path(save_dir)
        lidar_dir = save_dir / "lidar"
        lidar_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            scans_snapshot = list(self._scans)
            frame_id = self._frame_id
            fields = self._field_layout

        if not scans_snapshot:
            print("[LidarCollector] No scans to save.")
            return {"scans": 0}

        for i, scan in enumerate(scans_snapshot):
            np.save(lidar_dir / f"{i:06d}.npy", scan.points)

        timestamps = np.array([s.timestamp for s in scans_snapshot], dtype=np.float64)
        np.save(save_dir / "lidar_timestamps.npy", timestamps)

        meta = {
            "topic": self.topic,
            "frame_id": frame_id,
            "fields": fields,
            "num_scans": len(scans_snapshot),
            "per_scan_format": "(N, 4) float32: [x, y, z, intensity]",
            "parse_errors": self._parse_errors,
        }
        (save_dir / "lidar_meta.json").write_text(json.dumps(meta, indent=2))

        n = len(scans_snapshot)
        duration = round(float(timestamps[-1] - timestamps[0]), 2) if n > 1 else 0.0
        actual_hz = round(n / duration, 1) if duration > 0 else 0.0
        avg_points = int(np.mean([len(s.points) for s in scans_snapshot]))
        print(
            f"[LidarCollector] Saved {n} scans, {duration}s, ~{actual_hz} Hz, "
            f"avg {avg_points} pts/scan → {save_dir}"
        )
        return {
            "scans": n,
            "duration_s": duration,
            "hz": actual_hz,
            "avg_points_per_scan": avg_points,
        }

    @property
    def scan_count(self) -> int:
        return len(self._scans)

    # ------------------------------------------------------------------
    # DDS callback
    # ------------------------------------------------------------------

    def _on_cloud(self, msg: "PointCloud2_"):
        if not self._running:
            return
        ts = time.time()

        try:
            points = self._parse_cloud(msg)
        except Exception as e:
            self._parse_errors += 1
            if self._parse_errors == 1:
                print(f"[LidarCollector] Failed to parse PointCloud2: {e}")
            return

        with self._lock:
            if self._frame_id is None and msg.header is not None:
                self._frame_id = str(msg.header.frame_id)
            if self._field_layout is None:
                self._field_layout = [
                    {
                        "name":     str(f.name),
                        "offset":   int(f.offset),
                        "datatype": int(f.datatype),
                        "count":    int(f.count),
                    }
                    for f in msg.fields
                ]
            self._scans.append(_ParsedScan(timestamp=ts, points=points))

    # ------------------------------------------------------------------
    # PointCloud2 parser — generic, follows the message's fields layout
    # ------------------------------------------------------------------

    def _parse_cloud(self, msg: "PointCloud2_") -> np.ndarray:
        """
        Parse a PointCloud2 into (N, 4) float32 [x, y, z, intensity].
        Uses a numpy structured dtype built from msg.fields so any
        field layout with x/y/z works, including ones with extra fields
        (ring, time, etc.) we don't care about.
        """
        point_step = int(msg.point_step)
        num_points = int(msg.width) * int(msg.height)
        raw = bytes(msg.data)

        if point_step <= 0 or num_points == 0:
            return np.zeros((0, 4), dtype=np.float32)

        if len(raw) < point_step * num_points:
            raise ValueError(
                f"data len {len(raw)} < point_step*num_points = "
                f"{point_step}*{num_points}={point_step * num_points}"
            )

        names, formats, offsets = [], [], []
        for f in msg.fields:
            name = str(f.name)
            dtype = _PF_NUMPY.get(int(f.datatype))
            if dtype is None or int(f.count) != 1:
                continue        # skip unknown types / array-valued fields
            if name in names:
                continue        # guard against duplicates
            names.append(name)
            formats.append(dtype)
            offsets.append(int(f.offset))

        if not {"x", "y", "z"}.issubset(names):
            raise ValueError(f"PointCloud2 missing x/y/z fields; got {names}")

        struct_dtype = np.dtype(
            {"names": names, "formats": formats, "offsets": offsets,
             "itemsize": point_step}
        )
        arr = np.frombuffer(raw, dtype=struct_dtype, count=num_points)

        x = arr["x"].astype(np.float32, copy=False)
        y = arr["y"].astype(np.float32, copy=False)
        z = arr["z"].astype(np.float32, copy=False)
        if "intensity" in names:
            intensity = arr["intensity"].astype(np.float32, copy=False)
        else:
            intensity = np.zeros(num_points, dtype=np.float32)

        return np.stack([x, y, z, intensity], axis=-1)

    # ------------------------------------------------------------------
    # Mock mode (no SDK / no robot)
    # ------------------------------------------------------------------

    def _start_mock(self):
        print(f"[LidarCollector] Mock mode: synthetic scans at {NATIVE_SCAN_HZ} Hz")
        self._mock_thread = threading.Thread(target=self._mock_loop, daemon=True)
        self._mock_thread.start()

    def _mock_loop(self):
        interval = 1.0 / NATIVE_SCAN_HZ
        rng = np.random.default_rng(0)
        while self._running:
            t0 = time.monotonic()
            ts = time.time()
            n = 2000
            pts = np.empty((n, 4), dtype=np.float32)
            pts[:, 0] = rng.normal(0.0, 2.0, n)
            pts[:, 1] = rng.normal(0.0, 2.0, n)
            pts[:, 2] = rng.normal(0.0, 0.5, n)
            pts[:, 3] = rng.uniform(0.0, 100.0, n)
            with self._lock:
                self._scans.append(_ParsedScan(timestamp=ts, points=pts))
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))

    def _clear_buffers(self):
        with self._lock:
            self._scans.clear()
            self._frame_id = None
            self._field_layout = None
            self._parse_errors = 0
