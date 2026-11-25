"""
render_ui.py — Render the status screen's UI states to a PNG for the README.

Re-implements the drawing in screen/firmware/src/main.cpp pixel-for-pixel
(same geometry, same colours, the firmware's own 6x8 bitmap font) so the
picture can be regenerated without the hardware. Keep the constants here in
step with main.cpp when the layout changes.

Usage (from the repo root, after the firmware has been built once so the
font header exists under .pio/):
    python screen/tools/render_ui.py
"""

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
FONT_H = (REPO / "screen/firmware/.pio/libdeps/jxl_esp32s3"
          / "GFX Library for Arduino/src/font/glcdfont.h")
OUT = REPO / "screen/docs/ui_states.png"

CX = CY = 120


def rgb565(c):
    r, g, b = (c >> 11) & 0x1F, (c >> 5) & 0x3F, c & 0x1F
    return (r * 255 // 31, g * 255 // 63, b * 255 // 31)


C_BG, C_WHITE, C_GREY, C_DKGREY, C_PANEL = map(
    rgb565, (0x0000, 0xFFFF, 0x7BEF, 0x39E7, 0x18E3))
C_GREEN, C_RED, C_DKRED, C_AMBER = map(rgb565, (0x07E0, 0xF800, 0xB000, 0xFD20))
C_BLUE, C_DKGREEN, C_DKAMBER = map(rgb565, (0x22BA, 0x1BE9, 0xA280))

SENSOR_NAME = ["CAM", "LIDAR", "IMU", "JOINT", "FOOT"]
SENSOR_ANGLE = [252, 216, 180, 144, 108]
SENSOR_ORBIT, SENSOR_R = 92, 23
CENTER_R, RING_R = 60, 65
PIPE_ORBIT, PIPE_A0, PIPE_A1 = 104, 55.0, -55.0


def load_font():
    body = FONT_H.read_text().split("font[]", 1)[1].split("{", 1)[1].split("};", 1)[0]
    body = re.sub(r"//.*", "", body)
    return [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{2}", body)]


FONT = load_font()


class Screen:
    def __init__(self):
        self.img = Image.new("RGB", (240, 240), C_BG)
        self.d = ImageDraw.Draw(self.img)

    def fill_circle(self, x, y, r, color):
        self.d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def draw_circle(self, x, y, r, color):
        self.d.ellipse([x - r, y - r, x + r, y + r], outline=color)

    def text_centered(self, s, cx, cy, size, color):
        x0 = cx - len(s) * 6 * size // 2 + size // 2
        y0 = cy - 8 * size // 2 + size // 2
        for i, ch in enumerate(s):
            glyph = FONT[ord(ch) * 5: ord(ch) * 5 + 5]
            for col, bits in enumerate(glyph):
                for row in range(8):
                    if bits & (1 << row):
                        px = x0 + (i * 6 + col) * size
                        py = y0 + row * size
                        self.d.rectangle([px, py, px + size - 1, py + size - 1], fill=color)


def polar(deg, r):
    a = math.radians(deg)
    return CX + round(r * math.cos(a)), CY + round(r * math.sin(a))


def frange(a, b, step):
    while a >= b:
        yield a
        a -= step


def render(state, linked=True, elapsed=0, disk_pct=42, free_gb=128,
           hz=None, ok=None, alarm_phase=False, hold=None, export=None):
    """`export` is (phase, progress, current, total) with phase one of
    "linked", "copy", "verify", "delete", "done"."""
    s = Screen()
    show = linked and state == "rec"

    for i, name in enumerate(SENSOR_NAME):
        x, y = polar(SENSOR_ANGLE[i], SENSOR_ORBIT)
        live = show and hz is not None
        good = live and ok[i]
        border = C_DKGREY if not live else (C_GREEN if good else C_RED)
        s.fill_circle(x, y, SENSOR_R, border)
        s.fill_circle(x, y, SENSOR_R - 3, C_PANEL)
        s.text_centered(name, x, y - 10, 1, C_WHITE if live else C_GREY)
        s.text_centered(str(hz[i]) if live else "--", x, y + 5, 2,
                        (C_WHITE if good else C_RED) if live else C_GREY)

    for a in frange(PIPE_A0, PIPE_A1, 1.5):
        s.fill_circle(*polar(a, PIPE_ORBIT), 8, C_DKGREY)
    if linked:
        fill = C_RED if disk_pct >= 90 else (C_AMBER if disk_pct >= 70 else C_GREEN)
        a_end = PIPE_A0 + (PIPE_A1 - PIPE_A0) * disk_pct / 100.0
        for a in frange(PIPE_A0, a_end, 1.5):
            s.fill_circle(*polar(a, PIPE_ORBIT), 5, fill)
        s.text_centered("FREE", CX + 80, CY - 6, 1, C_GREY)
        s.text_centered(f"{free_gb}G", CX + 80, CY + 6, 1, C_WHITE)

    ex_phase = export[0] if export else None
    if not linked:
        s.fill_circle(CX, CY, CENTER_R, C_PANEL)
        s.text_centered("NO", CX, CY - 14, 3, C_AMBER)
        s.text_centered("LINK", CX, CY + 14, 3, C_AMBER)
    elif state not in ("rec", "paused") and ex_phase in ("copy", "verify"):
        s.fill_circle(CX, CY, CENTER_R, C_BLUE)
        s.text_centered("EXPORT" if ex_phase == "copy" else "VERIFY", CX, CY - 30, 2, C_WHITE)
        s.text_centered(f"{export[1]}%", CX, CY, 4, C_WHITE)
        s.text_centered(f"{export[2]}/{export[3]}", CX, CY + 32, 2, C_WHITE)
    elif state not in ("rec", "paused") and ex_phase == "delete":
        s.fill_circle(CX, CY, CENTER_R, C_DKRED)
        s.text_centered("DELETE", CX, CY, 3, C_WHITE)
    elif state not in ("rec", "paused") and ex_phase == "done":
        s.fill_circle(CX, CY, CENTER_R, C_DKGREEN)
        s.text_centered("DONE", CX, CY - 10, 3, C_WHITE)
        s.text_centered("UNPLUG OK", CX, CY + 20, 1, C_WHITE)
    elif state == "rec" and alarm_phase:
        s.fill_circle(CX, CY, CENTER_R, C_BG)
        s.draw_circle(CX, CY, CENTER_R, C_RED)
        s.text_centered("!", CX, CY, 9, C_RED)
    elif state == "rec":
        s.fill_circle(CX, CY, CENTER_R, C_DKRED)
        s.text_centered("REC", CX, CY - 24, 3, C_WHITE)
        s.text_centered(f"{elapsed // 60:02d}:{elapsed % 60:02d}", CX, CY + 2, 3, C_WHITE)
        s.text_centered("TAP x2 PAUSE", CX, CY + 30, 1, C_WHITE)
        s.text_centered("HOLD STOP", CX, CY + 42, 1, C_WHITE)
    elif state == "paused":
        s.fill_circle(CX, CY, CENTER_R, C_DKAMBER)
        s.text_centered("PAUSED", CX, CY - 26, 2, C_WHITE)
        s.text_centered(f"{elapsed // 60:02d}:{elapsed % 60:02d}", CX, CY + 2, 3, C_WHITE)
        s.text_centered("TAP x2 RESUME", CX, CY + 30, 1, C_WHITE)
        s.text_centered("HOLD STOP", CX, CY + 42, 1, C_WHITE)
    elif state == "saving":
        s.fill_circle(CX, CY, CENTER_R, C_AMBER)
        s.text_centered("SAVING", CX, CY, 3, C_BG)
    elif state == "error":
        s.fill_circle(CX, CY, CENTER_R, C_BG)
        s.draw_circle(CX, CY, CENTER_R, C_RED)
        s.text_centered("ERROR", CX, CY, 3, C_RED)
    else:
        s.fill_circle(CX, CY, CENTER_R, C_DKGREY)
        s.text_centered("IDLE", CX, CY - 10, 3, C_WHITE)
        s.text_centered("HOLD TO REC", CX, CY + 20, 1, C_WHITE)
        if ex_phase == "linked":
            s.text_centered("PC LINKED", CX, CY + 36, 1, C_GREEN)

    if hold is not None:
        for a in frange(360.0 * hold, 0, 2.0):
            s.fill_circle(*polar(a - 90, RING_R), 2, C_WHITE)
    return s.img


def main():
    good_hz = [30, 15, 500, 500, 500]
    panels = [
        ("IDLE", render("idle")),
        ("HOLD 1 s", render("idle", hold=0.65)),
        ("RECORDING", render("rec", elapsed=83, hz=good_hz, ok=[1] * 5)),
        ("PAUSED", render("paused", elapsed=83)),
        ("SENSOR DROPOUT", render("rec", elapsed=97, hz=[30, 0, 500, 500, 500],
                                  ok=[1, 0, 1, 1, 1], alarm_phase=True)),
        ("SAVING", render("saving")),
        ("NO LINK", render("idle", linked=False)),
        ("LAPTOP CONNECTED", render("idle", export=("linked", 0, 0, 0))),
        ("EXPORTING", render("idle", export=("copy", 62, 3, 8))),
        ("VERIFYING", render("idle", export=("verify", 40, 5, 8))),
        ("EXPORT DONE", render("idle", export=("done", 100, 8, 8))),
    ]

    scale, pad, label_h = 2, 24, 34
    size = 240 * scale
    cols = 6
    rows = (len(panels) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (size + pad) + pad,
                              rows * (size + label_h + pad) + pad), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
    except OSError:
        font = ImageFont.load_default()

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    for i, (label, img) in enumerate(panels):
        x = pad + (i % cols) * (size + pad)
        y = pad + (i // cols) * (size + label_h + pad)
        big = img.resize((size, size), Image.NEAREST)
        sheet.paste(big, (x, y), mask)
        w = draw.textlength(label, font=font)
        draw.text((x + (size - w) / 2, y + size + 8), label, fill=(0, 0, 0), font=font)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
