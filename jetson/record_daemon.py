"""
record_daemon.py — FastAPI service that supervises a single Recorder on the
Jetson. Started once at boot by systemd, stays up across many recordings.

Endpoints:
    POST /start   body {"task": "...", "prompt": "..."}
    POST /stop
    GET  /status

Entry point:
    python -m jetson.record_daemon                 # default port 8010
    python -m jetson.record_daemon --port 8020

Process model:
    One Python process. One Recorder instance. Three collectors internally
    owning their own DDS/capture threads. uvicorn worker threads handle
    HTTP. ChannelFactoryInitialize is called exactly once at startup.
"""

import argparse
import re
import subprocess
import sys
import time
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
        "collectors will run in mock mode.",
        file=sys.stderr,
    )

from .config import DATA_ROOT, HOST, INTERFACE, PORT, PROTOCOL_VERSION, TASKS
from .recorder import Recorder


app = FastAPI(title="Go2 Record Daemon")

# Module-level state. Populated in the startup hook.
_recorder: Optional[Recorder] = None
_startup_ts: float = 0.0


class StartRequest(BaseModel):
    task: str
    prompt: str = ""


@app.on_event("startup")
async def on_startup():
    """Initialize DDS once and construct the Recorder."""
    global _recorder, _startup_ts
    _startup_ts = time.time()

    if SDK_AVAILABLE:
        print(f"[record_daemon] ChannelFactoryInitialize(0, {INTERFACE!r})")
        ChannelFactoryInitialize(0, INTERFACE)

    _recorder = Recorder(interface=INTERFACE, data_root=DATA_ROOT)
    print(f"[record_daemon] Ready. HOST={HOST} PORT={PORT} DATA_ROOT={DATA_ROOT}")


@app.post("/start")
def start(req: StartRequest):
    assert _recorder is not None
    try:
        session_dir = _recorder.start(task=req.task, prompt=req.prompt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
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
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "status":          "stopped",
        "session_dir":     result["session_dir"],
        "elapsed_seconds": result["elapsed_seconds"],
        "samples":         result["samples"],
    }


@app.post("/resync_clock")
def resync_clock():
    """Restart chrony to force an immediate re-sync, then report offset.

    The Plan-B chrony config allows a step (instant jump) only during the
    first 3 updates after restart. Restarting triggers that window, so a
    clean, accurate sync happens in a few seconds. After this returns the
    clock is in slew-only mode again — no jumps during the subsequent
    recording.

    Requires passwordless sudo for `systemctl restart chrony`; see
    /etc/sudoers.d/chrony-restart on the Jetson.
    """
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
    try:
        subprocess.run(
            ["chronyc", "-n", "waitsync", "15", "0.005", "100", "1"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        pass  # fall through — /tracking below will show the actual state

    try:
        proc = subprocess.run(
            ["chronyc", "-n", "tracking"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"chronyc tracking failed: {e.stderr}")

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
        offset_s is not None
        and abs(offset_s) < 0.005
        and stratum is not None
        and stratum > 0
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


@app.get("/status")
def status():
    assert _recorder is not None
    return {
        "version":         PROTOCOL_VERSION,
        "state":           _recorder.state,   # "idle" | "recording" | "saving"
        "recording":       _recorder.state == "recording",
        "elapsed":         round(_recorder.elapsed, 2),
        "samples":         _recorder.samples(),
        "live_fps":        _recorder.live_fps(),
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
