"""
egocentric_camera.py — Collect RGB + Depth from the on-robot RealSense
D435I mounted on the Go2.

Hardware: single Intel RealSense D435I plugged into the Jetson via USB 3.
Output format mirrors the 4× third-person cameras handled by
camera_record_pipeline so downstream alignment code can treat them
identically:

    <save_dir>/ego_cam/
        rgb.mp4              H.264 color video at configured fps
        rgb_timestamps.npy   (F,)  float64  per-frame Unix timestamps
        depth.npz            {"depth": (F, H, W) uint16, "timestamps": (F,) float64}
        intrinsics.json      color + depth intrinsics, depth_scale, serial, name

RGB and depth frames are 1:1 aligned (same `align_to=color` + same host
timestamp) and share the timestamp array — loading rgb_timestamps.npy OR
depth.npz["timestamps"] gives the identical per-frame times.
"""

import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import pyrealsense2 as rs
    REALSENSE_AVAILABLE = True
except ImportError:
    REALSENSE_AVAILABLE = False
    print("[EgoRealSense] WARNING: pyrealsense2 not installed — running in mock mode")


WIDTH_DEFAULT = 640
HEIGHT_DEFAULT = 480
FPS_DEFAULT = 30


class EgocentricCameraCollector:
    """
    Single-camera RGB+Depth collector for the on-robot RealSense.

    API parity with other utility/ collectors:
        start()   — begin capture in a background thread
        stop()    — stop capture; frames remain buffered in memory
        save(p)   — flush buffered frames to <p>/ego_cam/
        frame_count   — property for current buffered frame count
    """

    # Remux MP4 with ffmpeg when actual fps diverges from declared by more
    # than this fraction (e.g. 30 vs 27 → mild; 30 vs 20 → remux kicks in).
    FPS_FIX_THRESHOLD = 0.05

    # Sliding-window size for live FPS reporting (seconds of history kept).
    _FPS_WINDOW_S = 1.0

    def __init__(
        self,
        network_interface: str = "eth0",     # unused; kept for API parity
        width: int = WIDTH_DEFAULT,
        height: int = HEIGHT_DEFAULT,
        fps: int = FPS_DEFAULT,
        serial: Optional[str] = None,        # if None, uses first detected device
    ):
        self.network_interface = network_interface
        self.width = width
        self.height = height
        self.fps = fps
        self.serial = serial

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pipeline = None
        self._profile = None

        # Buffers — cleared on each start()
        self._color_frames: list[np.ndarray] = []
        self._depth_frames: list[np.ndarray] = []
        self._timestamps:  list[float] = []
        self._intrinsics:  dict = {}
        # Sliding-window frame timestamps for live fps reporting.
        self._fps_window:  deque[float] = deque()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._clear_buffers()

        if not REALSENSE_AVAILABLE:
            self._start_mock()
            return

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

        # Cache intrinsics once — they don't change during streaming.
        self._intrinsics = self._read_intrinsics(self._profile)

        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(
            f"[EgoRealSense] Started {self._intrinsics['name']} "
            f"(serial {self._intrinsics['serial']}) "
            f"{self.width}x{self.height} @ {self.fps} fps"
        )

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        print(f"[EgoRealSense] Stopped. Buffered {len(self._timestamps)} frames.")

    def save(self, save_dir: Path):
        """
        Flush frames to <save_dir>/ego_cam/.

        Returns a summary dict suitable for the daemon's stop() response.
        """
        save_dir = Path(save_dir)
        cam_dir = save_dir / "ego_cam"
        cam_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            color_snap = list(self._color_frames)
            depth_snap = list(self._depth_frames)
            ts_snap    = list(self._timestamps)
            intr_snap  = dict(self._intrinsics)

        if not color_snap:
            print("[EgoRealSense] No frames to save.")
            return {"frames": 0}

        n = len(color_snap)
        timestamps = np.array(ts_snap, dtype=np.float64)
        duration = float(timestamps[-1] - timestamps[0]) if n > 1 else 0.0
        actual_fps = (n / duration) if duration > 0 else float(self.fps)

        # intrinsics.json
        (cam_dir / "intrinsics.json").write_text(json.dumps(intr_snap, indent=2))

        # rgb.mp4 (declared fps initially; remuxed below if actual diverges)
        mp4_path = cam_dir / "rgb.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(mp4_path), fourcc, float(self.fps), (self.width, self.height)
        )
        for frame in color_snap:
            writer.write(frame)
        writer.release()

        # rgb_timestamps.npy (per-frame host timestamps, shared with depth)
        np.save(cam_dir / "rgb_timestamps.npy", timestamps)

        # depth.npz — color-aligned uint16 depth frames + same timestamps
        depth_array = np.stack(depth_snap, axis=0)   # (F, H, W) uint16
        np.savez_compressed(
            cam_dir / "depth.npz",
            depth=depth_array,
            timestamps=timestamps,
        )

        # Optional: fix MP4 fps metadata if actual differs from declared.
        mp4_fps_fixed = False
        if (
            self.fps > 0
            and n > 1
            and abs(actual_fps - self.fps) / self.fps > self.FPS_FIX_THRESHOLD
        ):
            mp4_fps_fixed = self._remux_mp4_fps(mp4_path, actual_fps)

        info = {
            "frames":         n,
            "duration_s":     round(duration, 2),
            "actual_fps":     round(actual_fps, 2),
            "target_fps":     self.fps,
            "mp4_fps_fixed":  mp4_fps_fixed,
        }
        print(
            f"[EgoRealSense] Saved {n} frames, {duration:.2f}s, "
            f"~{actual_fps:.1f} fps → {cam_dir}"
        )
        return info

    @property
    def frame_count(self) -> int:
        return len(self._timestamps)

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

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

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

            with self._lock:
                self._color_frames.append(color_np)
                self._depth_frames.append(depth_np)
                self._timestamps.append(ts)
                # Update sliding-window live fps.
                cutoff = ts - self._FPS_WINDOW_S
                while self._fps_window and self._fps_window[0] < cutoff:
                    self._fps_window.popleft()
                self._fps_window.append(ts)

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

    @staticmethod
    def _remux_mp4_fps(mp4_path: Path, fps: float) -> bool:
        """Rewrite MP4 frame-rate metadata with ffmpeg (-c copy, no re-encode)."""
        tmp = mp4_path.with_suffix(".fps_fix.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y",
                 "-r", f"{fps:.4f}",
                 "-i", str(mp4_path),
                 "-c", "copy",
                 str(tmp)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tmp.replace(mp4_path)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[EgoRealSense] ffmpeg remux failed ({e}); keeping original rgb.mp4.")
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            return False

    def _clear_buffers(self):
        with self._lock:
            self._color_frames = []
            self._depth_frames = []
            self._timestamps = []
            self._intrinsics = {}
            self._fps_window.clear()

    # ------------------------------------------------------------------
    # Mock mode (no camera / running on laptop for dev)
    # ------------------------------------------------------------------

    def _start_mock(self):
        print(f"[EgoRealSense] Mock mode: synthetic frames at {self.fps} Hz")
        # Pre-fill mock intrinsics so save() produces a valid intrinsics.json.
        self._intrinsics = {
            "color": {
                "width": self.width, "height": self.height,
                "fx": 600.0, "fy": 600.0,
                "ppx": self.width / 2, "ppy": self.height / 2,
                "distortion_model": "Brown Conrady",
                "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
            },
            "depth": {
                "width": self.width, "height": self.height,
                "fx": 600.0, "fy": 600.0,
                "ppx": self.width / 2, "ppy": self.height / 2,
                "distortion_model": "Brown Conrady",
                "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
                "depth_scale": 0.001,
            },
            "serial": "MOCK-EGO-0000",
            "name":   "Mock Ego RealSense",
        }
        self._thread = threading.Thread(target=self._mock_loop, daemon=True)
        self._thread.start()

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

            with self._lock:
                self._color_frames.append(color)
                self._depth_frames.append(depth)
                self._timestamps.append(ts)
                cutoff = ts - self._FPS_WINDOW_S
                while self._fps_window and self._fps_window[0] < cutoff:
                    self._fps_window.popleft()
                self._fps_window.append(ts)
            idx += 1

            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
