// Pin-map check: draw on the LCD, read the CST816 touch chip, log to serial.
#include <Arduino.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>
#include "board_pins.h"

static Arduino_DataBus *bus = new Arduino_ESP32SPI(
    PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_SCK, PIN_LCD_MOSI, GFX_NOT_DEFINED);
static Arduino_GFX *gfx = new Arduino_GC9A01(bus, PIN_LCD_RST, 0, true);

static const uint8_t TOUCH_ADDR = 0x15;
static uint8_t touch_id = 0;
static bool touch_found = false;

static bool touch_read(uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(TOUCH_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(TOUCH_ADDR, (uint8_t)n) != n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

void setup() {
  Serial.begin(115200);

  pinMode(PIN_BTN_A, INPUT);
  pinMode(PIN_BTN_B, INPUT_PULLUP);
  pinMode(PIN_BTN_C, INPUT_PULLUP);

  bool lcd_ok = gfx->begin();
  ledcSetup(1, 5000, 8);
  ledcAttachPin(PIN_LCD_BL, 1);
  ledcWrite(1, 200);

  gfx->fillScreen(BLACK);
  gfx->drawCircle(120, 120, 118, GREEN);
  gfx->drawCircle(120, 120, 117, GREEN);
  gfx->setTextColor(WHITE);
  gfx->setTextSize(3);
  gfx->setCursor(62, 70);
  gfx->print("PIN OK");

  pinMode(PIN_TOUCH_RST, OUTPUT);
  pinMode(PIN_TOUCH_INT, INPUT_PULLUP);
  digitalWrite(PIN_TOUCH_RST, LOW);
  delay(10);
  digitalWrite(PIN_TOUCH_RST, HIGH);
  delay(50);
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
  touch_found = touch_read(0xA7, &touch_id, 1);

  gfx->setTextSize(2);
  gfx->setCursor(48, 112);
  if (touch_found) gfx->printf("touch 0x%02X", touch_id);
  else             gfx->print("touch: none");
  gfx->setCursor(40, 140);
  gfx->print("tap anywhere");

  Serial.printf("lcd_begin=%d touch_found=%d touch_id=0x%02X\n",
                lcd_ok, touch_found, touch_id);
}

void loop() {
  static uint32_t last_report = 0;
  static uint32_t touches = 0;
  static int last_btn = -1;

  uint8_t d[6];
  if (touch_read(0x01, d, 6) && d[1] > 0) {
    int x = ((d[2] & 0x0F) << 8) | d[3];
    int y = ((d[4] & 0x0F) << 8) | d[5];
    gfx->fillCircle(x, y, 4, RED);
    touches++;
    Serial.printf("touch x=%d y=%d\n", x, y);
  }

  int btn = (digitalRead(PIN_BTN_A) << 2) | (digitalRead(PIN_BTN_B) << 1) |
            digitalRead(PIN_BTN_C);
  if (btn != last_btn) {
    last_btn = btn;
    Serial.printf("buttons gpio14=%d gpio15=%d gpio16=%d\n",
                  (btn >> 2) & 1, (btn >> 1) & 1, btn & 1);
  }

  if (millis() - last_report > 2000) {
    last_report = millis();
    Serial.printf("alive touches=%lu\n", (unsigned long)touches);
  }
  delay(20);
}
