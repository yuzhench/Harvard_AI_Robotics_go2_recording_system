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

from .config import DATA_ROOT, HOST, INTERFACE, PORT, TASKS
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


@app.get("/status")
def status():
    assert _recorder is not None
    return {
        "recording":       _recorder.state == "recording",
        "elapsed":         round(_recorder.elapsed, 2),
        "samples":         _recorder.samples(),
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
