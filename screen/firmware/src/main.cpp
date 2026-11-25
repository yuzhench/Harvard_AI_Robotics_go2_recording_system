// Recording monitor for the Go2 record pipeline.
//
// The screen is a dumb terminal: the host (jetson/screen_link.py) sends one
// status line several times a second and this firmware renders it. Presses
// on the centre circle send requests back: hold 1 s to start (idle) or stop
// (recording or paused); double tap to pause (recording) or resume (paused).
// "$C,<0|1>" says whether the Jetson's clock has been set since it was switched
// on; while it is 0 the idle page shows CLOCK NOT SET (hosts that never send it
// get no warning).
//
// Host -> screen:
//   $S,<state>,<elapsed_s>,<disk_used_pct>,<disk_free_gb>,
//      <hz0>,<ok0>,<hz1>,<ok1>,<hz2>,<ok2>,<hz3>,<ok3>,<hz4>,<ok4>\n
//   state: 0 idle, 1 recording, 2 saving, 3 error, 4 paused.  hz < 0 means "not shown".
//   Sensor order: CAM, LIDAR, IMU, JOINT, FOOT.
//   $X,<phase>,<progress>,<current>,<total>\n
//   Progress of a laptop copying sessions off the robot.
//   phase: 0 none, 1 laptop linked, 2 copying, 3 verifying, 4 deleting, 5 done.
// Screen -> host:
//   $E,START\n   $E,STOP\n   $E,PAUSE\n   $E,RESUME\n
//
// Lines that start with neither "$S," nor "$X," are ignored, so tty echo of
// our own "$E" lines is harmless.
#include <Arduino.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>
#include "board_pins.h"

// ---------------------------------------------------------------- display
static Arduino_DataBus *bus = new Arduino_ESP32SPI(
    PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_SCK, PIN_LCD_MOSI, GFX_NOT_DEFINED);
static Arduino_GFX *panel = new Arduino_GC9A01(bus, PIN_LCD_RST, 0, true);
static Arduino_Canvas *gfx = new Arduino_Canvas(240, 240, panel);

static const int CX = 120, CY = 120;

// RGB565
static const uint16_t C_BG      = 0x0000;
static const uint16_t C_WHITE   = 0xFFFF;
static const uint16_t C_GREY    = 0x7BEF;
static const uint16_t C_DKGREY  = 0x39E7;
static const uint16_t C_PANEL   = 0x18E3;
static const uint16_t C_GREEN   = 0x07E0;
static const uint16_t C_RED     = 0xF800;
static const uint16_t C_DKRED   = 0xB000;
static const uint16_t C_AMBER   = 0xFD20;
static const uint16_t C_BLUE    = 0x22BA;
static const uint16_t C_DKGREEN = 0x1BE9;
static const uint16_t C_DKAMBER = 0xA280;

// ---------------------------------------------------------------- layout
static const int N_SENSORS = 5;
static const char *SENSOR_NAME[N_SENSORS] = {"CAM", "LIDAR", "IMU", "JOINT", "FOOT"};
static const float SENSOR_ANGLE[N_SENSORS] = {252, 216, 180, 144, 108};  // deg, y down
static const int SENSOR_ORBIT = 92;
static const int SENSOR_R = 23;

static const int CENTER_R = 60;
static const int RING_R = 65;          // long-press progress ring

static const int PIPE_ORBIT = 104;     // disk gauge on the right edge
static const float PIPE_A0 = 55;       // bottom end (0 GB used)
static const float PIPE_A1 = -55;      // top end (full)

// ---------------------------------------------------------------- state
enum HostState { ST_IDLE = 0, ST_REC = 1, ST_SAVING = 2, ST_ERROR = 3, ST_PAUSED = 4 };

struct Status {
  int state = ST_IDLE;
  int elapsed_s = 0;
  int disk_pct = 0;
  int free_gb = 0;
  int hz[N_SENSORS] = {-1, -1, -1, -1, -1};
  int ok[N_SENSORS] = {1, 1, 1, 1, 1};
};

static Status st;
static uint32_t last_status_ms = 0;
static bool ever_linked = false;

enum ExportPhase { EX_NONE = 0, EX_LINKED = 1, EX_COPY = 2, EX_VERIFY = 3,
                   EX_DELETE = 4, EX_DONE = 5 };

struct Export {
  int phase = EX_NONE;
  int progress = 0;      // percent of the current phase
  int current = 0;       // session being processed
  int total = 0;         // sessions selected
};

static Export ex;
static uint32_t last_export_ms = 0;

static const uint32_t LINK_TIMEOUT_MS = 2000;
static const uint32_t EXPORT_TIMEOUT_MS = 3000;
static const uint32_t HOLD_MS = 1000;
static const uint32_t PENDING_TIMEOUT_MS = 3000;
static const uint32_t TAP_MAX_MS = 300;       // a shorter press is a tap
static const uint32_t DOUBLE_TAP_MS = 500;    // second tap must end this soon after the first
static const uint32_t RING_DELAY_MS = 200;    // no hold ring for taps

static bool holding = false;       // finger down on the centre circle
static bool hold_fired = false;    // request already sent for this press
static uint32_t hold_start_ms = 0;
static bool pending = false;       // request sent, waiting for state change
static int pending_from_state = 0;
static uint32_t pending_ms = 0;
static bool tap_waiting = false;   // one tap done, a second makes a double tap
static uint32_t last_tap_ms = 0;

// ---------------------------------------------------------------- touch
static const uint8_t TOUCH_ADDR = 0x15;

static bool touch_get(int *x, int *y) {
  Wire.beginTransmission(TOUCH_ADDR);
  Wire.write(0x01);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(TOUCH_ADDR, (uint8_t)6) != 6) return false;
  uint8_t d[6];
  for (int i = 0; i < 6; i++) d[i] = Wire.read();
  if (d[1] == 0) return false;
  *x = ((d[2] & 0x0F) << 8) | d[3];
  *y = ((d[4] & 0x0F) << 8) | d[5];
  return true;
}

// ---------------------------------------------------------------- serial
static char line[200];
static size_t line_len = 0;

static bool clock_set = true;      // last "$C" from the host
static uint32_t last_clock_ms = 0;
static const uint32_t CLOCK_TIMEOUT_MS = 5000;

static bool clock_warning(uint32_t now) {
  return !clock_set && last_clock_ms != 0 && now - last_clock_ms < CLOCK_TIMEOUT_MS;
}

static void parse_line(const char *s) {
  if (strncmp(s, "$C,", 3) == 0) {
    int v;
    if (sscanf(s + 3, "%d", &v) != 1) return;
    clock_set = v != 0;
    last_clock_ms = millis();
    return;
  }
  if (strncmp(s, "$X,", 3) == 0) {
    int v[4];
    if (sscanf(s + 3, "%d,%d,%d,%d", &v[0], &v[1], &v[2], &v[3]) != 4) return;
    ex.phase = v[0];
    ex.progress = constrain(v[1], 0, 100);
    ex.current = v[2];
    ex.total = v[3];
    last_export_ms = millis();
    return;
  }
  if (strncmp(s, "$S,", 3) != 0) return;
  int v[14];
  int n = sscanf(s + 3, "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                 &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6],
                 &v[7], &v[8], &v[9], &v[10], &v[11], &v[12], &v[13]);
  if (n != 14) return;
  st.state = v[0];
  st.elapsed_s = v[1];
  st.disk_pct = constrain(v[2], 0, 100);
  st.free_gb = v[3];
  for (int i = 0; i < N_SENSORS; i++) {
    st.hz[i] = v[4 + 2 * i];
    st.ok[i] = v[5 + 2 * i];
  }
  last_status_ms = millis();
  ever_linked = true;
}

static void poll_serial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      line[line_len] = 0;
      if (line_len > 0) parse_line(line);
      line_len = 0;
    } else if (line_len < sizeof(line) - 1) {
      line[line_len++] = c;
    } else {
      line_len = 0;  // overlong garbage, drop it
    }
  }
}

// ---------------------------------------------------------------- drawing
static void text_centered(const char *s, int cx, int cy, int size, uint16_t color) {
  int w = strlen(s) * 6 * size;
  int h = 8 * size;
  gfx->setTextSize(size);
  gfx->setTextColor(color);
  gfx->setCursor(cx - w / 2 + size / 2, cy - h / 2 + size / 2);
  gfx->print(s);
}

static void polar(float deg, float r, int *x, int *y) {
  float a = deg * DEG_TO_RAD;
  *x = CX + (int)lroundf(r * cosf(a));
  *y = CY + (int)lroundf(r * sinf(a));
}

static void draw_sensors(bool linked) {
  bool show = linked && st.state == ST_REC;
  for (int i = 0; i < N_SENSORS; i++) {
    int x, y;
    polar(SENSOR_ANGLE[i], SENSOR_ORBIT, &x, &y);
    bool live = show && st.hz[i] >= 0;
    uint16_t border = !live ? C_DKGREY : (st.ok[i] ? C_GREEN : C_RED);
    gfx->fillCircle(x, y, SENSOR_R, border);
    gfx->fillCircle(x, y, SENSOR_R - 3, C_PANEL);
    text_centered(SENSOR_NAME[i], x, y - 10, 1, live ? C_WHITE : C_GREY);
    char buf[8];
    if (live) snprintf(buf, sizeof(buf), "%d", st.hz[i]);
    else      snprintf(buf, sizeof(buf), "--");
    text_centered(buf, x, y + 5, 2, live ? (st.ok[i] ? C_WHITE : C_RED) : C_GREY);
  }
}

static void draw_disk(bool linked) {
  for (float a = PIPE_A0; a >= PIPE_A1; a -= 1.5f) {
    int x, y;
    polar(a, PIPE_ORBIT, &x, &y);
    gfx->fillCircle(x, y, 8, C_DKGREY);
  }
  if (!linked) return;
  uint16_t fill = st.disk_pct >= 90 ? C_RED : (st.disk_pct >= 70 ? C_AMBER : C_GREEN);
  float a_end = PIPE_A0 + (PIPE_A1 - PIPE_A0) * st.disk_pct / 100.0f;
  for (float a = PIPE_A0; a >= a_end; a -= 1.5f) {
    int x, y;
    polar(a, PIPE_ORBIT, &x, &y);
    gfx->fillCircle(x, y, 5, fill);
  }
  char buf[8];
  snprintf(buf, sizeof(buf), "%dG", st.free_gb);
  text_centered("FREE", CX + 80, CY - 6, 1, C_GREY);
  text_centered(buf, CX + 80, CY + 6, 1, C_WHITE);
}

static bool any_sensor_bad() {
  for (int i = 0; i < N_SENSORS; i++)
    if (st.hz[i] >= 0 && !st.ok[i]) return true;
  return false;
}

// A laptop is copying, checking or deleting sessions: show that instead of
// the recording state. Returns false when there is nothing to show.
static bool draw_export() {
  char buf[12];
  switch (ex.phase) {
    case EX_COPY:
    case EX_VERIFY:
      gfx->fillCircle(CX, CY, CENTER_R, C_BLUE);
      text_centered(ex.phase == EX_COPY ? "EXPORT" : "VERIFY", CX, CY - 30, 2, C_WHITE);
      snprintf(buf, sizeof(buf), "%d%%", ex.progress);
      text_centered(buf, CX, CY, 4, C_WHITE);
      snprintf(buf, sizeof(buf), "%d/%d", ex.current, ex.total);
      text_centered(buf, CX, CY + 32, 2, C_WHITE);
      return true;
    case EX_DELETE:
      gfx->fillCircle(CX, CY, CENTER_R, C_DKRED);
      text_centered("DELETE", CX, CY, 3, C_WHITE);
      return true;
    case EX_DONE:
      gfx->fillCircle(CX, CY, CENTER_R, C_DKGREEN);
      text_centered("DONE", CX, CY - 10, 3, C_WHITE);
      text_centered("UNPLUG OK", CX, CY + 20, 1, C_WHITE);
      return true;
    default:
      return false;
  }
}

static void draw_elapsed(int cy, uint16_t color) {
  char buf[12];
  int s = st.elapsed_s;
  if (s < 3600) {
    snprintf(buf, sizeof(buf), "%02d:%02d", s / 60, s % 60);
    text_centered(buf, CX, cy, 3, color);
  } else {
    snprintf(buf, sizeof(buf), "%d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60);
    text_centered(buf, CX, cy, 2, color);
  }
}

static void draw_center(bool linked, uint32_t now) {
  if (!linked) {
    gfx->fillCircle(CX, CY, CENTER_R, C_PANEL);
    text_centered("NO", CX, CY - 14, 3, C_AMBER);
    text_centered("LINK", CX, CY + 14, 3, C_AMBER);
    return;
  }
  if (st.state != ST_REC && st.state != ST_PAUSED && draw_export()) return;
  if (pending) {
    gfx->fillCircle(CX, CY, CENTER_R, C_DKGREY);
    text_centered("WAIT", CX, CY, 3, C_WHITE);
    return;
  }
  switch (st.state) {
    case ST_REC: {
      // Sensor dropout: alternate the normal view with a red "!".
      if (any_sensor_bad() && (now / 400) % 2 == 0) {
        gfx->fillCircle(CX, CY, CENTER_R, C_BG);
        gfx->drawCircle(CX, CY, CENTER_R, C_RED);
        text_centered("!", CX, CY, 9, C_RED);
        break;
      }
      gfx->fillCircle(CX, CY, CENTER_R, C_DKRED);
      text_centered("REC", CX, CY - 24, 3, C_WHITE);
      draw_elapsed(CY + 2, C_WHITE);
      text_centered("TAP x2 PAUSE", CX, CY + 30, 1, C_WHITE);
      text_centered("HOLD STOP", CX, CY + 42, 1, C_WHITE);
      break;
    }
    case ST_PAUSED:
      gfx->fillCircle(CX, CY, CENTER_R, C_DKAMBER);
      text_centered("PAUSED", CX, CY - 26, 2, C_WHITE);
      draw_elapsed(CY + 2, C_WHITE);
      text_centered("TAP x2 RESUME", CX, CY + 30, 1, C_WHITE);
      text_centered("HOLD STOP", CX, CY + 42, 1, C_WHITE);
      break;
    case ST_SAVING:
      gfx->fillCircle(CX, CY, CENTER_R, C_AMBER);
      text_centered("SAVING", CX, CY, 3, C_BG);
      break;
    case ST_ERROR:
      gfx->fillCircle(CX, CY, CENTER_R, C_BG);
      gfx->drawCircle(CX, CY, CENTER_R, C_RED);
      text_centered("ERROR", CX, CY, 3, C_RED);
      break;
    default:
      gfx->fillCircle(CX, CY, CENTER_R, C_DKGREY);
      if (clock_warning(now)) text_centered("CLOCK NOT SET", CX, CY - 38, 1, C_AMBER);
      text_centered("IDLE", CX, CY - 10, 3, C_WHITE);
      text_centered("HOLD TO REC", CX, CY + 20, 1, C_WHITE);
      if (ex.phase == EX_LINKED) text_centered("PC LINKED", CX, CY + 36, 1, C_GREEN);
      break;
  }
}

static void draw_hold_ring(uint32_t now) {
  if (!holding || hold_fired || now - hold_start_ms < RING_DELAY_MS) return;
  float frac = min(1.0f, (now - hold_start_ms) / (float)HOLD_MS);
  for (float a = 0; a <= 360.0f * frac; a += 2.0f) {
    int x, y;
    polar(a - 90, RING_R, &x, &y);   // start at 12 o'clock, clockwise
    gfx->fillCircle(x, y, 2, C_WHITE);
  }
}

// ---------------------------------------------------------------- input
static void send_request(const char *msg, uint32_t now) {
  Serial.print(msg);
  pending = true;
  pending_from_state = st.state;
  pending_ms = now;
}

static void handle_touch(bool linked, uint32_t now) {
  int x, y;
  bool down = touch_get(&x, &y);
  bool on_center = false;
  if (down) {
    long dx = x - CX, dy = y - CY;
    on_center = dx * dx + dy * dy <= (long)(CENTER_R + 8) * (CENTER_R + 8);
  }
  bool recording = st.state == ST_REC || st.state == ST_PAUSED;
  // No recording while a laptop is reading or deleting session files.
  bool exporting = ex.phase >= EX_COPY && !recording;
  bool actionable = linked && !pending && !exporting &&
                    (st.state == ST_IDLE || recording);

  if (tap_waiting && now - last_tap_ms > DOUBLE_TAP_MS) tap_waiting = false;

  if (!down || !on_center) {
    // A short press that ends with the finger lifted on the circle is a tap;
    // sliding off the circle cancels it.
    if (holding && !hold_fired && !down && now - hold_start_ms < TAP_MAX_MS &&
        actionable && recording) {
      if (tap_waiting) {
        tap_waiting = false;
        send_request(st.state == ST_REC ? "$E,PAUSE\n" : "$E,RESUME\n", now);
      } else {
        tap_waiting = true;
        last_tap_ms = now;
      }
    }
    holding = false;
    hold_fired = false;
    return;
  }
  if (!actionable) {
    // Cancel a hold in progress. The press stays consumed until the finger
    // lifts, so one long press can never send a second request.
    if (holding) hold_fired = true;
    return;
  }
  if (!holding) {
    holding = true;
    hold_fired = false;
    hold_start_ms = now;
    return;
  }
  if (!hold_fired && now - hold_start_ms >= HOLD_MS) {
    hold_fired = true;
    tap_waiting = false;
    send_request(recording ? "$E,STOP\n" : "$E,START\n", now);
  }
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);

  gfx->begin();
  ledcSetup(1, 5000, 8);
  ledcAttachPin(PIN_LCD_BL, 1);
  ledcWrite(1, 255);

  pinMode(PIN_TOUCH_RST, OUTPUT);
  pinMode(PIN_TOUCH_INT, INPUT_PULLUP);
  digitalWrite(PIN_TOUCH_RST, LOW);
  delay(10);
  digitalWrite(PIN_TOUCH_RST, HIGH);
  delay(50);
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
}

void loop() {
  // Read the clock after poll_serial(): it stamps last_status_ms, and a
  // stamp newer than `now` would underflow the unsigned age below.
  poll_serial();
  uint32_t now = millis();

  bool linked = ever_linked && (now - last_status_ms < LINK_TIMEOUT_MS);
  if (pending && (!linked || st.state != pending_from_state ||
                  now - pending_ms > PENDING_TIMEOUT_MS)) {
    pending = false;
  }

  if (!linked || now - last_export_ms > EXPORT_TIMEOUT_MS) ex.phase = EX_NONE;

  handle_touch(linked, now);

  gfx->fillScreen(C_BG);
  draw_sensors(linked);
  draw_disk(linked);
  draw_center(linked, now);
  draw_hold_ring(now);
  gfx->flush();
}
