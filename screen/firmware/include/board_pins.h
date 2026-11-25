// Pin map of the "JXL-ESP32S3" 1.28" round touch board (EC Buying, Amazon
// B0DLNMG2KP). Chip: ESP32-S3, 16 MB flash, 8 MB octal PSRAM, native USB.
//
// No vendor documentation was available; these were read out of the factory
// firmware (screen/factory_backup/) by disassembly.
#pragma once

// GC9A01 240x240 LCD, write-only SPI
#define PIN_LCD_SCK   3
#define PIN_LCD_MOSI  10
#define PIN_LCD_CS    2
#define PIN_LCD_DC    18
#define PIN_LCD_RST   21
#define PIN_LCD_BL    42   // backlight, PWM

// Shared I2C bus: CST816D touch (0x15), QMI8658 IMU, PCF85063 RTC (0x51)
#define PIN_I2C_SDA   8
#define PIN_I2C_SCL   9
#define PIN_TOUCH_RST 0
#define PIN_TOUCH_INT 11
#define PIN_IMU_INT1  12
#define PIN_IMU_INT2  13
#define PIN_RTC_INT   45

// Side buttons, active low. Which of the three is UP / PW / DOWN is not yet
// verified on hardware.
#define PIN_BTN_A     14
#define PIN_BTN_B     15
#define PIN_BTN_C     16

#define PIN_CHARGE    17   // charger status input
#define PIN_BAT_ADC   1    // battery voltage through a 1/2 divider
#define PIN_WS2812    46   // 2 RGB LEDs

// TF card on its own SPI bus
#define PIN_SD_CS     40
#define PIN_SD_SCK    41
#define PIN_SD_MOSI   47
#define PIN_SD_MISO   48
