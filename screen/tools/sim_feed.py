"""
sim_feed.py — Drive the status screen with fake recorder data.

Lets the screen firmware be exercised from a laptop with no robot and no
Jetson: it plays the role of record_daemon, reacting to the screen's
long-press start/stop and double-tap pause/resume, and feeding back
plausible sensor rates.

Usage (from the repo root):
    python screen/tools/sim_feed.py
    python screen/tools/sim_feed.py --drop lidar@8      # lidar dies 8 s into a recording
    python screen/tools/sim_feed.py --drop lidar@8 --recover 10
    python screen/tools/sim_feed.py --autostart         # start recording immediately
"""

import argparse
import random
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jetson.screen_link import (  # noqa: E402
    NOMINAL_HZ, SENSORS, ScreenLink, SensorHealth, format_status,
)

SEND_HZ = 5.0
SAVING_S = 2.0      # pretend flush time after a stop


class FakeRecorder:
    def __init__(self):
        self.state = "idle"
        self.start_ts = 0.0
        self.saving_until = 0.0
        self.paused_at = None        # start of the current pause
        self.paused_total = 0.0
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.state == "idle":
                self.state = "recording"
                self.start_ts = time.time()
                self.paused_at, self.paused_total = None, 0.0
                print("[sim] START from screen -> recording")

    def pause(self):
        with self._lock:
            if self.state == "recording" and self.paused_at is None:
                self.paused_at = time.time()
                print("[sim] PAUSE from screen")

    def resume(self):
        with self._lock:
            if self.state == "recording" and self.paused_at is not None:
                self.paused_total += time.time() - self.paused_at
                self.paused_at = None
                print("[sim] RESUME from screen")

    def stop(self):
        with self._lock:
            if self.state == "recording":
                self.state = "saving"
                self.saving_until = time.time() + SAVING_S
                print("[sim] STOP from screen -> saving")

    def tick(self):
        with self._lock:
            if self.state == "saving" and time.time() >= self.saving_until:
                self.state = "idle"
                print("[sim] saved -> idle")

    @property
    def screen_state(self) -> str:
        return "paused" if self.state == "recording" and self.paused_at is not None else self.state

    @property
    def elapsed(self) -> float:
        if self.state != "recording":
            return 0.0
        end = self.paused_at if self.paused_at is not None else time.time()
        return end - self.start_ts - self.paused_total


def main():
    parser = argparse.ArgumentParser(description="Fake data feeder for the status screen")
    parser.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    parser.add_argument("--drop", default=None, metavar="SENSOR@SECONDS",
                        help=f"make one of {SENSORS} drop to 0 Hz this long into a recording")
    parser.add_argument("--recover", type=float, default=None, metavar="SECONDS",
                        help="seconds after the drop at which the sensor comes back")
    parser.add_argument("--autostart", action="store_true",
                        help="begin recording without waiting for the screen")
    parser.add_argument("--duration", type=float, default=None,
                        help="exit after this many seconds")
    args = parser.parse_args()

    drop_name, drop_at = None, 0.0
    if args.drop:
        drop_name, at = args.drop.split("@")
        drop_at = float(at)
        if drop_name not in SENSORS:
            parser.error(f"--drop sensor must be one of {SENSORS}")

    rec = FakeRecorder()
    link = ScreenLink(on_start=rec.start, on_stop=rec.stop, port=args.port,
                      on_pause=rec.pause, on_resume=rec.resume)
    link.start()
    health = {name: SensorHealth(NOMINAL_HZ[name]) for name in SENSORS}
    if args.autostart:
        rec.start()

    t_begin = time.time()
    was_recording = False
    try:
        while args.duration is None or time.time() - t_begin < args.duration:
            rec.tick()
            now = time.time()
            usage = shutil.disk_usage(Path.home())
            used_pct = 100.0 * usage.used / usage.total
            free_gb = usage.free / 1e9

            recording = rec.screen_state == "recording"     # live, not paused
            if recording and not was_recording:
                for h in health.values():
                    h.reset()
            was_recording = recording

            if recording:
                hz = {n: NOMINAL_HZ[n] * random.uniform(0.96, 1.02) for n in SENSORS}
                if drop_name and rec.elapsed >= drop_at:
                    if args.recover is None or rec.elapsed < drop_at + args.recover:
                        hz[drop_name] = 0.0
                ok = {n: health[n].update(hz[n], now) for n in SENSORS}
            else:
                hz, ok = None, None

            link.send(format_status(rec.screen_state, rec.elapsed, used_pct, free_gb, hz, ok))
            time.sleep(1.0 / SEND_HZ)
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()


if __name__ == "__main__":
    main()
