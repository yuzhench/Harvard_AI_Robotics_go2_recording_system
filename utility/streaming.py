"""
streaming.py — Small helpers shared by the collectors for write-as-you-record.

Collectors append to raw files while recording instead of buffering a whole
session in memory. Periodic fsync reduces buffered-data loss, but queue
backlog, storage failures and power cuts can lose more than one interval.
scripts/finalize_session.py turns the raw files into the final layout offline.
"""

import os
import threading
import time
from collections import deque
from pathlib import Path

# How often appended data is forced through the OS cache onto the disk.
SYNC_INTERVAL_S = 2.0


class AppendFile:
    """Append-only binary file that fsyncs itself every SYNC_INTERVAL_S."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._f = open(self.path, "wb")
        self._last_sync = time.monotonic()
        self.bytes_written = 0

    def write(self, data) -> None:
        self._f.write(data)
        self.bytes_written += len(data)
        now = time.monotonic()
        if now - self._last_sync >= SYNC_INTERVAL_S:
            self.sync()
            self._last_sync = now

    def sync(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        if self._f.closed:
            return
        try:
            self.sync()
        finally:
            self._f.close()


class PauseGate:
    """Pause/resume switch of one collector: samples are dropped while paused.

    gap_within(first, last) is how much of the recorded span [first, last]
    was paused, so a collector can leave pauses out of its duration and rate.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.paused = False
        self._intervals = []        # [start, end]; end is None while paused

    def pause(self, now: float) -> None:
        if not self.paused:
            self._intervals.append([now, None])
            self.paused = True

    def resume(self, now: float) -> None:
        if self.paused:
            self._intervals[-1][1] = now
            self.paused = False

    def gap_within(self, first: float, last: float) -> float:
        total = 0.0
        for a, b in self._intervals:
            b = last if b is None else b
            total += max(0.0, min(b, last) - max(a, first))
        return total


class RateMeter:
    """Arrival rate over a sliding window, safe to read from another thread."""

    def __init__(self, window_s: float = 1.0):
        self.window_s = window_s
        self._ticks: deque = deque()
        self._lock = threading.Lock()

    def tick(self, ts: float) -> None:
        with self._lock:
            self._ticks.append(ts)
            cutoff = ts - self.window_s
            while self._ticks and self._ticks[0] < cutoff:
                self._ticks.popleft()

    def hz(self) -> float:
        cutoff = time.time() - self.window_s
        with self._lock:
            while self._ticks and self._ticks[0] < cutoff:
                self._ticks.popleft()
            return len(self._ticks) / self.window_s

    def reset(self) -> None:
        with self._lock:
            self._ticks.clear()
