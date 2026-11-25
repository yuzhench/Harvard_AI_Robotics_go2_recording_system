"""
export_api.py — HTTP endpoints for moving recorded sessions off the Jetson.

Used by the offload tool (github.com/yuzhench/go2_data_collection) running on a laptop plugged into
the robot's Ethernet port:

    GET  /sessions                      list every session under DATA_ROOT
    GET  /sessions/thumbnail?id=        one JPEG frame from the middle of a session
    GET  /sessions/files?id=            files of a session with sizes
    GET  /sessions/file?id=&path=       one file (supports HTTP Range, so resumable)
    POST /sessions/pack                 many small files in one response
    POST /sessions/checksums            sha256 of a batch of files
    POST /sessions/mark_exported        remember that a session was copied and verified
    POST /sessions/delete               delete sessions
    POST /export/state                  the tool's phase + progress, shown on the screen
    POST /time                          set the Jetson clock from the laptop

A session id is its path relative to DATA_ROOT, e.g.
"unlabeled/10_05_2026/21_40_39".
"""

import hashlib
from functools import wraps
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from utility.egocentric_camera import (
    CAM_DIRNAME, FRAME_DTYPE, FRAMES_FILENAME, RGB_FILENAME,
)

from .config import DATA_ROOT
from .recorder import SESSION_META_FILENAME

EXPORTED_MARKER = ".exported.json"

# Export phases, in the order the offload tool walks through them.
PHASES = ["none", "linked", "transfer", "verify", "delete", "done"]
# Phases during which the session files must not change under the tool.
BUSY_PHASES = {"transfer", "verify", "delete"}
# The tool re-posts its state about once a second; without that for this
# long it is considered gone (cable pulled, laptop closed).
STATE_TIMEOUT_S = 5.0

# Only correct the clock when it is clearly wrong, so an already synchronised
# clock is never stepped by a laptop that is a few hundred ms off.
CLOCK_TOLERANCE_S = 2.0

router = APIRouter()

_data_root = Path(DATA_ROOT)
_is_recording: Callable[[], Optional[Path]] = lambda: None
_lock = threading.Lock()
_operation_lock = threading.RLock()
_export_state: Dict = {"phase": "none", "progress": 0, "current": 0, "total": 0, "ts": 0.0}
_size_cache: Dict[str, tuple] = {}


def configure(data_root: str, recording_session_dir: Callable[[], Optional[Path]], operation_lock=None):
    """Called once by record_daemon at startup."""
    global _data_root, _is_recording, _operation_lock
    _data_root = Path(data_root)
    _is_recording = recording_session_dir
    if operation_lock is not None:
        _operation_lock = operation_lock


def exclusive_operation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _operation_lock:
            return fn(*args, **kwargs)
    return wrapped


def export_state() -> Dict:
    """Current export phase for the screen; falls back to 'none' when stale."""
    with _lock:
        state = dict(_export_state)
    if time.monotonic() - state["ts"] > STATE_TIMEOUT_S:
        state.update(phase="none", progress=0, current=0, total=0)
    return state


def export_busy() -> bool:
    """True while the offload tool is reading or deleting session files."""
    return export_state()["phase"] in BUSY_PHASES


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _session_dir(session_id: str) -> Path:
    """Resolve a session id, refusing anything outside DATA_ROOT."""
    root = _data_root.resolve()
    path = (root / session_id).resolve()
    if (root not in path.parents or len(path.relative_to(root).parts) != 3
            or not path.is_dir() or not (path / "first_person").is_dir()):
        raise HTTPException(status_code=404, detail=f"No such session: {session_id}")
    return path


def _session_file(session_id: str, rel_path: str) -> Path:
    base = _session_dir(session_id)
    path = (base / rel_path).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such file: {rel_path}")
    return path


def _find_sessions() -> List[Path]:
    """Session directories are the parents of every first_person/ folder."""
    if not _data_root.is_dir():
        return []
    return sorted(p.parent for p in _data_root.glob("*/*/*/first_person") if p.is_dir())


def _dir_size(path: Path, recording: bool) -> tuple:
    """(bytes, file count). Cached for finished sessions, whose files no
    longer change; a session being recorded is measured every time."""
    key = str(path)
    stamp = max(path.stat().st_mtime, (path / "first_person").stat().st_mtime)
    cached = _size_cache.get(key)
    if not recording and cached and cached[0] == stamp:
        return cached[1], cached[2]
    total, count = 0, 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            count += 1
    if not recording:
        _size_cache[key] = (stamp, total, count)
    return total, count


def _describe(path: Path) -> Dict:
    session_id = path.relative_to(_data_root).as_posix()
    first_person = path / "first_person"
    meta = {}
    meta_path = first_person / SESSION_META_FILENAME
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
    marker = path / EXPORTED_MARKER
    exported = None
    if marker.exists():
        try:
            exported = json.loads(marker.read_text())
        except (OSError, ValueError):
            exported = {}
    recording_dir = _is_recording()
    recording = recording_dir is not None and first_person == recording_dir
    size_bytes, n_files = _dir_size(path, recording)
    return {
        "id":            session_id,
        "task":          meta.get("task", path.parts[-3]),
        "start_time":    meta.get("start_time"),
        "duration_s":    meta.get("elapsed_seconds"),
        "samples":       meta.get("samples"),
        "clean_stop":    meta.get("clean_stop"),
        "size_bytes":    size_bytes,
        "n_files":       n_files,
        "exported":      exported,
        "recording":     recording,
        "has_thumbnail": (first_person / CAM_DIRNAME / FRAMES_FILENAME).exists(),
    }


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

@router.get("/sessions")
def list_sessions():
    usage = shutil.disk_usage(_data_root) if _data_root.is_dir() else None
    return {
        "sessions":   [_describe(p) for p in _find_sessions()],
        "disk_total": usage.total if usage else 0,
        "disk_free":  usage.free if usage else 0,
        "time":       time.time(),
    }


@router.get("/sessions/thumbnail")
def thumbnail(id: str):
    """The JPEG of the middle frame, straight out of rgb.mjpeg."""
    cam = _session_dir(id) / "first_person" / CAM_DIRNAME
    frames_path, rgb_path = cam / FRAMES_FILENAME, cam / RGB_FILENAME
    if not frames_path.exists() or not rgb_path.exists():
        raise HTTPException(status_code=404, detail="Session has no raw RGB stream")
    n = frames_path.stat().st_size // FRAME_DTYPE.itemsize
    if n == 0:
        raise HTTPException(status_code=404, detail="Session has no frames")
    frames = np.fromfile(frames_path, dtype=FRAME_DTYPE, count=n)
    rec = frames[n // 2]
    with open(rgb_path, "rb") as f:
        f.seek(int(rec["rgb_offset"]))
        jpeg = f.read(int(rec["rgb_size"]))
    return Response(content=jpeg, media_type="image/jpeg")


@router.get("/sessions/files")
def list_files(id: str):
    base = _session_dir(id)
    files = [
        {"path": f.relative_to(base).as_posix(), "size": f.stat().st_size}
        for f in sorted(base.rglob("*"))
        if f.is_file() and f.name != EXPORTED_MARKER
    ]
    return {"id": id, "files": files}


@router.get("/sessions/file")
def get_file(id: str, path: str):
    return FileResponse(_session_file(id, path))


class PackRequest(BaseModel):
    id: str
    paths: List[str]


@router.post("/sessions/pack")
def pack(req: PackRequest):
    """Stream several files back to back, each as an 8-byte little-endian
    length followed by its bytes, in the order requested.

    A session holds thousands of small LiDAR scans; fetching them one
    request at a time costs ~12 ms each, far more than the data itself.
    """
    files = [_session_file(req.id, rel) for rel in req.paths]

    def body():
        for f in files:
            yield f.stat().st_size.to_bytes(8, "little")
            with f.open("rb") as source:
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    yield chunk

    return StreamingResponse(body(), media_type="application/octet-stream")


class ChecksumRequest(BaseModel):
    id: str
    paths: List[str]


@router.post("/sessions/checksums")
def checksums(req: ChecksumRequest):
    out = {}
    for rel in req.paths:
        h = hashlib.sha256()
        with open(_session_file(req.id, rel), "rb") as f:
            for chunk in iter(lambda: f.read(4 << 20), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return {"id": req.id, "sha256": out}


class MarkRequest(BaseModel):
    id: str
    host: str = ""


@router.post("/sessions/mark_exported")
@exclusive_operation
def mark_exported(req: MarkRequest):
    marker = _session_dir(req.id) / EXPORTED_MARKER
    marker.write_text(json.dumps({"time": time.time(), "host": req.host}))
    return {"status": "ok"}


class DeleteRequest(BaseModel):
    ids: List[str]


@router.post("/sessions/delete")
@exclusive_operation
def delete_sessions(req: DeleteRequest):
    recording_dir = _is_recording()
    deleted, skipped = [], []
    for session_id in req.ids:
        path = _session_dir(session_id)
        if recording_dir is not None and recording_dir.parent == path:
            skipped.append({"id": session_id, "reason": "recording in progress"})
            continue
        shutil.rmtree(path)
        _size_cache.pop(str(path), None)
        deleted.append(session_id)
        # Drop the date and task folders once they are empty.
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
    return {"deleted": deleted, "skipped": skipped}


class StateRequest(BaseModel):
    phase: str
    progress: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)     # 0..100 within the current phase
    current: int = Field(default=0, ge=0)          # session being processed (1-based)
    total: int = Field(default=0, ge=0)            # sessions selected


@router.post("/export/state")
@exclusive_operation
def set_export_state(req: StateRequest):
    if req.phase not in PHASES:
        raise HTTPException(status_code=400, detail=f"Unknown phase {req.phase!r}")
    if req.phase in BUSY_PHASES and _is_recording() is not None:
        raise HTTPException(status_code=409, detail="Recording in progress")
    with _lock:
        _export_state.update(
            phase=req.phase, progress=req.progress,
            current=req.current, total=req.total, ts=time.monotonic(),
        )
    return {"status": "ok"}


class TimeRequest(BaseModel):
    unix_time: float = Field(gt=0, allow_inf_nan=False)


@router.post("/time")
@exclusive_operation
def set_time(req: TimeRequest):
    """Step the clock to the laptop's time if it is off by more than
    CLOCK_TOLERANCE_S. Needs the sudoers rule installed by
    jetson/install_offload_support.sh."""
    offset = req.unix_time - time.time()
    if abs(offset) <= CLOCK_TOLERANCE_S:
        return {"status": "unchanged", "offset_s": round(offset, 3)}
    if _is_recording() is not None:
        # Stepping the clock mid-recording would break the timestamps.
        return {"status": "skipped", "offset_s": round(offset, 3), "detail": "recording in progress"}
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/bin/date", "-s", f"@{req.unix_time:.3f}"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or str(e)
        return {"status": "failed", "offset_s": round(offset, 3), "detail": detail.strip()}
    return {"status": "set", "offset_s": round(offset, 3)}
