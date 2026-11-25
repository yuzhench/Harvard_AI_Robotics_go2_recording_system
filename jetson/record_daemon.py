"""
record_daemon.py — FastAPI service that supervises a single Recorder on the
Jetson. Started once at boot by systemd, stays up across many recordings.

Endpoints:
    POST /start   body {"task": "...", "prompt": "..."}
    POST /stop
    POST /pause    POST /resume      keep the session open, keep no data in between
    POST /resync_clock
    GET  /status
    plus the session export endpoints in export_api.py

The round status screen (screen/firmware), when plugged into a USB port, is
a second way to start and stop: a long-press on it calls the same Recorder,
a double tap pauses or resumes, and it is sent live stream rates and disk
usage a few times a second.

Entry point:
    python -m jetson.record_daemon                 # default port 8010
    python -m jetson.record_daemon --port 8020

Process model:
    One Python process. One Recorder instance. Three collectors internally
    owning their own DDS/capture/disk-writer threads. uvicorn worker threads
    handle HTTP; one more thread feeds the status screen.
    ChannelFactoryInitialize is called exactly once at startup.
"""

import argparse
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print(
        "[record_daemon] WARNING: unitree_sdk2py not importable — "
        "real recording is unavailable.",
        file=sys.stderr,
    )

from .config import (
    DATA_ROOT, HOST, INTERFACE, PORT, PROTOCOL_VERSION, SCREEN_TASK, TASKS,
)
from . import clock, export_api
from .recorder import Recorder, RecorderStateError, RecordingError
from .screen_link import (
    NOMINAL_HZ, SENSORS, ScreenLink, SensorHealth, format_clock, format_export, format_status,
)


# Written by the robot update tool (robot_dog_data_web, branch jetson-recorder):
# lets it confirm that the restarted service runs the code it just copied.
try:
    CODE_VERSION = (Path(__file__).resolve().parent / "CODE_VERSION").read_text().strip()
except OSError:
    CODE_VERSION = "unknown"

app = FastAPI(title="Go2 Record Daemon")
app.include_router(export_api.router)

# Module-level state. Populated in the startup hook.
_recorder: Optional[Recorder] = None
_startup_ts: float = 0.0

# Status screen. A failed start from the screen has no HTTP response to
# carry the error, so it is shown on the screen for SCREEN_ERROR_HOLD_S.
SCREEN_SEND_HZ = 5.0
SCREEN_ERROR_HOLD_S = 5.0
_screen: Optional[ScreenLink] = None
_screen_error_until: float = 0.0


class StartRequest(BaseModel):
    task: str
    prompt: str = ""
    # When the laptop provides these, Jetson uses them verbatim instead of
    # calling datetime.now() locally. Guarantees both sides end up with
    # identical folder names regardless of HTTP round-trip delay. Optional
    # for backward compatibility.
    session_date: Optional[str] = None    # "%m_%d_%Y"
    session_id:   Optional[str] = None    # "%H_%M_%S"


@app.on_event("startup")
async def on_startup():
    """Initialize DDS once and construct the Recorder."""
    global _recorder, _startup_ts
    _startup_ts = time.time()

    if SDK_AVAILABLE:
        print(f"[record_daemon] ChannelFactoryInitialize(0, {INTERFACE!r})")
        ChannelFactoryInitialize(0, INTERFACE)

    _recorder = Recorder(interface=INTERFACE, data_root=DATA_ROOT)
    export_api.configure(DATA_ROOT, lambda: _recorder.session_dir, _recorder.operation_lock)

    global _screen
    _screen = ScreenLink(on_start=_screen_start, on_stop=_screen_stop,
                         on_pause=_screen_pause, on_resume=_screen_resume)
    _screen.start()
    threading.Thread(target=_screen_loop, daemon=True).start()

    print(f"[record_daemon] Ready. HOST={HOST} PORT={PORT} DATA_ROOT={DATA_ROOT}")


@app.on_event("shutdown")
def on_shutdown():
    if _screen is not None:
        _screen.stop()
    if _recorder is not None and _recorder.state == "recording":
        _recorder.stop()


@export_api.exclusive_operation
def _screen_start():
    global _screen_error_until
    try:
        if export_api.export_busy():
            raise RuntimeError("Sessions are being exported to a laptop")
        session_dir = _recorder.start(task=SCREEN_TASK)
        print(f"[record_daemon] Screen start -> {session_dir}")
    except Exception as e:
        print(f"[record_daemon] Screen start failed: {e}")
        _screen_error_until = time.time() + SCREEN_ERROR_HOLD_S


def _screen_stop():
    global _screen_error_until
    try:
        result = _recorder.stop()
        print(f"[record_daemon] Screen stop -> {result['samples']}")
    except Exception as e:
        print(f"[record_daemon] Screen stop failed: {e}")
        _screen_error_until = time.time() + SCREEN_ERROR_HOLD_S
    _correct_recordings()        # the clock may have been set while it was recording


def _screen_pause():
    try:
        _recorder.pause()
        print("[record_daemon] Screen pause")
    except Exception as e:
        print(f"[record_daemon] Screen pause failed: {e}")


def _screen_resume():
    try:
        _recorder.resume()
        print("[record_daemon] Screen resume")
    except Exception as e:
        print(f"[record_daemon] Screen resume failed: {e}")


def _screen_loop():
    """Push recorder state, stream rates and disk usage to the screen."""
    health = {name: SensorHealth(NOMINAL_HZ[name]) for name in SENSORS}
    was_live = False
    clock_was_set = None
    while True:
        now = time.time()
        state = _recorder.state
        recording = state == "recording"
        if recording and _recorder.paused:
            state = "paused"
        live = state == "recording"            # data is being kept
        if live and not was_live:
            for h in health.values():
                h.reset()
        was_live = live

        if live:
            hz = _recorder.live_hz()
            ok = {name: health[name].update(hz[name], now) for name in SENSORS}
        else:
            hz, ok = None, None
        if now < _screen_error_until and not recording:
            state = "error"

        try:
            usage = shutil.disk_usage(DATA_ROOT)
            used_pct = 100.0 * usage.used / usage.total
            free_gb = usage.free / 1e9
        except OSError:
            used_pct, free_gb = 0.0, 0.0

        _screen.send(format_status(state, _recorder.elapsed, used_pct, free_gb, hz, ok))
        ex = export_api.export_state()
        _screen.send(format_export(ex["phase"], ex["progress"], ex["current"], ex["total"]))
        clock_set = clock.trusted()
        _screen.send(format_clock(clock_set))
        if clock_set and clock_was_set is False:       # set meanwhile (e.g. chrony): correct earlier recordings
            _correct_recordings()
        clock_was_set = clock_set
        time.sleep(1.0 / SCREEN_SEND_HZ)


@app.post("/start")
@export_api.exclusive_operation
def start(req: StartRequest):
    assert _recorder is not None
    if export_api.export_busy():
        raise HTTPException(status_code=409, detail="Sessions are being exported to a laptop")
    try:
        session_dir = _recorder.start(
            task=req.task,
            prompt=req.prompt,
            session_date=req.session_date,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RecorderStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RecordingError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "status":      "started",
        "session_dir": str(session_dir),
        "collectors":  ["imu", "ego_camera", "lidar"],
    }


@app.post("/stop")
def stop():
    assert _recorder is not None
    try:
        result = _recorder.stop()
    except RecorderStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RecordingError as e:
        _correct_recordings()
        raise HTTPException(status_code=500, detail=str(e))
    _correct_recordings()        # the clock may have been set while it was recording
    return {
        "status":          "stopped",
        "session_dir":     result["session_dir"],
        "elapsed_seconds": result["elapsed_seconds"],
        "samples":         result["samples"],
    }


@app.post("/pause")
def pause():
    assert _recorder is not None
    try:
        _recorder.pause()
    except RecorderStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "paused", "elapsed": round(_recorder.elapsed, 2)}


@app.post("/resume")
def resume():
    assert _recorder is not None
    try:
        _recorder.resume()
    except RecorderStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "recording", "elapsed": round(_recorder.elapsed, 2)}


@app.post("/resync_clock")
@export_api.exclusive_operation
def resync_clock():
    """Restart chrony to force an immediate re-sync, then report offset.

    The configured startup updates may step the clock. A successful
    waitsync reports convergence; it does not prove the step window ended.

    Requires passwordless sudo for `systemctl restart chrony`; see
    /etc/sudoers.d/chrony-restart on the Jetson.
    """
    if _recorder is None or _recorder.state != "idle":
        raise HTTPException(status_code=409, detail="Clock sync requires an idle recorder")
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "chrony"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f"systemctl restart chrony failed: {e.stderr.strip() or e.stdout.strip()}",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="systemctl not found on PATH")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="systemctl restart chrony timed out")

    # Wait for chrony to actually converge. `chronyc waitsync` polls
    # every `interval` seconds and returns 0 as soon as the clock is
    # within the given max-correction AND max-skew. Args:
    #     max-tries max-correction max-skew interval
    # Here: up to 15 tries (1s each) for offset < 5 ms, skew < 100 ppm.
    # Returns as soon as converged, so fast path is ~4-5s.
    converged = False
    try:
        wait = subprocess.run(
            ["chronyc", "-n", "waitsync", "15", "0.005", "100", "1"],
            capture_output=True, text=True, timeout=20,
        )
        converged = wait.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # tracking below reports failure or the actual state

    try:
        proc = subprocess.run(
            ["chronyc", "-n", "tracking"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=f"chronyc tracking failed: {e}")

    raw = proc.stdout
    offset_s = None
    ref_id = None
    stratum = None
    for line in raw.splitlines():
        # "System time : 0.000010482 seconds slow of NTP time"
        m = re.match(r"System time\s*:\s*([\d.]+)\s+seconds\s+(slow|fast)", line)
        if m:
            sign = -1.0 if m.group(2) == "slow" else 1.0
            offset_s = sign * float(m.group(1))
            continue
        m = re.match(r"Reference ID\s*:\s*\S+\s*\((.+?)\)", line)
        if m:
            ref_id = m.group(1).strip()
            continue
        m = re.match(r"Stratum\s*:\s*(\d+)", line)
        if m:
            stratum = int(m.group(1))

    synced = (
        converged
        and offset_s is not None
        and abs(offset_s) < 0.005
        and stratum is not None
        and 0 < stratum < 16
        and re.search(r"Leap status\s*:\s*Normal", raw) is not None
    )
    return {
        "status":    "ok" if synced else "warning",
        "offset_s":  offset_s,
        "offset_ms": round(offset_s * 1000, 3) if offset_s is not None else None,
        "reference": ref_id,
        "stratum":   stratum,
        "synced":    synced,
        "raw":       raw,
    }


def _correct_recordings():
    """clock_correction.json for recordings of this boot made before the clock was set (clock.py)."""
    try:
        done = clock.correct_sessions(DATA_ROOT, skip=_recorder.session_dir)
        if done:
            print(f"[record_daemon] Clock corrections written: {done}")
    except Exception as e:                       # never let this break a stop
        print(f"[record_daemon] Clock corrections failed: {e}")


@app.get("/status")
def status():
    assert _recorder is not None
    return {
        "version":         PROTOCOL_VERSION,
        "code_version":    CODE_VERSION,
        "state":           _recorder.state,   # "idle" | "recording" | "saving"
        "recording":       _recorder.state == "recording",
        "paused":          _recorder.paused,
        "clock_set":       clock.trusted(),
        "elapsed":         round(_recorder.elapsed, 2),
        "samples":         _recorder.samples(),
        "live_fps":        _recorder.live_fps(),
        "live_hz":         _recorder.live_hz(),
        "screen":          _screen is not None and _screen.connected,
        "uptime_s":        round(time.time() - _startup_ts, 1),
        "current_session": str(_recorder.session_dir) if _recorder.session_dir else None,
        "interface":       INTERFACE,
        "tasks":           TASKS,
    }


def main():
    parser = argparse.ArgumentParser(description="Go2 record daemon (FastAPI)")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    uvicorn.run(
        "jetson.record_daemon:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
