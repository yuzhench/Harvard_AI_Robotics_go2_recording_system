"""
screen_link.py — Serial link to the round status screen (screen/firmware).

The screen is an ESP32-S3 board plugged into a USB port. It renders whatever
status line it is sent and reports presses of its centre circle (hold 1 s:
start / stop; double tap while recording: pause / resume):

    host -> screen   $S,<state>,<elapsed_s>,<disk_used_pct>,<disk_free_gb>,
                        <hz0>,<ok0>, ... ,<hz4>,<ok4>\\n
                     $X,<phase>,<progress>,<current>,<total>\\n
                     $C,<clock_set>\\n
    screen -> host   $E,START\\n   $E,STOP\\n   $E,PAUSE\\n   $E,RESUME\\n

<state> is STATE_CODES below; "paused" is a recording that keeps no data.

"$X" carries the offload tool's progress (see export_api.py) while a laptop
is copying sessions off the robot; phase 0 means no laptop is attached.

"$C" says whether the Jetson's clock has been set since it was switched on (clock.py); while it is 0 the
screen shows CLOCK NOT SET on its idle page. Older firmware ignores the line.

Sensor order is SENSORS below. The screen holds no thresholds of its own —
"is this stream healthy" is decided here by SensorHealth and sent as <ok>.

This module only needs pyserial; it has no dependency on the recorder, so the
same code drives the screen from screen/tools/sim_feed.py on a laptop.
"""

import glob
import threading
import time
from typing import Callable, Dict, Optional

try:
    import serial
except ImportError:
    serial = None


# Display order on the screen, top to bottom.
SENSORS = ["cam", "lidar", "imu", "joint", "foot"]

# Reference rate of each stream (Hz); a stream is flagged below half of it.
# The LiDAR measures ~15 Hz on the robot; 9 is its deliberately loose reference.
NOMINAL_HZ = {
    "cam":   30.0,
    "lidar": 9.0,
    "imu":   500.0,
    "joint": 500.0,
    "foot":  500.0,
}

STATE_CODES = {"idle": 0, "recording": 1, "saving": 2, "error": 3, "paused": 4}
EXPORT_PHASE_CODES = {
    "none": 0, "linked": 1, "transfer": 2, "verify": 3, "delete": 4, "done": 5,
}

# A stream is flagged only when it stays below LOW_FRACTION of nominal for
# BAD_AFTER_S — the target is a sensor that dropped out, not rate jitter.
LOW_FRACTION = 0.5
BAD_AFTER_S = 3.0
GOOD_AFTER_S = 1.0

PORT_GLOB = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*"


class SensorHealth:
    """Debounced healthy/unhealthy flag for one stream."""

    def __init__(self, nominal_hz: float):
        self.nominal_hz = nominal_hz
        self.ok = True
        self._since: Optional[float] = None   # when the opposite condition began

    def reset(self):
        self.ok = True
        self._since = None

    def update(self, hz: float, now: float) -> bool:
        low = hz < LOW_FRACTION * self.nominal_hz
        if low == (not self.ok):
            # Measurement agrees with the current flag.
            self._since = None
            return self.ok
        if self._since is None:
            self._since = now
        wait = BAD_AFTER_S if self.ok else GOOD_AFTER_S
        if now - self._since >= wait:
            self.ok = not self.ok
            self._since = None
        return self.ok


def format_status(
    state: str,
    elapsed_s: float,
    disk_used_pct: float,
    disk_free_gb: float,
    hz: Optional[Dict[str, float]],
    ok: Optional[Dict[str, bool]],
) -> str:
    """Build one status line. `hz=None` blanks the sensor circles."""
    fields = [
        STATE_CODES[state],
        int(elapsed_s),
        int(round(disk_used_pct)),
        int(disk_free_gb),
    ]
    for name in SENSORS:
        if hz is None:
            fields += [-1, 1]
        else:
            fields += [int(round(hz[name])), 1 if ok[name] else 0]
    return "$S," + ",".join(str(f) for f in fields) + "\n"


def format_export(phase: str, progress: float, current: int, total: int) -> str:
    """Build one export-progress line for the screen."""
    return "$X,%d,%d,%d,%d\n" % (
        EXPORT_PHASE_CODES[phase], int(round(progress)), int(current), int(total),
    )


def format_clock(clock_set: bool) -> str:
    """Build one clock line for the screen."""
    return "$C,%d\n" % (1 if clock_set else 0)


class ScreenLink:
    """Owns the serial port: sends status lines, dispatches screen events.

    Survives the screen being absent, unplugged, or powered on late — it
    keeps retrying the port in the background.
    """

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        port: Optional[str] = None,
        on_pause: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
    ):
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._port = port
        self._ser = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def start(self):
        if serial is None:
            print("[ScreenLink] pyserial is unavailable; screen disabled")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close()

    def send(self, line: str):
        """Write one status line. Silently dropped while disconnected."""
        with self._lock:
            ser = self._ser
        if ser is None:
            return
        try:
            ser.write(line.encode("ascii"))
        except (serial.SerialException, OSError):
            self._close()

    # ------------------------------------------------------------------

    def _open(self) -> bool:
        port = self._port
        if port is None:
            matches = sorted(glob.glob(PORT_GLOB))
            if not matches:
                return False
            port = matches[0]
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = 115200
            ser.timeout = 0.2
            ser.write_timeout = 0.5
            # On this chip's USB port DTR/RTS are the reset and boot-mode
            # lines. Keep both released so opening the port never resets the
            # screen or drops it into download mode.
            ser.dtr = False
            ser.rts = False
            ser.open()
        except (serial.SerialException, OSError):
            return False
        with self._lock:
            self._ser = ser
        print(f"[ScreenLink] Connected on {port}")
        return True

    def _close(self):
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            print("[ScreenLink] Disconnected")

    def _read_loop(self):
        buf = b""
        while self._running:
            if self._ser is None:
                if not self._open():
                    time.sleep(1.0)
                    continue
                buf = b""
            try:
                chunk = self._ser.read(64)
            except (serial.SerialException, OSError, AttributeError, TypeError):
                self._close()
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._dispatch(raw.decode("ascii", errors="ignore").strip())

    def _dispatch(self, line: str):
        if line == "$E,START":
            self._on_start()
        elif line == "$E,STOP":
            self._on_stop()
        elif line == "$E,PAUSE" and self._on_pause is not None:
            self._on_pause()
        elif line == "$E,RESUME" and self._on_resume is not None:
            self._on_resume()
