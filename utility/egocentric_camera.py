"""
egocentric_camera.py — Collect Go2 EDU front camera frames.

Uses Go2VideoClient from unitree_sdk2_python to grab frames from the
robot's built-in front-facing camera, saves as MP4 + timestamps NPZ.

Usage:
    from utility.egocentric_camera import EgocentricCameraCollector

    collector = EgocentricCameraCollector(network_interface="eth0")
    collector.start()
    # ... robot is moving ...
    collector.stop()
    collector.save(Path("data/task1/session/robot"))
"""

import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.go2.video.video_client import VideoClient
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("[EgocentricCamera] WARNING: unitree_sdk2py not found — running in mock mode")

# Go2 front camera native resolution
CAMERA_WIDTH  = 1280
CAMERA_HEIGHT = 720
TARGET_FPS    = 30


class EgocentricCameraCollector:
    """
    Captures frames from the Go2 EDU's built-in front camera.

    Buffers frames in memory during recording, then saves:
        ego_rgb.mp4      — color video (H.264)
        ego_timestamps.npy — Unix timestamps per frame (float64)
    """

    def __init__(self, network_interface: str = "eth0",
                 width: int = CAMERA_WIDTH, height: int = CAMERA_HEIGHT,
                 fps: int = TARGET_FPS):
        self.network_interface = network_interface
        self.width  = width
        self.height = height
        self.fps    = fps

        self._lock      = threading.Lock()
        self._running   = False
        self._thread    = None
        self._client    = None

        # Frame buffer: list of (timestamp, frame_bgr)
        self._frames: deque[tuple[float, np.ndarray]] = deque()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self):
        """Initialize video client and begin capturing frames."""
        if self._running:
            return

        self._running = True
        self._frames.clear()

        if not SDK_AVAILABLE:
            self._start_mock()
            return

        self._client = VideoClient()
        self._client.SetTimeout(3.0)
        self._client.Init()

        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(f"[EgocentricCamera] Started capture on {self.network_interface}")

    def stop(self):
        """Stop capturing. Frames remain in memory until save() is called."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        print(f"[EgocentricCamera] Stopped. Buffered {len(self._frames)} frames.")

    def save(self, save_dir: Path):
        """
        Flush buffered frames to disk.

        Outputs:
            ego_rgb.mp4          — H.264 video
            ego_timestamps.npy   — shape (N,) float64 Unix timestamps
        """
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            frames_snapshot = list(self._frames)

        if not frames_snapshot:
            print("[EgocentricCamera] No frames to save.")
            return {"frames": 0}

        timestamps = np.array([f[0] for f in frames_snapshot], dtype=np.float64)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(save_dir / "ego_rgb.mp4"),
            fourcc, self.fps, (self.width, self.height)
        )
        for _, frame in frames_snapshot:
            writer.write(frame)
        writer.release()

        np.save(str(save_dir / "ego_timestamps.npy"), timestamps)

        n = len(frames_snapshot)
        duration = round(float(timestamps[-1] - timestamps[0]), 2) if n > 1 else 0.0
        actual_fps = round(n / duration, 1) if duration > 0 else 0.0
        print(f"[EgocentricCamera] Saved {n} frames, {duration}s, ~{actual_fps} fps → {save_dir}")
        return {"frames": n, "duration_s": duration, "fps": actual_fps}

    def get_latest_frame(self) -> np.ndarray | None:
        """Return the most recent frame (BGR) for live preview, or None."""
        with self._lock:
            if not self._frames:
                return None
            return self._frames[-1][1].copy()

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self):
        interval = 1.0 / self.fps
        while self._running:
            t0 = time.monotonic()
            try:
                # GetImageSample returns raw JPEG bytes from the Go2 camera
                code, data = self._client.GetImageSample()
                if code == 0 and data:
                    ts = time.time()
                    buf = np.frombuffer(bytes(data), dtype=np.uint8)
                    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                    if frame is not None:
                        # Resize if camera returns a different resolution
                        if frame.shape[1] != self.width or frame.shape[0] != self.height:
                            frame = cv2.resize(frame, (self.width, self.height))
                        with self._lock:
                            self._frames.append((ts, frame))
            except Exception as e:
                print(f"[EgocentricCamera] Frame error: {e}")

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    # ------------------------------------------------------------------
    # Mock mode
    # ------------------------------------------------------------------

    def _start_mock(self):
        print(f"[EgocentricCamera] Mock mode: generating synthetic frames at {self.fps} Hz")
        self._thread = threading.Thread(target=self._mock_loop, daemon=True)
        self._thread.start()

    def _mock_loop(self):
        interval = 1.0 / self.fps
        frame_idx = 0
        while self._running:
            t0 = time.monotonic()
            ts = time.time()

            frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(frame, f"MOCK ego cam #{frame_idx}", (20, self.height // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2)
            frame_idx += 1

            with self._lock:
                self._frames.append((ts, frame))

            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
