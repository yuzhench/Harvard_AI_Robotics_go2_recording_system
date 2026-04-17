"""
master_record.py — Laptop-side master controller.

Drives two FastAPI services simultaneously with the same /start /stop /status
contract, so a single keypress starts or stops both recordings:

    - camera_record_pipeline at  http://localhost:8000
      (4× RealSense RGB+Depth, files land on the laptop)

    - Jetson record_daemon   at  http://<jetson-host>:8010
      (IMU+joints+foot, ego camera, LiDAR, files land on the Jetson)

Both services accept POST /start {"task", "prompt"} and POST /stop.

Usage:
    python laptop/master_record.py                                 # phswifi3 default
    python laptop/master_record.py --host 100.112.18.112           # Tailscale
    python laptop/master_record.py --host 192.168.123.18           # USB ethernet to Go2

Keys:
    T  change task              P  edit prompt
    R  start recording          S  stop recording
    Q  quit (stops first if recording)
"""

import argparse
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Optional

import requests


DEFAULT_JETSON_HOST = "10.100.206.170"     # Jetson wlan0 on phswifi3
DEFAULT_JETSON_PORT = 8010
DEFAULT_CAMERA_URL  = "http://localhost:8000"

TASKS = [f"task{i}" for i in range(1, 11)]
HTTP_TIMEOUT_S = 10.0


@dataclass
class Endpoints:
    camera: str      # e.g. "http://localhost:8000"
    jetson: str      # e.g. "http://10.100.206.170:8010"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(url: str, json: Optional[dict] = None) -> tuple[int, dict]:
    try:
        r = requests.post(url, json=json, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return -1, {"error": str(e)}
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text}
    return r.status_code, body


def _get(url: str) -> tuple[int, dict]:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return -1, {"error": str(e)}
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text}
    return r.status_code, body


def get_status(endpoints: Endpoints) -> dict:
    _, cam = _get(f"{endpoints.camera}/status")
    _, jet = _get(f"{endpoints.jetson}/status")
    return {"camera": cam, "jetson": jet}


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def start_both(task: str, prompt: str, endpoints: Endpoints) -> bool:
    print("  Starting camera pipeline...")
    code, body = _post(f"{endpoints.camera}/start", json={"task": task, "prompt": prompt})
    if code != 200:
        print(f"  [camera] FAILED ({code}): {body}")
        return False
    print(f"  [camera] OK: {body.get('session_dir')}")

    print("  Starting Jetson daemon...")
    code, body = _post(f"{endpoints.jetson}/start", json={"task": task, "prompt": prompt})
    if code != 200:
        print(f"  [jetson] FAILED ({code}): {body}")
        print("  Rolling back camera pipeline...")
        _post(f"{endpoints.camera}/stop")
        return False
    print(f"  [jetson] OK: {body.get('session_dir')}")
    return True


def stop_both(endpoints: Endpoints) -> dict:
    print("  Stopping Jetson daemon...")
    jet_code, jet_body = _post(f"{endpoints.jetson}/stop")
    print(f"  [jetson] {jet_code}: {jet_body}")

    print("  Stopping camera pipeline...")
    cam_code, cam_body = _post(f"{endpoints.camera}/stop")
    print(f"  [camera] {cam_code}: {cam_body}")

    return {"camera": cam_body, "jetson": jet_body}


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------

def _svc_state(svc_status: dict) -> str:
    if not svc_status or "error" in svc_status:
        return "offline"
    return "recording" if svc_status.get("recording") else "idle"


def print_header(task: str, prompt: str, endpoints: Endpoints, status: dict):
    cam_state = _svc_state(status.get("camera", {}))
    jet_state = _svc_state(status.get("jetson", {}))
    # Clear + home cursor
    print("\033[2J\033[H", end="")
    print("┌─────────────────────────────────────────────────────┐")
    print("│  Go2 + Camera Record Master                         │")
    print("├─────────────────────────────────────────────────────┤")
    print(f"│  Task:   {task:<42} │")
    print(f"│  Prompt: {(prompt[:42] or '(none — press P)'):<42} │")
    print(f"│  Jetson: {endpoints.jetson:<30}  ({jet_state:<9}) │")
    print(f"│  Camera: {endpoints.camera:<30}  ({cam_state:<9}) │")
    print("│                                                     │")
    print("│  [T] Change task    [P] Edit prompt                 │")
    print("│  [R] Record         [S] Stop                        │")
    print("│  [Q] Quit                                           │")
    print("└─────────────────────────────────────────────────────┘")


def ask_task() -> str:
    print("\nAvailable tasks:")
    for i, t in enumerate(TASKS, 1):
        print(f"  {i:2d}. {t}")
    while True:
        s = input("Choose task number or name: ").strip()
        if s.isdigit() and 1 <= int(s) <= len(TASKS):
            return TASKS[int(s) - 1]
        if s in TASKS:
            return s
        print("  Invalid. Try again.")


def ask_prompt() -> str:
    return input("Enter prompt: ").strip()


def getch() -> str:
    """Read a single char from stdin in raw mode (no echo, no Enter needed)."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Go2 + Camera master record controller")
    parser.add_argument("--host", default=DEFAULT_JETSON_HOST,
                        help=f"Jetson record_daemon host (default {DEFAULT_JETSON_HOST})")
    parser.add_argument("--jetson-port", type=int, default=DEFAULT_JETSON_PORT)
    parser.add_argument("--camera-url", default=DEFAULT_CAMERA_URL)
    args = parser.parse_args()

    endpoints = Endpoints(
        camera=args.camera_url.rstrip("/"),
        jetson=f"http://{args.host}:{args.jetson_port}",
    )

    task = TASKS[0]
    prompt = ""

    while True:
        status = get_status(endpoints)
        print_header(task, prompt, endpoints, status)

        recording = bool(
            status.get("jetson", {}).get("recording")
            or status.get("camera", {}).get("recording")
        )

        ch = getch().lower()
        print()

        if ch == "q" or ch == "\x03":   # q or Ctrl-C
            if recording:
                print("  Recording in progress — stopping first...")
                stop_both(endpoints)
            print("Bye.")
            return

        elif ch == "t":
            task = ask_task()

        elif ch == "p":
            prompt = ask_prompt()

        elif ch == "r":
            if recording:
                print("  Already recording — press S to stop first.")
                time.sleep(1.0)
                continue
            if not prompt:
                print("  Prompt is empty — press P to enter one first.")
                time.sleep(1.0)
                continue
            if start_both(task, prompt, endpoints):
                print("  ✓ Recording started.")
            time.sleep(1.0)

        elif ch == "s":
            if not recording:
                print("  Not recording.")
                time.sleep(1.0)
                continue
            result = stop_both(endpoints)
            jet = result.get("jetson") or {}
            if "elapsed_seconds" in jet:
                print(f"  ✓ Stopped. Elapsed: {jet['elapsed_seconds']}s")
                print(f"  Samples: {jet.get('samples', {})}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
