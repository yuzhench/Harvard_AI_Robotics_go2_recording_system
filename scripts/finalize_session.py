"""
finalize_session.py — Convert raw recorded sessions into the final layout.

While recording, the Jetson only appends to raw stream files so that nothing
whole session is held in memory. Crash recovery depends on which complete
records reached storage. This script
runs afterwards (on the Jetson or on a laptop after rsync) and produces the
layout that downstream code and camera_record_pipeline expect:

    raw (written while recording)          final (written here)
    -------------------------------------  ---------------------------------
    lowstate.bin                           imu.npz  joints.npz  contacts.npz
    ego_cam/rgb.mjpeg + frames.bin         ego_cam/rgb.mp4
                                           ego_cam/rgb_timestamps.npy
    ego_cam/depth.raw + frames.bin         ego_cam/depth.npz
    lidar_timestamps.bin                   lidar_timestamps.npy
    lidar/*.npy, lidar_meta.json,          (already final)
    ego_cam/intrinsics.json

A session that was cut short (power loss, crash) is finalized the same way:
each stream is truncated to its last complete sample.

Usage:
    python scripts/finalize_session.py <path>                # one session or a whole tree
    python scripts/finalize_session.py <path> --delete-raw   # drop raw files once converted
"""

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utility.egocentric_camera import (  # noqa: E402
    CAM_DIRNAME, DEPTH_FILENAME, FRAME_DTYPE, FRAMES_FILENAME, RGB_FILENAME,
)
from utility.imu import (  # noqa: E402
    FOOT_NAMES, JOINT_NAMES, LOWSTATE_DTYPE, RAW_FILENAME as LOWSTATE_FILENAME,
)
from utility.lidar import (  # noqa: E402
    META_FILENAME, SCAN_DIRNAME, TIMESTAMPS_RAW_FILENAME,
)

# Depth frames compressed per chunk so a long session never has to fit in RAM.
DEPTH_CHUNK_FRAMES = 64


def _read_records(path: Path, dtype: np.dtype) -> np.ndarray:
    """Load a flat record file, ignoring a partially written last record."""
    n = path.stat().st_size // dtype.itemsize
    return np.memmap(path, dtype=dtype, mode="r", shape=(n,)) if n else np.empty(0, dtype=dtype)


def finalize_lowstate(session: Path, output: Path = None) -> list:
    output = session if output is None else output
    raw = session / LOWSTATE_FILENAME
    if not raw.exists():
        return []
    rec = _read_records(raw, LOWSTATE_DTYPE)
    ts = rec["timestamp"]

    np.savez_compressed(
        output / "imu.npz",
        timestamps=ts,
        quaternion=rec["quaternion"],
        gyroscope=rec["gyroscope"],
        accelerometer=rec["accelerometer"],
        rpy=rec["rpy"],
    )
    np.savez_compressed(
        output / "joints.npz",
        timestamps=ts,
        q=rec["q"],
        dq=rec["dq"],
        tau=rec["tau"],
        joint_names=np.array(JOINT_NAMES),
    )
    np.savez_compressed(
        output / "contacts.npz",
        timestamps=ts,
        foot_force=rec["foot_force"],
        foot_names=np.array(FOOT_NAMES),
    )
    print(f"  lowstate : {len(rec)} samples -> imu.npz, joints.npz, contacts.npz")
    return [raw]


def _write_depth_npz(path: Path, depth_raw: Path, n: int, h: int, w: int,
                     timestamps: np.ndarray):
    """Write {"depth": (n,h,w) uint16, "timestamps": (n,) float64} as a
    standard .npz, streaming the depth array through the compressor."""
    frame_bytes = h * w * 2
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        with zf.open("depth.npy", "w", force_zip64=True) as out:
            np.lib.format.write_array_header_1_0(out, {
                "descr": "<u2", "fortran_order": False, "shape": (n, h, w),
            })
            with open(depth_raw, "rb") as src:
                remaining = n
                while remaining > 0:
                    k = min(remaining, DEPTH_CHUNK_FRAMES)
                    data = src.read(k * frame_bytes)
                    if len(data) != k * frame_bytes:
                        raise ValueError("Depth stream was truncated during conversion")
                    out.write(data)
                    remaining -= k
        with zf.open("timestamps.npy", "w") as out:
            np.lib.format.write_array(out, np.ascontiguousarray(timestamps))


def finalize_ego_cam(session: Path, output: Path = None) -> list:
    cam = session / CAM_DIRNAME
    output = cam if output is None else output / CAM_DIRNAME
    output.mkdir(parents=True, exist_ok=True)
    frames_path = cam / FRAMES_FILENAME
    if not frames_path.exists():
        return []
    rgb_raw = cam / RGB_FILENAME
    depth_raw = cam / DEPTH_FILENAME

    intr = json.loads((cam / "intrinsics.json").read_text())
    w, h = intr["color"]["width"], intr["color"]["height"]
    frame_bytes = h * w * 2

    frames = _read_records(frames_path, FRAME_DTYPE)
    # Keep only frames whose RGB and depth data are completely on disk.
    rgb_size = rgb_raw.stat().st_size
    rgb_complete = ((frames["rgb_offset"] <= rgb_size)
                    & (frames["rgb_size"] > 0)
                    & (frames["rgb_size"] <= rgb_size - np.minimum(frames["rgb_offset"], rgb_size)))
    invalid = np.flatnonzero(~rgb_complete)
    prefix = int(invalid[0]) if len(invalid) else len(frames)
    n = min(prefix, depth_raw.stat().st_size // frame_bytes)
    frames = frames[:n]
    ts = frames["timestamp"].astype(np.float64)
    if n == 0:
        print("  ego_cam  : no complete frames")
        return []

    duration = float(ts[-1] - ts[0]) if n > 1 else 0.0
    if not np.isfinite(ts).all() or (n > 1 and np.any(np.diff(ts) <= 0)):
        raise ValueError("Camera timestamps are not finite and strictly increasing")
    # Median frame interval, not (n - 1) / duration: a paused recording has
    # gaps, and the video should play at the camera's rate with the pauses cut.
    fps = 1.0 / float(np.median(np.diff(ts))) if n > 1 else 30.0

    video = output / "rgb.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"Cannot open video encoder for {video}")
    try:
        with open(rgb_raw, "rb") as f:
            for index, rec in enumerate(frames):
                f.seek(int(rec["rgb_offset"]))
                jpeg = np.frombuffer(f.read(int(rec["rgb_size"])), dtype=np.uint8)
                image = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
                if image is None or image.shape[:2] != (h, w):
                    raise ValueError(f"Invalid JPEG or dimensions at frame {index}")
                writer.write(image)
    finally:
        writer.release()
    # Some video backends fail writes without raising. Decode the result
    # before allowing --delete-raw to remove the only recoverable input.
    cap = cv2.VideoCapture(str(video))
    decoded = 0
    try:
        while True:
            ok, image = cap.read()
            if not ok:
                break
            if image.shape[:2] != (h, w):
                raise ValueError("Encoded video dimensions differ from source")
            decoded += 1
    finally:
        cap.release()
    if decoded != n:
        raise RuntimeError(f"Encoded video has {decoded} frames; expected {n}")

    np.save(output / "rgb_timestamps.npy", ts)
    _write_depth_npz(output / "depth.npz", depth_raw, n, h, w, ts)
    print(f"  ego_cam  : {n} frames, {duration:.2f}s, ~{fps:.1f} fps "
          f"-> rgb.mp4, rgb_timestamps.npy, depth.npz")
    return [rgb_raw, depth_raw, frames_path]


def finalize_lidar(session: Path, output: Path = None) -> list:
    output = session if output is None else output
    raw = session / TIMESTAMPS_RAW_FILENAME
    if not raw.exists():
        return []
    ts = _read_records(raw, np.dtype("<f8"))
    n = 0
    for index in range(len(ts)):
        scan = session / SCAN_DIRNAME / f"{index:06d}.npy"
        if not scan.exists():
            raise ValueError(f"Missing LiDAR scan for timestamp {index}: {scan}")
        points = np.load(scan, allow_pickle=False)
        if points.ndim != 2 or points.shape[1] != 4 or points.dtype != np.float32:
            raise ValueError(f"Invalid LiDAR scan: {scan}")
        n += 1
    np.save(output / "lidar_timestamps.npy", ts[:n])

    meta_path = session / META_FILENAME
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["num_scans"] = n
        (output / META_FILENAME).write_text(json.dumps(meta, indent=2))
    print(f"  lidar    : {n} scans -> lidar_timestamps.npy")
    return [raw]


def finalize(session: Path, delete_raw: bool):
    print(f"{session}")
    # Convert all streams in a staging directory. Failures preserve raw
    # files and previously finalized outputs. Run only on stopped sessions.
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns)
              for p in session.rglob("*") if p.is_file()}
    with tempfile.TemporaryDirectory(prefix=".finalize-", dir=session) as tmp:
        output = Path(tmp)
        raw_files = (finalize_lowstate(session, output)
                     + finalize_ego_cam(session, output)
                     + finalize_lidar(session, output))
        if any(not p.exists() or (p.stat().st_size, p.stat().st_mtime_ns) != stamp
               for p, stamp in before.items()):
            raise RuntimeError("Session changed during conversion; stop recording first")
        for path in output.rglob("*"):
            if path.is_file():
                target = session / path.relative_to(output)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
    if delete_raw:
        for f in raw_files:
            f.unlink()
        print(f"  removed {len(raw_files)} raw files")


def find_sessions(root: Path) -> list:
    """Every directory under root (inclusive) that still holds raw files."""
    hits = set()
    for marker in (LOWSTATE_FILENAME, TIMESTAMPS_RAW_FILENAME):
        hits.update(p.parent for p in root.rglob(marker))
    hits.update(p.parent.parent for p in root.rglob(f"{CAM_DIRNAME}/{FRAMES_FILENAME}"))
    return sorted(hits)


def main():
    parser = argparse.ArgumentParser(description="Finalize raw Go2 recording sessions")
    parser.add_argument("path", type=Path,
                        help="a first_person/ session directory, or any parent directory")
    parser.add_argument("--delete-raw", action="store_true",
                        help="delete the raw stream files after a successful conversion")
    args = parser.parse_args()

    if not args.path.is_dir():
        parser.error(f"Directory not found: {args.path}")
    sessions = find_sessions(args.path)
    if not sessions:
        print(f"No raw sessions found under {args.path}")
        return
    for session in sessions:
        finalize(session, args.delete_raw)


if __name__ == "__main__":
    main()
