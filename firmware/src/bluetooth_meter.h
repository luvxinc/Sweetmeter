#pragma once
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <Wire.h>
#include <esp_sleep.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>
#include <Preferences.h>
#include <sys/time.h>
#include <time.h>
#include "screen_font.h"
#include "buttons.h"
#include "disconnect_sleep.h"

const char *SERVICE_UUID = "7a1e0001-ff1b-4d9f-a023-47c7752c1a01";
const char *CONTROL_UUID = "7a1e0002-ff1b-4d9f-a023-47c7752c1a01";
const char *DATA_UUID = "7a1e0003-ff1b-4d9f-a023-47c7752c1a01";
const char *STATUS_UUID = "7a1e0004-ff1b-4d9f-a023-47c7752c1a01";
BLECharacteristic *controlCharacteristic;
BLEServer *meterServer;
esp_bd_addr_t peerAddress = {};
Preferences preferences;
String selectedHost, selectedName, peerHost;
volatile bool authorized = false, hostDirty = false;
volatile bool menuOpen = false;
RTC_DATA_ATTR volatile int32_t timezoneOffset = 0;
RTC_DATA_ATTR volatile bool clockSynced = false;
volatile uint8_t keyEvents = 0;
uint8_t lastKey = 0;
unsigned long lastKeyAt = 0;
uint8_t dashboard[FRAME_SIZE];
bool uiDirty = true;
unsigned long refreshRequestedAt = 0;
struct Computer { String id, name; int rssi; };
Computer computers[8];
int computerCount = 0, selection = 0;
volatile bool scanDone = false;
bool scanning = false;
unsigned long menuAt = 0;
SemaphoreHandle_t computerMutex;
void drawScreen();
void startComputerScan();
volatile bool connected = false, restartAdvertising = false, pendingFrame = false;
volatile bool delivered = false;
volatile int batteryPercent = -1, batteryMillivolts = -1;
uint8_t incomingFrame[FRAME_SIZE];
size_t received = 0;
uint32_t incomingSequence = 0, incomingCRC = 0;
bool receiving = false;
unsigned long disconnectedAt = 0, advertisedAt = 0, packetAt = 0;
SemaphoreHandle_t frameMutex;
DisconnectSleep disconnectSleep;
bool sleepCommitted = false;
bool disconnectLogPending = false, reconnectLogPending = false;

uint32_t read32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
void put32(uint8_t *p, uint32_t value) {
  for (int i = 0; i < 4; ++i) p[i] = value >> (8 * i);
}
void bleReply(uint8_t result, uint32_t sequence, uint32_t checksum) {
  if (!connected) return;
  uint8_t message[10] = {'A', result};
  put32(message + 2, sequence); put32(message + 6, checksum);
  controlCharacteristic->setValue(message, sizeof(message));
  controlCharacteristic->notify();
}

// MAX17048 is optional: the stock board has no MCU-connected battery ADC.
bool gaugeRead(uint8_t reg, uint16_t &value) {
  Wire.beginTransmission(0x36); Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(0x36, 2) != 2) return false;
  value = uint16_t(Wire.read()) << 8; value |= Wire.read(); return true;
}
void readBattery() {
  Wire.beginTransmission(0x36);
  if (Wire.endTransmission() != 0) { batteryPercent = batteryMillivolts = -1; return; }
  uint16_t version, voltage, soc;
  if (!gaugeRead(0x08, version) || (version & 0xfff0) != 0x0010 ||
      !gaugeRead(0x02, voltage) || !gaugeRead(0x04, soc)) {
    batteryPercent = batteryMillivolts = -1; return;
  }
  int mv = lroundf(voltage * 0.078125f);
  if (mv < 2500 || mv > 4350 || soc > 105 * 256) {
    batteryPercent = batteryMillivolts = -1; return;
  }
  batteryMillivolts = mv;
  batteryPercent = min(100, int((soc + 128) / 256));
}
unsigned int refreshSeconds() {
  return batteryPercent >= 0 && batteryPercent <= 15 ? 300 : 60;
}
bool criticalBattery() {
  return batteryPercent >= 0 && (batteryPercent <= 5 || batteryMillivolts <= 3350);
}

class StatusCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    // Bluedroid invokes this on BTC_TASK's small stack. Keep the JSON buffer
    // in static storage; BLECharacteristic::setValue copies it before return.
    static char text[512];
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    snprintf(text, sizeof(text),
      "{\"protocol\":3,\"firmware\":\"QM3.2\",\"battery_percent\":%d,\"battery_mv\":%d,\"interval\":%u,\"critical\":%s,\"charge_state\":\"unknown\",\"wake_cause\":%d,\"uptime_ms\":%lu,\"selected_host\":\"%s\",\"clock_synced\":%s,\"menu\":%s,\"scanning\":%s,\"computers\":%d,\"last_key\":%u,\"key_at_ms\":%lu,\"epoch\":%lu,\"free_heap\":%u,\"reset_reason\":%d}",
      batteryPercent, batteryMillivolts, refreshSeconds(), criticalBattery() ? "true" : "false",
      int(esp_sleep_get_wakeup_cause()), millis(), selectedHost.c_str(), clockSynced ? "true" : "false", menuOpen ? "true" : "false",
      scanning ? "true" : "false", computerCount, lastKey, lastKeyAt,
      (unsigned long)time(nullptr), ESP.getFreeHeap(), int(esp_reset_reason()));
    xSemaphoreGive(frameMutex);
    characteristic->setValue(text);
  }
};

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override {
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    // A BLE connection alone is not proof that the selected Mac has returned.
    connected = true; authorized = false; delivered = false;
    xSemaphoreGive(frameMutex);
  }
  void onConnect(BLEServer *server, esp_ble_gatts_cb_param_t *parameters) override {
    memcpy(peerAddress, parameters->connect.remote_bda, sizeof(peerAddress));
    server->updateConnParams(peerAddress, 12, 24, 0, 600);
  }
  void onDisconnect(BLEServer *) override {
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    bool wasCounting = disconnectSleep.counting();
    disconnectSleep.targetDisconnected(millis());
    if (!wasCounting && disconnectSleep.counting()) disconnectLogPending = true;
    connected = false; authorized = false; restartAdvertising = true; disconnectedAt = millis();
    xSemaphoreGive(frameMutex);
  }
};

class ControlCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    std::string bytes = characteristic->getValue();
    const uint8_t *p = reinterpret_cast<const uint8_t *>(bytes.data());
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    if (sleepCommitted) { xSemaphoreGive(frameMutex); return; }
    if (bytes.size() >= 37 && p[0] == 'H') {
      bool wasAuthorized = authorized;
      authorized = false;
      String id = String(bytes.substr(1, 36).c_str()); id.toLowerCase();
      if (id.startsWith("7a1e1000-ff1b-4d9f-a023-") &&
          (selectedHost.isEmpty() || selectedHost == id)) {
        peerHost = id; authorized = true;
        if (selectedHost.isEmpty()) {
          selectedHost = id;
          selectedName = String(bytes.substr(37, 20).c_str()); hostDirty = true;
        }
        if (disconnectSleep.counting()) reconnectLogPending = true;
        disconnectSleep.targetConnected();
      } else if (wasAuthorized) {
        disconnectSleep.targetDisconnected(millis()); disconnectLogPending = true;
      }
      uint8_t hello[] = {'H', uint8_t(authorized ? 0 : 7)};
      controlCharacteristic->setValue(hello, sizeof(hello)); controlCharacteristic->notify();
    } else if (!authorized) {
      // A bonded computer must also match the user's selected companion identity.
    } else if (bytes.size() == 9 && p[0] == 'T') {
      timeval tv = {time_t(read32(p + 1)), 0}; settimeofday(&tv, nullptr);
      timezoneOffset = int32_t(read32(p + 5)); clockSynced = true;
    } else if (bytes.size() == 11 && p[0] == 'B') {
      meterServer->updateConnParams(peerAddress, 12, 24, 0, 600);
      if (!pendingFrame && (unsigned(p[9]) | unsigned(p[10]) << 8) == FRAME_SIZE) {
        incomingSequence = read32(p + 1); incomingCRC = read32(p + 5);
        received = 0; receiving = true; packetAt = millis();
      } else {
        bleReply(2, read32(p + 1), read32(p + 5));
      }
    } else if (bytes.size() == 5 && p[0] == 'C') {
      if (!receiving || pendingFrame || received != FRAME_SIZE ||
          read32(p + 1) != incomingSequence || crc32(incomingFrame, FRAME_SIZE) != incomingCRC) {
        receiving = false; bleReply(3, read32(p + 1), incomingCRC);
      } else {
        receiving = false; pendingFrame = true;
      }
    }
    xSemaphoreGive(frameMutex);
  }
};

class DataCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    std::string bytes = characteristic->getValue();
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    if (!sleepCommitted && authorized && receiving && !pendingFrame && bytes.size() > 2) {
      size_t offset = uint8_t(bytes[0]) | size_t(uint8_t(bytes[1])) << 8;
      size_t count = bytes.size() - 2;
      if (offset != received || received + count > FRAME_SIZE) {
        receiving = false; bleReply(4, incomingSequence, incomingCRC);
      } else {
        memcpy(incomingFrame + received, bytes.data() + 2, count);
        received += count; packetAt = millis();
      }
    }
    xSemaphoreGive(frameMutex);
  }
};

void setupBluetooth() {
  frameMutex = xSemaphoreCreateMutex();
  computerMutex = xSemaphoreCreateMutex();
  preferences.begin("quota-meter", false);
  selectedHost = preferences.getString("host", "");
  selectedName = preferences.getString("name", "");
  memset(dashboard, 0xff, FRAME_SIZE);
  Wire.begin(40, 41, 100000); Wire.setTimeOut(25); readBattery();
  BLEDevice::init("Sweetmeter");
  BLEDevice::setMTU(185);
  BLEDevice::setEncryptionLevel(ESP_BLE_SEC_ENCRYPT);
  BLESecurity *security = new BLESecurity();
  security->setAuthenticationMode(ESP_LE_AUTH_REQ_SC_BOND);
  security->setCapability(ESP_IO_CAP_NONE);
  security->setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
  security->setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
  BLEServer *server = BLEDevice::createServer(); meterServer = server;
  server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  controlCharacteristic = service->createCharacteristic(CONTROL_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY);
  controlCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED);
  controlCharacteristic->addDescriptor(new BLE2902());
  controlCharacteristic->setCallbacks(new ControlCallbacks());
  BLECharacteristic *dataCharacteristic = service->createCharacteristic(DATA_UUID, BLECharacteristic::PROPERTY_WRITE);
  dataCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED);
  dataCharacteristic->setCallbacks(new DataCallbacks());
  BLECharacteristic *statusCharacteristic = service->createCharacteristic(STATUS_UUID, BLECharacteristic::PROPERTY_READ);
  statusCharacteristic->setAccessPermissions(ESP_GATT_PERM_READ_ENCRYPTED);
  statusCharacteristic->setCallbacks(new StatusCallbacks());
  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->setMinInterval(800); advertising->setMaxInterval(1600);
  BLEDevice::startAdvertising(); advertisedAt = millis();
  Serial.printf("READY QM3.2 BLE Sweetmeter wake=%d reset=%d\n",
                int(esp_sleep_get_wakeup_cause()), int(esp_reset_reason()));
}

void powerOffPanel() {
  // Q9 disconnects LCD ground, not VDD. Avoid supplying a ground return via SPI.
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS}) pinMode(pin, INPUT);
  digitalWrite(POWER, LOW); digitalWrite(LED, LOW);
}

void enterSleep(unsigned int seconds) {
  if (scanning) BLEDevice::getScan()->stop();
  scanning = false;
  if (hostDirty) {
    preferences.putString("host", selectedHost);
    preferences.putString("name", selectedName); hostDirty = false;
  }
  if (connected) meterServer->disconnect(meterServer->getConnId());
  BLEDevice::deinit(true);
  powerOffPanel();
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  pinMode(2, INPUT_PULLUP);
  if (seconds == 0) {
    Serial.println("POWER WAIT_RELEASE");
    // EXT0 is level triggered. Require a stable release to avoid waking at once.
    unsigned long releasedAt = millis();
    while (millis() - releasedAt < 50) {
      if (digitalRead(2) == LOW) releasedAt = millis();
      delay(10);
    }
  } else {
    esp_sleep_enable_timer_wakeup(uint64_t(seconds) * 1000000);
  }
  if (digitalRead(2) == HIGH) {
    rtc_gpio_pullup_en(GPIO_NUM_2);
    rtc_gpio_pulldown_dis(GPIO_NUM_2);
    esp_sleep_enable_ext0_wakeup(GPIO_NUM_2, 0);
  }
  // Hold power off and keep screen signals at high impedance during deep sleep.
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS, POWER, LED})
    gpio_hold_en(gpio_num_t(pin));
  gpio_deep_sleep_hold_en();
  Serial.printf("POWER SLEEP timer=%u\n", seconds); Serial.flush();
  esp_deep_sleep_start();
}


void pixel(int x, int y, bool black) {
  if (x < 0 || x >= 250 || y < 0 || y >= 122) return;
  uint8_t &b = frame[x * 16 + y / 8]; uint8_t mask = 0x80 >> (y % 8);
  if (black) b &= ~mask; else b |= mask;
}
void box(int x, int y, int w, int h, bool black) {
  for (int xx = x; xx < x + w; ++xx) for (int yy = y; yy < y + h; ++yy) pixel(xx, yy, black);
}
void textAt(int x, int y, const String &text, bool black = true) {
  for (unsigned int i = 0; i < text.length() && x + 6 <= 250; ++i, x += 6) {
    unsigned char c = text[i]; if (c < 32 || c > 126) c = '?';
    for (int yy = 0; yy < 11; ++yy) for (int xx = 0; xx < 6; ++xx)
      if (screenFont[c - 32][yy] & (1 << (5 - xx))) pixel(x + xx, y + yy, black);
  }
}
void powerOff(bool linkLost = false) {
  memset(frame, 0xff, FRAME_SIZE);
  textAt(112, 31, "OFF");
  textAt(40, 55, linkLost ? "Bluetooth disconnected." : "Release the top button.");
  textAt(22, 74, "Press top again to wake up.");
  displayFrame();
  enterSleep(0);
}
void batteryIcon() {
  box(229, 2, 17, 9, false); box(230, 3, 15, 7, true); box(246, 4, 2, 5, false);
  if (batteryPercent < 0) { textAt(235, 1, "?", false); return; }
  int bars = batteryPercent == 0 ? 0 : (batteryPercent + 24) / 25;
  for (int i = 0; i < bars; ++i) box(231 + i * 3, 4, 2, 5, false);
}
void drawScreen() {
  if (menuOpen) {
    memset(frame, 0xff, FRAME_SIZE);
    box(0, 0, 250, 14, true); textAt(4, 1, "SELECT COMPUTER", false);
    textAt(4, 16, scanning ? "Searching..." : "Wheel: move / press: connect");
    xSemaphoreTake(computerMutex, portMAX_DELAY);
    int first = selection >= 4 ? selection - 3 : 0;
    for (int i = first; i < computerCount && i < first + 4; ++i) {
      int y = 32 + (i - first) * 17;
      bool chosen = i == selection;
      if (chosen) box(2, y, 246, 15, true);
      textAt(5, y + 2, computers[i].name.substring(0, 28), !chosen);
      textAt(206, y + 2, String(computers[i].rssi), !chosen);
    }
    if (!computerCount && !scanning) textAt(4, 42, "No companion found. Open app.");
    xSemaphoreGive(computerMutex);
    textAt(4, 107, "Top: rescan    Bottom: back");
  } else {
    memcpy(frame, dashboard, FRAME_SIZE);
    if (!hasFrame) textAt(4, 44, "Waiting for companion...");
    box(0, 0, 250, 13, true);
    textAt(3, 1, connected && authorized ? "BT" : "--", false);
    textAt(22, 1, "USED%", false);
    if (refreshRequestedAt && millis() - refreshRequestedAt < 10000) textAt(58, 1, "*", false);
    char stamp[24] = "----/--/-- --:--";
    if (clockSynced) {
      time_t now = time(nullptr) + timezoneOffset; tm local; gmtime_r(&now, &local);
      strftime(stamp, sizeof(stamp), "%Y/%m/%d %H:%M", &local);
    }
    textAt(124, 1, stamp, false); batteryIcon();
  }
  if (memcmp(frame, panelFrame, FRAME_SIZE) || !panelReady) displayFrame();
  uiDirty = false;
}
class ComputerCallbacks : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice device) override {
    for (int i = 0; i < device.getServiceUUIDCount(); ++i) {
      String id(device.getServiceUUID(i).toString().c_str());
      if (!id.startsWith("7a1e1000-ff1b-4d9f-a023-")) continue;
      String name(device.getName().c_str());
      if (name.isEmpty()) name = "Mac " + id.substring(28);
      xSemaphoreTake(computerMutex, portMAX_DELAY);
      int slot = 0; while (slot < computerCount && computers[slot].id != id) ++slot;
      if (slot < 8) {
        computers[slot] = {id, name, device.getRSSI()};
        if (slot == computerCount) ++computerCount;
      }
      xSemaphoreGive(computerMutex);
    }
  }
};
void scanComplete(BLEScanResults) { scanDone = true; }
void startComputerScan() {
  if (scanning) return;
  menuOpen = true; menuAt = millis(); selection = 0;
  xSemaphoreTake(computerMutex, portMAX_DELAY); computerCount = 0; xSemaphoreGive(computerMutex);
  scanning = true; scanDone = false; uiDirty = true;
  BLEScan *scan = BLEDevice::getScan();
  static ComputerCallbacks callbacks;
  scan->setAdvertisedDeviceCallbacks(&callbacks, true);
  scan->setActiveScan(true); scan->setInterval(160); scan->setWindow(80);
  scan->start(6, scanComplete, false);
  Serial.println("UI SCAN");
}
// Independent debounce task captures short presses even during a panel refresh.
void buttonTask(void *) {
  const int pins[] = {2, 1, 6, 4, 5};
  MeterButton buttons[] = {{1, 64}, {2, 32}, {4}, {8}, {16}};
  for (int pin : pins) pinMode(pin, INPUT_PULLUP);
  while (true) {
    for (int i = 0; i < 5; ++i) {
      uint8_t event = buttons[i].update(digitalRead(pins[i]) == LOW, millis());
      if (event) __atomic_fetch_or(&keyEvents, event, __ATOMIC_RELAXED);
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
void bluetoothLoop() {
  static bool buttonsStarted = false;
  if (!buttonsStarted) { xTaskCreate(buttonTask, "buttons", 2048, nullptr, 1, nullptr); buttonsStarted = true; }
  static unsigned long batteryAt = 0;
  static time_t paintedMinute = -1;
  if (hostDirty) { preferences.putString("host", selectedHost); preferences.putString("name", selectedName); hostDirty = false; }
  if (millis() - batteryAt >= 10000) { readBattery(); batteryAt = millis(); }
  if (criticalBattery()) {
    enterSleep(300);
  }
  xSemaphoreTake(frameMutex, portMAX_DELAY);
  uint32_t disconnectedMs = disconnectSleep.elapsed(millis());
  bool autoOff = disconnectSleep.expired(millis());
  if (autoOff) sleepCommitted = true;
  bool logLost = disconnectLogPending, logRecovered = reconnectLogPending;
  disconnectLogPending = reconnectLogPending = false;
  xSemaphoreGive(frameMutex);
  if (logLost) Serial.printf("POWER LINK_LOST grace_ms=%u elapsed_ms=%u\n",
                            unsigned(DisconnectSleep::graceMs), unsigned(disconnectedMs));
  if (logRecovered) Serial.println("POWER AUTO_OFF_CANCELLED target_reconnected");
  if (autoOff) {
    Serial.printf("POWER AUTO_OFF disconnected_ms=%u\n", unsigned(disconnectedMs));
    powerOff(true);
  }
  if (refreshRequestedAt && millis() - refreshRequestedAt > 10000) { refreshRequestedAt = 0; uiDirty = true; }
  uint8_t keys = __atomic_exchange_n(&keyEvents, uint8_t(0), __ATOMIC_RELAXED);
  if (keys) {
    lastKey = keys; lastKeyAt = millis();
    Serial.printf("UI KEYS %u\n", keys); menuAt = millis();
  }
  if (keys & 64) powerOff();
  if (keys & 32) startComputerScan();
  if (scanDone) { scanDone = false; scanning = false; uiDirty = true; Serial.printf("UI FOUND %d\n", computerCount); }
  if (menuOpen) {
    if (keys & 1) startComputerScan();
    if ((keys & 4) && computerCount) { selection = (selection + computerCount - 1) % computerCount; uiDirty = true; }
    if ((keys & 8) && computerCount) { selection = (selection + 1) % computerCount; uiDirty = true; }
    if ((keys & 16) && computerCount && !scanning) {
      xSemaphoreTake(computerMutex, portMAX_DELAY);
      String chosenHost = computers[selection].id, chosenName = computers[selection].name;
      xSemaphoreGive(computerMutex);
      xSemaphoreTake(frameMutex, portMAX_DELAY);
      selectedHost = chosenHost; selectedName = chosenName;
      bool wasCounting = disconnectSleep.counting();
      disconnectSleep.targetDisconnected(millis());
      if (!wasCounting && disconnectSleep.counting()) disconnectLogPending = true;
      authorized = false; receiving = false; pendingFrame = false;
      xSemaphoreGive(frameMutex);
      hostDirty = true; menuOpen = false; uiDirty = true;
      if (connected) meterServer->disconnect(meterServer->getConnId());
      Serial.printf("UI SELECT %s\n", selectedName.c_str());
    }
    if ((keys & 2) || millis() - menuAt > 60000) {
      if (scanning) BLEDevice::getScan()->stop();
      scanning = false; menuOpen = false; uiDirty = true;
    }
  } else if (keys & 1) {
    refreshRequestedAt = millis(); uiDirty = true;
    if (connected && authorized) {
      uint8_t request = 'R'; controlCharacteristic->setValue(&request, 1); controlCharacteristic->notify();
      Serial.println("UI REFRESH");
    } else { restartAdvertising = true; }
  }
  if (pendingFrame) {
    xSemaphoreTake(frameMutex, portMAX_DELAY);
    memcpy(dashboard, incomingFrame, FRAME_SIZE);
    uint32_t sequence = incomingSequence, checksum = incomingCRC;
    xSemaphoreGive(frameMutex);
    uint8_t result = criticalBattery() ? 6 : 0;
    if (!result) {
      hasFrame = true; lastCRC = checksum; delivered = true; refreshRequestedAt = 0;
      drawScreen(); if (!panelReady) result = 5;
    }
    bleReply(result, sequence, checksum);
    meterServer->updateConnParams(peerAddress, 48, 72, 4, 600);
    Serial.printf("BLE ACK %u %08x result=%u\n", sequence, checksum, result);
    xSemaphoreTake(frameMutex, portMAX_DELAY); pendingFrame = false; xSemaphoreGive(frameMutex);
  }
  if (restartAdvertising && !pendingFrame) {
    xSemaphoreTake(frameMutex, portMAX_DELAY); receiving = false; xSemaphoreGive(frameMutex);
    restartAdvertising = false; BLEDevice::startAdvertising(); advertisedAt = millis(); uiDirty = true;
  }
  time_t minute = time(nullptr) / 60;
  if (uiDirty || (!menuOpen && minute != paintedMinute)) {
    drawScreen(); paintedMinute = minute;
  }
  xSemaphoreTake(frameMutex, portMAX_DELAY);
  if (receiving && millis() - packetAt > 15000) receiving = false;
  xSemaphoreGive(frameMutex);
  delay(10);
}
