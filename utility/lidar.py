"""
lidar.py — Collect 4D LiDAR L1 point clouds from Go2 EDU.

Subscribes to rt/utlidar/cloud (sensor_msgs::PointCloud2) over DDS and
writes each scan to disk as it arrives:

    <save_dir>/lidar/000000.npy, 000001.npy, ...   per-scan (N, 4) float32
    <save_dir>/lidar_timestamps.bin                float64 host timestamps
    <save_dir>/lidar_meta.json                     topic, frame_id, field layout

scripts/finalize_session.py turns lidar_timestamps.bin into
lidar_timestamps.npy offline.

DDS message rate depends on the active LiDAR and publishing configuration.
The collector does not rate-limit; its mock stream generates 11 scans/s.

Usage:
    from utility.lidar import LidarCollector

    collector = LidarCollector(network_interface="eth0")
    collector.start(Path("data/task1/session/first_person"))
    # ... robot is moving ...
    collector.stop()
"""

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .streaming import AppendFile, PauseGate, RateMeter

try:
    from unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("[LidarCollector] WARNING: unitree_sdk2py not found — real recording unavailable")


# Synthetic scan rate only; not a limit on the live DDS topic.
NATIVE_SCAN_HZ = 11
LIDAR_TOPIC_DEFAULT = "rt/utlidar/cloud"

SCAN_DIRNAME = "lidar"
TIMESTAMPS_RAW_FILENAME = "lidar_timestamps.bin"
META_FILENAME = "lidar_meta.json"
PER_SCAN_FORMAT = "(N, 4) float32: [x, y, z, intensity]"


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


class LidarCollector:
    """
    Subscribes to rt/utlidar/cloud and streams point clouds to disk.

    Each scan is parsed to (N, 4) float32 [x, y, z, intensity] on arrival.
    If the DDS message has no 'intensity' field, the 4th column is zeros.
    """

    # Scans waiting for the disk writer (~10 s at the native rate).
    _QUEUE_SCANS = 128

    def __init__(
        self,
        network_interface: str = "eth0",
        topic: str = LIDAR_TOPIC_DEFAULT,
        mock: bool = False,
    ):
        self.network_interface = network_interface
        self.mock = mock
        self._error = None
        self.topic = topic

        self._lock = threading.Lock()
        self._running = False
        self._subscriber: Optional["ChannelSubscriber"] = None
        self._mock_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._QUEUE_SCANS)

        self._save_dir: Optional[Path] = None
        self._ts_file: Optional[AppendFile] = None
        self._frame_id: Optional[str] = None
        self._field_layout: Optional[list] = None
        self._meta_written = False
        self._parse_errors: int = 0
        self._dropped: int = 0
        self._scan_count: int = 0
        self._points_total: int = 0
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._rate = RateMeter()
        self._record_from = 0.0
        self._gate = PauseGate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, save_dir: Path, record_from: float = 0.0):
        """Scans that arrive before `record_from` (Unix time) are discarded."""
        if self._running:
            return
        if not self.mock and not SDK_AVAILABLE:
            raise RuntimeError("LidarCollector: required driver is unavailable; mock mode must be explicit")
        self._error = None
        self._record_from = record_from
        self._gate.reset()
        self._save_dir = Path(save_dir)
        (self._save_dir / SCAN_DIRNAME).mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._frame_id = None
            self._field_layout = None
        self._meta_written = False
        self._parse_errors = 0
        self._dropped = 0
        self._scan_count = 0
        self._points_total = 0
        self._first_ts = None
        self._last_ts = None
        self._rate.reset()
        self._queue = queue.Queue(maxsize=self._QUEUE_SCANS)
        self._ts_file = AppendFile(self._save_dir / TIMESTAMPS_RAW_FILENAME)

        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_guard, daemon=True)
        self._writer_thread.start()

        if self.mock:
            self._start_mock()
            return

        # Subscribe once and keep it for the life of the process (the callback
        # ignores messages while not recording). Subscribing again on every
        # start() would deliver each scan once per subscriber.
        if self._subscriber is None:
            subscriber = ChannelSubscriber(self.topic, PointCloud2_)
            subscriber.Init(self._on_cloud, 5)
            self._subscriber = subscriber
            print(f"[LidarCollector] Subscribed to {self.topic} on {self.network_interface}")

    def stop(self) -> dict:
        if not self._running:
            return {"scans": 0}
        with self._lock:
            self._running = False
        if self._mock_thread is not None:
            self._mock_thread.join()
            self._mock_thread = None
        # The writer drains whatever is still queued before exiting.
        if self._writer_thread is not None:
            self._writer_thread.join()
            self._writer_thread = None
        if self._ts_file is not None:
            try:
                self._ts_file.close()
            except Exception as exc:
                self._error = self._error or exc
            self._ts_file = None
        self._write_meta()

        if self._error is not None:
            raise RuntimeError(f"Disk writer failed: {self._error}") from self._error
        n = self._scan_count
        duration = (
            round(self._last_ts - self._first_ts - self._gate.gap_within(self._first_ts, self._last_ts), 2)
            if n > 1 and self._first_ts is not None else 0.0
        )
        actual_hz = round((n - 1) / duration, 1) if duration > 0 else 0.0
        avg_points = int(self._points_total / n) if n else 0
        print(
            f"[LidarCollector] Stopped. {n} scans, {duration}s, ~{actual_hz} Hz, "
            f"avg {avg_points} pts/scan (parse errors: {self._parse_errors}, "
            f"dropped: {self._dropped})"
        )
        return {
            "scans": n,
            "duration_s": duration,
            "hz": actual_hz,
            "avg_points_per_scan": avg_points,
            "dropped_scans": self._dropped,
        }

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @property
    def recent_hz(self) -> float:
        """Scans received in the last second. Zero when not recording."""
        return self._rate.hz() if self._running else 0.0

    def pause(self):
        """Drop data until resume(); files stay open."""
        self._gate.pause(time.time())

    def resume(self):
        self._gate.resume(time.time())

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
        self._enqueue(ts, points)

    def _enqueue(self, ts: float, points: np.ndarray):
        self._rate.tick(ts)
        if ts < self._record_from or self._gate.paused:
            return
        with self._lock:
            if not self._running or self._error is not None:
                return
            self._put_scan(ts, points)

    def _put_scan(self, ts, points):
        try:
            self._queue.put_nowait((ts, points))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                print("[LidarCollector] Disk writer is behind — dropping scans.")

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
        scan_dir = self._save_dir / SCAN_DIRNAME
        while self._running or not self._queue.empty():
            try:
                ts, points = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            # Scan file first, timestamp second: a timestamp on disk means
            # its scan file was written before it.
            with open(scan_dir / f"{self._scan_count:06d}.npy", "wb") as f:
                np.save(f, points)
                f.flush()
                os.fsync(f.fileno())
            self._ts_file.write(np.float64(ts).tobytes())

            if not self._meta_written:
                self._write_meta()
                self._meta_written = True
            self._scan_count += 1
            self._points_total += len(points)
            if self._first_ts is None:
                self._first_ts = ts
            self._last_ts = ts

    def _write_meta(self):
        with self._lock:
            frame_id = self._frame_id
            fields = self._field_layout
        meta = {
            "topic": self.topic,
            "frame_id": frame_id,
            "fields": fields,
            "num_scans": self._scan_count,
            "per_scan_format": PER_SCAN_FORMAT,
            "parse_errors": self._parse_errors,
        }
        (self._save_dir / META_FILENAME).write_text(json.dumps(meta, indent=2))

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
            formats.append(np.dtype(dtype).newbyteorder(">" if msg.is_bigendian else "<"))
            offsets.append(int(f.offset))

        if not {"x", "y", "z"}.issubset(names):
            raise ValueError(f"PointCloud2 missing x/y/z fields; got {names}")

        struct_dtype = np.dtype(
            {"names": names, "formats": formats, "offsets": offsets,
             "itemsize": point_step}
        )
        width, height = int(msg.width), int(msg.height)
        row_step = int(msg.row_step)
        if row_step < width * point_step or len(raw) < row_step * height:
            raise ValueError("Invalid PointCloud2 row_step or truncated data")
        arr = np.ndarray((height, width), dtype=struct_dtype, buffer=raw,
                         strides=(row_step, point_step)).reshape(-1)

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
            self._enqueue(ts, pts)
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
