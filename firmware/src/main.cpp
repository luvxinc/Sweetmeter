#include <Arduino.h>
#include <initializer_list>
#include "screen_orientation.h"

// ELECROW CrowPanel ESP32-S3 2.13(E), PCB V1.2, JD79661 panel.
// Register sequence and GC waveform: ELECROW example/arduino-v1.2.
constexpr int SCK_PIN = 12, MOSI_PIN = 11, RST = 10, DC = 13, CS = 14;
constexpr int BUSY_PIN = 9, POWER = 7, LED = 19;
constexpr size_t FRAME_SIZE = 4000;
uint8_t frame[FRAME_SIZE];
uint32_t lastCRC = 0;
bool hasFrame = false;

uint32_t crc32(const uint8_t *bytes, size_t size) {
  uint32_t crc = 0xffffffff;
  for (size_t i = 0; i < size; ++i) {
    crc ^= bytes[i];
    for (int bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return ~crc;
}

void writeByte(uint8_t value) {
  digitalWrite(CS, LOW);
  for (int bit = 0; bit < 8; ++bit) {
    digitalWrite(SCK_PIN, LOW);
    digitalWrite(MOSI_PIN, value & 0x80 ? HIGH : LOW);
    digitalWrite(SCK_PIN, HIGH);
    value <<= 1;
  }
  digitalWrite(CS, HIGH);
}
void command(uint8_t value) {
  digitalWrite(DC, LOW); writeByte(value); digitalWrite(DC, HIGH);
}
void data(uint8_t value) {
  digitalWrite(DC, HIGH); writeByte(value);
}
bool waitReady() {
  unsigned long start = millis();
  while (digitalRead(BUSY_PIN) == LOW) {
    if (millis() - start > 15000) return false;
    delay(1);
  }
  return true;
}

void loadWaveform(bool alternate) {
  const uint8_t waveform[] = {0x00, 0x60, 0x20, 0x10, 0x90};
  for (int table = 0; table < 5; ++table) {
    int target = table;
    if (alternate && table == 2) target = 3;
    if (alternate && table == 3) target = 2;
    command(0x20 + target);
    data(0x01); data(waveform[table]); data(0x14); data(0x14);
    data(0x01); data(0); data(0); data(0x01);
    for (int i = 8; i < 56; ++i) data(0);
  }
}

bool refreshPanel() {
  command(0x17); data(0xa5);
  unsigned long start = millis();
  while (digitalRead(BUSY_PIN) == HIGH && millis() - start < 1000) delay(1);
  if (digitalRead(BUSY_PIN) == HIGH) {
    Serial.println("ERR NO_BUSY_TRANSITION"); return false;
  }
  bool ready = waitReady();
  Serial.printf("TRACE refresh_ms=%lu ready=%d\n", millis() - start, ready);
  return ready;
}

bool panelReady = false;
uint8_t panelFrame[FRAME_SIZE];
unsigned long lastFullAt = 0;

// V1.2 DU trial produced optical ghosting on this unit. Use the previously
// confirmed full clear + draw waveform for all changes, including menu pages.
bool displayFrame(bool forceFull = false) {
  panelReady = false;
  digitalWrite(POWER, HIGH); delay(100);
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS}) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  digitalWrite(CS, HIGH);
  digitalWrite(RST, HIGH); delay(10);
  digitalWrite(RST, LOW); delay(100);
  digitalWrite(RST, HIGH); delay(100);
  if (!waitReady()) return false;
  command(0x00); data(0xf7); data(0x8a);
  command(0x01); data(0x03); data(0); data(0x3f); data(0x3f); data(0x03);
  command(0x03); data(0);
  command(0x06); data(0x27); data(0x27); data(0x2f);
  command(0x30); data(0x0d);
  command(0x60); data(0x22);
  command(0x82); data(0x07);
  command(0xe3); data(0x88);
  command(0x41); data(0);
  command(0x61); data(0x80); data(0); data(0xfa);
  command(0x65); data(0); data(0); data(0);
  command(0x50); data(0xb7);
  command(0x10);
  for (size_t i = 0; i < FRAME_SIZE; ++i) data(0xff);
  command(0x13);
  for (size_t i = 0; i < FRAME_SIZE; ++i) data(0xff);
  loadWaveform(false);
  if (!refreshPanel()) return false;
  command(0x50); data(0xd7);
  command(0x13);
  for (size_t i = 0; i < FRAME_SIZE; ++i) data(sweetmeter::rotatedPanelByte(frame, i));
  loadWaveform(true);
  if (!refreshPanel()) return false;
  panelReady = true; lastFullAt = millis();
  memcpy(panelFrame, frame, FRAME_SIZE);
  return true;
}

#include "bluetooth_meter.h"

extern "C" bool verifyRollbackLater() { return true; }

void setup() {
  setCpuFrequencyMhz(80);
  Serial.begin(115200);
  // EXT0 leaves its wake pad in RTC mode; restore digital button input on boot.
  rtc_gpio_deinit(GPIO_NUM_2);
  // The board switches LCD ground, so leave its signals floating while off.
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS}) pinMode(pin, INPUT);
  for (int pin : {POWER, LED}) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  gpio_deep_sleep_hold_dis();
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS, POWER, LED})
    gpio_hold_dis(gpio_num_t(pin));
  pinMode(BUSY_PIN, INPUT);
  const esp_sleep_wakeup_cause_t wake = esp_sleep_get_wakeup_cause();
  // A drifted RC clock after a long deep sleep shows --:-- until T arrives.
  if (!sweetmeter::clockStillValid(clockSynced, wake != ESP_SLEEP_WAKEUP_UNDEFINED, int64_t(time(nullptr)), sleptAt,
                                   sleptTotal))
    clockSynced = false;
  // Check an optional gauge before BLE and the panel: a critical cell wakes on
  // a 300 s timer only to measure again, not to advertise and redraw.
  Wire.begin(40, 41, 100000); Wire.setTimeOut(25); readBattery();
  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t imageState = ESP_OTA_IMG_UNDEFINED;
  bool candidate = running && esp_ota_get_state_partition(running, &imageState) == ESP_OK &&
                   imageState == ESP_OTA_IMG_PENDING_VERIFY;
  // A pending-verify update must finish its health checks first; sleeping here
  // would reset it unconfirmed and roll it back.
  if (!candidate && criticalBattery()) lowBatterySleep(wake == ESP_SLEEP_WAKEUP_EXT0);
  if (batteryPercent >= 0 && !criticalBattery()) lowBatteryLatched = lowBatteryShown = false;
  if (xTaskCreate(meterWorker, "sweetmeter", 16384, nullptr, 2, &workerTask) != pdPASS) {
    Serial.println("ERR WORKER_ALLOCATION"); esp_restart();
  }
}

void loop() {
  delay(1000);
}
