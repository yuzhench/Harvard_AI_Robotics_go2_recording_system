// Flash-path smoke test: print a counter over native USB serial, echo input.
#include <Arduino.h>

static uint32_t n = 0;

void setup() {
  Serial.begin(115200);
}

void loop() {
  Serial.printf("hello from JXL-ESP32S3 #%lu uptime_ms=%lu\n",
                (unsigned long)n++, (unsigned long)millis());
  while (Serial.available()) {
    Serial.printf("echo: %c\n", (char)Serial.read());
  }
  delay(1000);
}
