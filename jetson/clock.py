"""
clock.py — Is the Jetson's clock right, and correcting recordings made while it was not.

The Jetson has no clock battery. At boot the clock keeper (install_offload_support.sh) restores the last saved time,
so until something sets the clock, recordings carry the time the robot was last switched off plus the time since
boot. The clock counts as set ("trusted") once, since this boot, the laptop offload tool has sent its time
(POST /time, export_api.py) or chrony reports a synchronised clock. The screen shows CLOCK NOT SET until then.

Every recording stores "clock" in session_meta.json: {"boot_id", "boot_epoch", "trusted"}, where boot_epoch is the
wall clock minus CLOCK_BOOTTIME at its start, i.e. when the clock thought the robot was switched on. Setting the clock
does not change CLOCK_BOOTTIME, so once the clock is right, the error of every earlier recording of the same boot is
known: offset = boot_epoch now − boot_epoch of the recording. That recording then gets
first_person/clock_correction.json:

    {"offset_s": X, "start_time": corrected start, "source": ..., "corrected_at": ..., "boot_id": ...}

Real time = recorded time + offset_s, for every timestamp of the recording (camera, IMU, LiDAR, session_meta.json
start_time and pauses). A recording made in an earlier boot cannot be corrected: its boot's clock is gone.
The recorded files themselves are never changed.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import List, Optional

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
STATE_PATH = Path("/dev/shm/go2-clock.json")        # tmpfs: empty after every boot
CORRECTION_FILENAME = "clock_correction.json"
SESSION_META_FILENAME = "session_meta.json"
CHRONY_CHECK_S = 30.0                               # how often chrony is asked

_chrony_cache = {"t": -1e9, "synced": False}


def boot_id() -> str:
    try:
        return BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


def boot_epoch() -> float:
    """What the wall clock says the robot was switched on (changes when the clock is set)."""
    return time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)


def _state() -> Optional[dict]:
    try:
        s = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) and s.get("boot_id") == boot_id() else None


def chrony_synced() -> bool:
    """chrony is synchronised to a real source (not its own local clock). Asked at most every CHRONY_CHECK_S."""
    now = time.monotonic()
    if now - _chrony_cache["t"] < CHRONY_CHECK_S:
        return _chrony_cache["synced"]
    synced = False
    try:
        out = subprocess.run(["chronyc", "-n", "tracking"], capture_output=True, text=True, timeout=2).stdout
        ref = next((l for l in out.splitlines() if l.startswith("Reference ID")), "")
        leap = next((l for l in out.splitlines() if l.startswith("Leap status")), "")
        synced = leap.split(":", 1)[-1].strip() == "Normal" and "7F7F0101" not in ref and " 00000000 " not in ref + " "
    except (OSError, subprocess.SubprocessError):
        pass
    _chrony_cache.update(t=now, synced=synced)
    return synced


def mark_trusted(source: str) -> bool:
    """Remember that the clock is right in this boot. Returns True if it was not trusted before."""
    if _state():
        return False
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"boot_id": boot_id(), "source": source, "at": time.time()}))
    tmp.replace(STATE_PATH)
    return True


def trusted() -> bool:
    if _state():
        return True
    if chrony_synced():
        mark_trusted("chrony")
        return True
    return False


def stamp() -> dict:
    """The "clock" entry of a new recording's session_meta.json."""
    return {"boot_id": boot_id(), "boot_epoch": round(boot_epoch(), 3), "trusted": trusted()}


def correct_sessions(data_root, skip: Optional[Path] = None) -> List[str]:
    """Write clock_correction.json into every recording of this boot made before the clock was set (not into `skip`,
    the first_person folder of a recording in progress). Does nothing while the clock is not trusted.
    Returns the corrected session folders, relative to data_root."""
    state = _state()
    if not state and not trusted():
        return []
    state = _state() or {"source": "unknown"}
    root, now_epoch, here, done = Path(data_root), boot_epoch(), boot_id(), []
    for meta_path in sorted(root.glob("*/*/*/first_person/" + SESSION_META_FILENAME)):
        first_person = meta_path.parent
        if skip is not None and first_person == Path(skip):
            continue
        target = first_person / CORRECTION_FILENAME
        if target.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        c = meta.get("clock") if isinstance(meta, dict) else None
        if not isinstance(c, dict) or c.get("trusted") or c.get("boot_id") != here or not isinstance(c.get("boot_epoch"), (int, float)):
            continue
        offset = round(now_epoch - c["boot_epoch"], 3)
        start = meta.get("start_time")
        correction = {"offset_s": offset,
                      "start_time": round(start + offset, 3) if isinstance(start, (int, float)) else None,
                      "source": state.get("source"), "corrected_at": round(time.time(), 3), "boot_id": here,
                      "note": "real time = recorded time + offset_s, for every timestamp of this recording"}
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(correction, indent=2))
        tmp.replace(target)
        done.append(first_person.parent.relative_to(root).as_posix())
    return done
