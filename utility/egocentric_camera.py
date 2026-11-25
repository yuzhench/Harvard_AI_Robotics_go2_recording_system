"""
egocentric_camera.py — Collect RGB + Depth from the on-robot RealSense
D435I mounted on the Go2.

Hardware: single Intel RealSense D435I plugged into the Jetson via USB 3.

Frames are written to disk while recording:

    <save_dir>/ego_cam/
        intrinsics.json   color + depth intrinsics, depth_scale, serial, name
        rgb.mjpeg         JPEG frames, concatenated
        depth.raw         uint16 depth frames (H*W*2 bytes each), concatenated
        frames.bin        one FRAME_DTYPE record per frame

RGB and depth frames are 1:1 aligned (same `align_to=color` + same host
timestamp); record i of frames.bin describes frame i of both files.
scripts/finalize_session.py converts this offline into the layout shared
with the third-person cameras of camera_record_pipeline (rgb.mp4,
rgb_timestamps.npy, depth.npz).
"""

import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .streaming import AppendFile, PauseGate

try:
    import pyrealsense2 as rs
    REALSENSE_AVAILABLE = True
except ImportError:
    REALSENSE_AVAILABLE = False
    print("[EgoRealSense] WARNING: pyrealsense2 not installed — real recording unavailable")


WIDTH_DEFAULT = 640
HEIGHT_DEFAULT = 480
FPS_DEFAULT = 30

CAM_DIRNAME = "ego_cam"
RGB_FILENAME = "rgb.mjpeg"
DEPTH_FILENAME = "depth.raw"
FRAMES_FILENAME = "frames.bin"

# One record per frame. rgb_offset/rgb_size locate the frame's JPEG inside
# rgb.mjpeg; the depth frame is at index * H * W * 2 in depth.raw.
FRAME_DTYPE = np.dtype([
    ("timestamp",  "<f8"),
    ("rgb_offset", "<u8"),
    ("rgb_size",   "<u4"),
])

JPEG_QUALITY = 95


class EgocentricCameraCollector:
    """
    Single-camera RGB+Depth collector for the on-robot RealSense.

    API parity with other utility/ collectors:
        start(p)  — begin capture, streaming frames to <p>/ego_cam/
        stop()    — stop capture and close the files
        frame_count   — frames written so far
    """

    # Sliding-window size for live FPS reporting (seconds of history kept).
    _FPS_WINDOW_S = 1.0

    # Frames waiting for the disk writer. If the disk cannot keep up the
    # queue fills and further frames are dropped (counted) rather than
    # letting memory grow without bound.
    _QUEUE_FRAMES = 90

    def __init__(
        self,
        network_interface: str = "eth0",     # unused; kept for API parity
        width: int = WIDTH_DEFAULT,
        height: int = HEIGHT_DEFAULT,
        fps: int = FPS_DEFAULT,
        serial: Optional[str] = None,        # if None, uses first detected device
        mock: bool = False,
    ):
        self.network_interface = network_interface
        self.mock = mock
        self._error = None
        self.width = width
        self.height = height
        self.fps = fps
        self.serial = serial

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._QUEUE_FRAMES)
        self._pipeline = None
        self._profile = None

        self._rgb_file: Optional[AppendFile] = None
        self._depth_file: Optional[AppendFile] = None
        self._frames_file: Optional[AppendFile] = None

        self._intrinsics: dict = {}
        self._frame_count = 0
        self._dropped = 0
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None
        # Sliding-window frame timestamps for live fps reporting.
        self._fps_window: deque = deque()
        self._record_from = 0.0
        self._gate = PauseGate()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self, save_dir: Path, record_from: float = 0.0):
        try:
            self._start(save_dir, record_from)
        except Exception:
            self.stop()
            raise

    def _start(self, save_dir: Path, record_from: float = 0.0):
        """Frames captured before `record_from` (Unix time) are discarded."""
        if self._running:
            return
        if not self.mock and not REALSENSE_AVAILABLE:
            raise RuntimeError("EgoRealSense: required driver is unavailable; mock mode must be explicit")
        self._error = None
        self._record_from = record_from
        self._gate.reset()
        cam_dir = Path(save_dir) / CAM_DIRNAME
        cam_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._frame_count = 0
            self._dropped = 0
            self._first_ts = None
            self._last_ts = None
            self._fps_window.clear()
        self._queue = queue.Queue(maxsize=self._QUEUE_FRAMES)

        if not self.mock:
            self._pipeline = rs.pipeline()
            config = rs.config()
            if self.serial:
                config.enable_device(self.serial)
            config.enable_stream(
                rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps
            )
            config.enable_stream(
                rs.stream.depth, self.width, self.height, rs.format.z16, self.fps
            )
            self._profile = self._pipeline.start(config)
            # Intrinsics don't change during streaming.
            self._intrinsics = self._read_intrinsics(self._profile)
            capture = self._capture_loop
        else:
            print(f"[EgoRealSense] Mock mode: synthetic frames at {self.fps} Hz")
            self._intrinsics = self._mock_intrinsics()
            capture = self._mock_loop

        self._intrinsics["depth_native"] = dict(self._intrinsics["depth"])
        self._intrinsics["depth"] = dict(
            self._intrinsics["color"], depth_scale=self._intrinsics["depth_native"]["depth_scale"])
        self._intrinsics["depth_aligned_to"] = "color"
        self._intrinsics["mock"] = self.mock
        (cam_dir / "intrinsics.json").write_text(json.dumps(self._intrinsics, indent=2))
        self._rgb_file = AppendFile(cam_dir / RGB_FILENAME)
        self._depth_file = AppendFile(cam_dir / DEPTH_FILENAME)
        self._frames_file = AppendFile(cam_dir / FRAMES_FILENAME)

        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_guard, daemon=True)
        self._writer_thread.start()
        self._thread = threading.Thread(target=self._capture_guard, args=(capture,), daemon=True)
        self._thread.start()
        print(
            f"[EgoRealSense] Started {self._intrinsics['name']} "
            f"(serial {self._intrinsics['serial']}) "
            f"{self.width}x{self.height} @ {self.fps} fps"
        )

    def stop(self) -> dict:
        self._running = False
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        # The writer drains whatever is still queued before exiting.
        if self._writer_thread is not None:
            self._writer_thread.join()
            self._writer_thread = None
        for f in (self._rgb_file, self._depth_file, self._frames_file):
            if f is not None:
                try:
                    f.close()
                except Exception as exc:
                    self._error = self._error or exc
        self._rgb_file = self._depth_file = self._frames_file = None

        if self._error is not None:
            raise RuntimeError(f"Camera writer failed: {self._error}") from self._error
        n = self._frame_count
        duration = (
            float(self._last_ts - self._first_ts - self._gate.gap_within(self._first_ts, self._last_ts))
            if n > 1 and self._first_ts is not None else 0.0
        )
        actual_fps = ((n - 1) / duration) if duration > 0 else 0.0
        print(
            f"[EgoRealSense] Stopped. {n} frames, {duration:.2f}s, "
            f"~{actual_fps:.1f} fps, {self._dropped} dropped"
        )
        return {
            "frames":         n,
            "duration_s":     round(duration, 2),
            "actual_fps":     round(actual_fps, 2),
            "target_fps":     self.fps,
            "dropped_frames": self._dropped,
        }

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def dropped_frames(self) -> int:
        return self._dropped

    @property
    def recent_fps(self) -> float:
        """Frames captured in the last _FPS_WINDOW_S seconds (sliding)."""
        if not self._running:
            return 0.0
        now = time.time()
        cutoff = now - self._FPS_WINDOW_S
        with self._lock:
            while self._fps_window and self._fps_window[0] < cutoff:
                self._fps_window.popleft()
            count = len(self._fps_window)
        return float(count) / self._FPS_WINDOW_S

    def pause(self):
        """Drop data until resume(); files stay open."""
        self._gate.pause(time.time())

    def resume(self):
        self._gate.resume(time.time())

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_guard(self, capture):
        try:
            capture()
        except Exception as exc:
            self._error = exc
            print(f"[EgoRealSense] Capture failed: {exc}")

    def _capture_loop(self):
        align = rs.align(rs.stream.color)
        while self._running:
            try:
                frames = self._pipeline.wait_for_frames(timeout_ms=1000)
            except Exception as e:
                # Jetson occasionally hiccups on USB 3 isoc — keep going.
                if self._running:
                    print(f"[EgoRealSense] wait_for_frames timeout: {e}")
                continue

            if not frames.get_color_frame() or not frames.get_depth_frame():
                continue

            aligned = align.process(frames)
            color_f = aligned.get_color_frame()
            depth_f = aligned.get_depth_frame()
            if not color_f or not depth_f:
                continue

            ts = time.time()
            # Must .copy() — the underlying RealSense buffers get recycled.
            color_np = np.asanyarray(color_f.get_data()).copy()
            depth_np = np.asanyarray(depth_f.get_data()).copy()
            self._enqueue(ts, color_np, depth_np)

    def _enqueue(self, ts: float, color: np.ndarray, depth: np.ndarray):
        if not self._running or self._error is not None:
            return
        with self._lock:
            # Update sliding-window live fps.
            cutoff = ts - self._FPS_WINDOW_S
            while self._fps_window and self._fps_window[0] < cutoff:
                self._fps_window.popleft()
            self._fps_window.append(ts)
        if ts < self._record_from or self._gate.paused:
            return
        try:
            self._queue.put_nowait((ts, color, depth))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                print("[EgoRealSense] Disk writer is behind — dropping frames.")

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
        while self._running or not self._queue.empty():
            try:
                ts, color, depth = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            ok, jpeg = cv2.imencode(
                ".jpg", color, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                self._dropped += 1
                continue
            jpeg = jpeg.tobytes()

            # Append the index after the payloads. Separate fsync calls do not
            # guarantee crash ordering; conversion validates each payload.
            offset = self._rgb_file.bytes_written
            self._rgb_file.write(jpeg)
            self._depth_file.write(np.ascontiguousarray(depth, dtype="<u2").tobytes())
            record = np.array([(ts, offset, len(jpeg))], dtype=FRAME_DTYPE)
            self._frames_file.write(record.tobytes())

            self._frame_count += 1
            if self._first_ts is None:
                self._first_ts = ts
            self._last_ts = ts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_intrinsics(profile) -> dict:
        color_stream = profile.get_stream(rs.stream.color).as_video_stream_profile()
        depth_stream = profile.get_stream(rs.stream.depth).as_video_stream_profile()
        ci = color_stream.get_intrinsics()
        di = depth_stream.get_intrinsics()
        depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()
        device = profile.get_device()
        return {
            "color": {
                "width": ci.width, "height": ci.height,
                "fx": ci.fx, "fy": ci.fy,
                "ppx": ci.ppx, "ppy": ci.ppy,
                "distortion_model": str(ci.model),
                "coeffs": list(ci.coeffs),
            },
            "depth": {
                "width": di.width, "height": di.height,
                "fx": di.fx, "fy": di.fy,
                "ppx": di.ppx, "ppy": di.ppy,
                "distortion_model": str(di.model),
                "coeffs": list(di.coeffs),
                "depth_scale": depth_scale,
            },
            "serial": device.get_info(rs.camera_info.serial_number),
            "name":   device.get_info(rs.camera_info.name),
        }

    # ------------------------------------------------------------------
    # Mock mode (no camera / running on laptop for dev)
    # ------------------------------------------------------------------

    def _mock_intrinsics(self) -> dict:
        intr = {
            "width": self.width, "height": self.height,
            "fx": 600.0, "fy": 600.0,
            "ppx": self.width / 2, "ppy": self.height / 2,
            "distortion_model": "Brown Conrady",
            "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
        }
        return {
            "color": dict(intr),
            "depth": dict(intr, depth_scale=0.001),
            "serial": "MOCK-EGO-0000",
            "name":   "Mock Ego RealSense",
        }

    def _mock_loop(self):
        interval = 1.0 / self.fps
        idx = 0
        while self._running:
            t0 = time.monotonic()
            ts = time.time()

            color = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(
                color, f"MOCK ego #{idx}", (20, self.height // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2,
            )
            # Gradient depth — 0.5m to 3m left→right
            depth = np.tile(
                np.linspace(500, 3000, self.width, dtype=np.uint16),
                (self.height, 1),
            )
            self._enqueue(ts, color, depth)
            idx += 1

            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
