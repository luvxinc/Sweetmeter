#pragma once
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <Wire.h>
#include <esp_sleep.h>
#include <esp_random.h>
#include <esp_gap_ble_api.h>
#include <esp_gatts_api.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>
#include <Preferences.h>
#include <sys/time.h>
#include <time.h>
#include <freertos/queue.h>
#include "screen_font.h"
#include "buttons.h"
#include "disconnect_sleep.h"
#include "discovery.h"
#include "ota_runtime.h"
#include "pairing.h"
#include "update_notice.h"
#include "power_policy.h"
#include "bond_policy.h"
#include "display_policy.h"
#include "advertising.h"
#include "status_json.h"

const char *SERVICE_UUID="7a1e0001-ff1b-4d9f-a023-47c7752c1a01";
const char *CONTROL_UUID="7a1e0002-ff1b-4d9f-a023-47c7752c1a01";
const char *DATA_UUID="7a1e0003-ff1b-4d9f-a023-47c7752c1a01";
const char *STATUS_UUID="7a1e0004-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_CONTROL_UUID="7a1e0005-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_DATA_UUID="7a1e0006-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_STATUS_UUID="7a1e0007-ff1b-4d9f-a023-47c7752c1a01";
BLECharacteristic *controlCharacteristic=nullptr,*otaStatusCharacteristic=nullptr;
BLE2902 *controlCccd=nullptr,*otaStatusCccd=nullptr;
BLEServer *meterServer=nullptr;
bool bleStarted=false;
char advertisedName[24]{};
TaskHandle_t workerTask=nullptr;
// Written by BTC callbacks, read by the worker: guarded by snapshotMux.
esp_bd_addr_t peerAddress={};
// Bonds of the current link (see bond_policy.h) and those of ended links that
// the worker must remove before advertising again. Guarded by snapshotMux.
sweetmeter::LinkBonds linkBonds;
uint8_t bondRemovals[sweetmeter::bondListCapacity][6]{};
size_t bondRemovalCount=0;
uint8_t linkChallenge[sweetmeter::challengeSize]{};
char linkChallengeHex[2*sweetmeter::challengeSize+1]="00000000000000000000000000000000";
Preferences preferences;
bool nvsReady=false;
sweetmeter::Registry registry;
sweetmeter::LinkAuth linkAuth;
sweetmeter::LegacyWindow legacyWindow;
sweetmeter::NoticeState notices;
sweetmeter::RefreshRequest refreshRequest;
sweetmeter::MinuteRedraw minuteRedraw;
sweetmeter::IdlePowerOff idlePower;
sweetmeter::RetryBackoff registryRetry;
bool registryDirty=false;
char deviceSerial[sweetmeter::serialSize+1]{};
volatile bool connected=false;
volatile int signalRssi=127;
volatile uint32_t signalReadAt=0;
volatile uint32_t linkGeneration=0,connectionAt=0,firstProbeAt=0;
volatile esp_gatt_if_t gattInterface=ESP_GATT_IF_NONE;
volatile bool bleServiceStarted=false;
volatile uint32_t droppedRejects=0;
RTC_DATA_ATTR int32_t timezoneOffset=0;
RTC_DATA_ATTR bool clockSynced=false;
RTC_DATA_ATTR int64_t sleptAt=0,sleptTotal=0;  // see clockStillValid()
RTC_DATA_ATTR bool lowBatteryLatched=false,lowBatteryShown=false;
volatile uint8_t keyEvents=0;
uint8_t dashboard[FRAME_SIZE],incomingFrame[FRAME_SIZE];
size_t received=0;
uint32_t incomingSequence=0,incomingCRC=0,packetAt=0;
uint32_t seenGeneration=0,discoveryReleaseAt=0,droppedGeneration=0,clockGeneration=0,menuClosedAt=0;
bool menuEverClosed=false;
uint8_t linkPeer[6]{};
bool receiving=false,pendingFrame=false,uiDirty=true,sleepCommitted=false,serviceChangeSent=false;
int removeConfirm=-1,keyConfirm=-1;
int batteryPercent=-1,batteryMillivolts=-1;
DisconnectSleep disconnectSleep;
sweetmeter::Discovery discovery;
sweetmeter::BootHealth bootHealth;
portMUX_TYPE snapshotMux=portMUX_INITIALIZER_UNLOCKED;
char deviceSnapshot[sweetmeter::statusLimit]{};
size_t snapshotChallengeOffset=0;
uint8_t otaSnapshot[20]{};
QueueHandle_t packetQueue=nullptr,rejectQueue=nullptr;
struct HostPacket { uint32_t generation; uint16_t size; uint8_t kind; bool invalid; uint8_t bytes[182]; };
enum PacketKind : uint8_t { DashboardControl, DashboardData, OtaControl, OtaData };
constexpr uint8_t helloBusy=2;
void drawScreen();
uint32_t read32(const uint8_t *p) { return sweetmeter::u32(p); }
void put32(uint8_t *p,uint32_t value) { sweetmeter::put32(p,value); }
void wakeWorker() { if(workerTask) xTaskNotifyGive(workerTask); }

// All notifications are sent from the worker task only, from a private
// buffer. Arduino's BLECharacteristic::notify() re-reads the characteristic's
// std::string value without a lock while BTC_TASK may be replacing it (for
// example on the next host write to the same characteristic), so it is not used.
bool notifyValue(BLECharacteristic *characteristic,BLE2902 *cccd,const uint8_t *bytes,size_t size) {
  if(!connected || !characteristic || gattInterface==ESP_GATT_IF_NONE || size>20) return false;
  if(cccd && !cccd->getNotifications()) return false;
  uint8_t copy[20]; memcpy(copy,bytes,size);
  return esp_ble_gatts_send_indicate(gattInterface,meterServer->getConnId(),characteristic->getHandle(),size,copy,false)==ESP_OK;
}
void notifyControl(const uint8_t *bytes,size_t size) { notifyValue(controlCharacteristic,controlCccd,bytes,size); }
void bleReply(uint8_t result,uint32_t sequence,uint32_t checksum) {
  uint8_t message[10]={'A',result}; put32(message+2,sequence); put32(message+6,checksum); notifyControl(message,sizeof(message));
}
void frameBeginReply(uint8_t result,uint32_t sequence,uint32_t checksum) {
  uint8_t message[10]={'b',result}; put32(message+2,sequence); put32(message+6,checksum); notifyControl(message,sizeof(message));
}
void registrationReply(uint8_t error,uint32_t session,uint32_t offset) {
  uint8_t out[10]={'J',error}; put32(out+2,session); put32(out+6,offset); notifyControl(out,sizeof(out));
}
void publishOta(const sweetmeter::OtaStatus &state,bool updateSnapshot) {
  uint8_t bytes[20]; state.encode(bytes);
  if(updateSnapshot) { portENTER_CRITICAL(&snapshotMux); memcpy(otaSnapshot,bytes,20); portEXIT_CRITICAL(&snapshotMux); }
  notifyValue(otaStatusCharacteristic,otaStatusCccd,bytes,20);
}
bool isAuthorized() { return linkAuth.authorized(); }
sweetmeter::OtaInputs otaInputs() {
  sweetmeter::OtaInputs value; value.authorized=isAuthorized(); value.connected=connected;
  value.menu=discovery.open; value.frameBusy=receiving||pendingFrame;
  value.critical=sweetmeter::batteryCritical(batteryPercent,batteryMillivolts,lowBatteryLatched);
  value.battery=batteryPercent; value.generation=linkGeneration; return value;
}
bool cancelWaiting(uint32_t session) {
  if(__atomic_load_n(&keyEvents,__ATOMIC_RELAXED)&64) return true;
  HostPacket next;
  if(packetQueue && xQueuePeek(packetQueue,&next,0)==pdTRUE && next.generation==linkGeneration &&
     next.kind==OtaControl && next.size==5 && next.bytes[0]=='X' && read32(next.bytes+1)==session) {
    xQueueReceive(packetQueue,&next,0); return true;
  }
  return false;
}
sweetmeter::OtaManager ota(bootHealth,otaInputs,publishOta,cancelWaiting);
sweetmeter::OtaRadioPolicy otaRadio;
void copyPeer(uint8_t *out) { portENTER_CRITICAL(&snapshotMux); memcpy(out,peerAddress,6); portEXIT_CRITICAL(&snapshotMux); }
void updateOtaRadio() {
  sweetmeter::RadioChange change=otaRadio.update(ota.active(),connected);
  esp_bd_addr_t peer; copyPeer(peer);
  if(change==sweetmeter::RadioChange::Fast) meterServer->updateConnParams(peer,12,24,0,600);
  if(change==sweetmeter::RadioChange::Idle) meterServer->updateConnParams(peer,48,72,4,600);
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
  return sweetmeter::batteryCritical(batteryPercent,batteryMillivolts,lowBatteryLatched);
}

// Callbacks only copy/queue bounded work and wake the worker. A packet that
// cannot be queued is recorded for a worker-side BUSY reply (never notified here).
void rejectPacket(uint8_t kind,const uint8_t *p,size_t n,bool busy) {
  using namespace sweetmeter;
  if(kind==OtaControl || kind==OtaData) {
    OtaStatus rejected;
    portENTER_CRITICAL(&snapshotMux); uint8_t prior[20]; memcpy(prior,otaSnapshot,20); portEXIT_CRITICAL(&snapshotMux);
    rejected.state=OtaState(prior[2]); rejected.offset=read32(prior+8); rejected.total=read32(prior+12); rejected.signature=prior[17]&2;
    rejected.session=kind==OtaData?(n>=4?read32(p):0):(n>=5?read32(p+1):0);
    rejected.opcode=kind==OtaData?'d':(n?p[0]:0); rejected.error=busy?OtaError::Busy:OtaError::Malformed;
    publishOta(rejected,false); return;
  }
  if(kind==DashboardControl && n && (p[0]=='J'||p[0]=='j'||p[0]=='K')) {
    registrationReply(busy?RegBusy:RegMalformed,n>=5?read32(p+1):0,0); return;
  }
  if(kind==DashboardControl && n && (p[0]=='H'||p[0]=='P'||p[0]=='Y')) {
    uint8_t answer[2]={uint8_t(p[0]=='Y'?'Y':'H'),uint8_t(busy?helloBusy:helloRejected)}; notifyControl(answer,2); return;
  }
  if(kind==DashboardControl && n && p[0]=='B') frameBeginReply(2,n>=5?read32(p+1):0,n>=9?read32(p+5):0);
  else if(kind==DashboardControl && n>=5 && p[0]=='C') bleReply(3,read32(p+1),0);
  else if(kind==DashboardData) bleReply(busy?2:4,0,0);
}
class PacketCallbacks : public BLECharacteristicCallbacks {
  uint8_t kind_;
 public:
  explicit PacketCallbacks(uint8_t kind):kind_(kind) {}
  void onWrite(BLECharacteristic *characteristic) override {
    std::string value=characteristic->getValue();
    const uint8_t *p=reinterpret_cast<const uint8_t*>(value.data()); size_t n=value.size();
    uint16_t mtu=meterServer?meterServer->getPeerMTU(meterServer->getConnId()):23;
    size_t budget=mtu>=23?min(size_t(182),size_t(mtu-3)):20;
    // Legacy H (37..57) and Y (33) may arrive through a GATT long write.
    bool longWrite=kind_==DashboardControl && n && ((n>=37 && n<=57 && p[0]=='H') || (n==33 && p[0]=='Y'));
    HostPacket item{}; item.generation=linkGeneration; item.kind=kind_; item.size=min(n,size_t(182));
    item.invalid=n>182 || (!longWrite && n>budget);
    if(item.size) memcpy(item.bytes,p,item.size);
    if(!packetQueue || xQueueSend(packetQueue,&item,0)!=pdTRUE) {
      if(!rejectQueue || xQueueSend(rejectQueue,&item,0)!=pdTRUE) __atomic_add_fetch(&droppedRejects,1,__ATOMIC_RELAXED);
    }
    if(kind_==DashboardControl && n && (p[0]=='J'||p[0]=='j'||p[0]=='K')) {
      uint32_t expected=0; __atomic_compare_exchange_n(&firstProbeAt,&expected,millis(),false,__ATOMIC_RELAXED,__ATOMIC_RELAXED);
    }
    wakeWorker();
  }
};
class StatusCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    static char copy[sweetmeter::statusLimit];
    // The snapshot is prepared by the worker; only this link's challenge is
    // patched in here so a read immediately after connecting is never stale.
    portENTER_CRITICAL(&snapshotMux);
    memcpy(copy,deviceSnapshot,sizeof(copy));
    if(snapshotChallengeOffset) memcpy(copy+snapshotChallengeOffset,linkChallengeHex,2*sweetmeter::challengeSize);
    portEXIT_CRITICAL(&snapshotMux);
    characteristic->setValue(copy);
    uint32_t expected=0; __atomic_compare_exchange_n(&firstProbeAt,&expected,millis(),false,__ATOMIC_RELAXED,__ATOMIC_RELAXED);
    wakeWorker();
  }
};
class OtaStatusCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t copy[20]; portENTER_CRITICAL(&snapshotMux); memcpy(copy,otaSnapshot,20); portEXIT_CRITICAL(&snapshotMux);
    characteristic->setValue(copy,20);
  }
};
// Called from the Bluetooth callback task only (it owns the static buffer).
void readBonds(sweetmeter::BondList &out) {
  static esp_ble_bond_dev_t list[CONFIG_BT_SMP_MAX_BONDS];
  out=sweetmeter::BondList{};
  int count=esp_ble_get_bond_device_num();
  if(count<0 || count>CONFIG_BT_SMP_MAX_BONDS || size_t(count)>sweetmeter::bondListCapacity) return;
  if(count && (esp_ble_get_bond_device_list(&count,list)!=ESP_OK || count<0 || count>CONFIG_BT_SMP_MAX_BONDS)) return;
  for(int i=0;i<count;++i) memcpy(out.addresses[i],list[i].bd_addr,6);
  out.count=uint8_t(count); out.valid=true;
}
class ServerCallbacks : public BLEServerCallbacks {
  // Arduino calls onConnect(server) and then onConnect(server,param). All work
  // happens in the second so the peer address is stored before the worker can
  // observe the new link generation.
  void onConnect(BLEServer *server,esp_ble_gatts_cb_param_t *parameters) override {
    // A fresh challenge for every physical connection; one hello attempt each.
    uint8_t fresh[sweetmeter::challengeSize]; char hex[2*sweetmeter::challengeSize+1];
    esp_fill_random(fresh,sizeof(fresh)); sweetmeter::hexEncode(fresh,sizeof(fresh),hex);
    // Bonds that exist before this link; SMP for this link completes later in
    // this same task, so the snapshot cannot already contain its bond.
    static sweetmeter::BondList bonds; readBonds(bonds);
    portENTER_CRITICAL(&snapshotMux);
    memcpy(peerAddress,parameters->connect.remote_bda,sizeof(peerAddress));
    memcpy(linkChallenge,fresh,sizeof(fresh)); memcpy(linkChallengeHex,hex,sizeof(hex));
    linkBonds.connected(linkGeneration+1,bonds);
    portEXIT_CRITICAL(&snapshotMux);
    signalRssi=127; signalReadAt=0;
    connected=true; connectionAt=millis(); firstProbeAt=0; __atomic_add_fetch(&linkGeneration,1,__ATOMIC_RELEASE);
    server->updateConnParams(parameters->connect.remote_bda,12,24,0,600);
    wakeWorker();
  }
  void onDisconnect(BLEServer *) override {
    // A link that did not earn its bonds loses the ones it created; the
    // worker removes them before advertising again (see bond_policy.h).
    static sweetmeter::BondList bonds; readBonds(bonds);
    static uint8_t added[sweetmeter::bondListCapacity][6];
    portENTER_CRITICAL(&snapshotMux);
    size_t count=linkBonds.ended(linkGeneration,bonds,added);
    for(size_t i=0;i<count && bondRemovalCount<sweetmeter::bondListCapacity;++i) memcpy(bondRemovals[bondRemovalCount++],added[i],6);
    portEXIT_CRITICAL(&snapshotMux);
    signalRssi=127; signalReadAt=0;
    connected=false; __atomic_add_fetch(&linkGeneration,1,__ATOMIC_RELEASE);
    wakeWorker();
  }
};
void updateDeviceSnapshot() {
  sweetmeter::StatusFields f;
  f.firmware=SWEETMETER_VERSION; f.board=sweetmeter::boardId; f.serial=deviceSerial;
  f.selected=registry.selected>=0; f.secured=f.selected && registry.current()->hasSecret;
  f.battery=batteryPercent; f.millivolts=batteryMillivolts; f.interval=refreshSeconds(); f.critical=criticalBattery();
  f.clockSynced=clockSynced; f.menu=discovery.open; f.nonce=discovery.open?discovery.nonce:0;
  f.remaining=discovery.remaining(millis()); f.computers=discovery.open?discovery.count:0; f.ota=ota.active();
  f.health=bootHealth.health; f.lastUpdate=bootHealth.lastUpdate; f.target=bootHealth.target;
  f.rssi=connected&&isAuthorized()&&signalReadAt&&millis()-signalReadAt<30000?signalRssi:127;
  char next[sweetmeter::statusLimit]; size_t offset=0;
  int length=sweetmeter::formatStatus(next,sizeof(next),f,offset);
  if(length<0) { Serial.println("ERR STATUS_OVERFLOW"); return; }  // never expose truncated JSON
  portENTER_CRITICAL(&snapshotMux); memcpy(deviceSnapshot,next,length+1); snapshotChallengeOffset=offset; portEXIT_CRITICAL(&snapshotMux);
}

// Pairing registry persistence. The "pairs" blob is authoritative; the legacy
// `host`/`name` keys mirror the selected computer for downgrade tooling only.
bool writeRegistry() {
  if(!nvsReady) return false;
  sweetmeter::RegistryBlob blob=registry.encode(), check;
  if(preferences.putBytes("pairs",&blob,sizeof(blob))!=sizeof(blob) ||
     preferences.getBytes("pairs",&check,sizeof(check))!=sizeof(check) || memcmp(&blob,&check,sizeof(blob))) return false;
  const sweetmeter::PairedComputer *current=registry.current();
  if(current) { preferences.putString("host",current->host); preferences.putString("name",current->name); }
  else { if(preferences.isKey("host")) preferences.remove("host"); if(preferences.isKey("name")) preferences.remove("name"); }
  return true;
}
void saveRegistrySoon() { registryDirty=true; registryRetry.succeeded(); }
void persistPending(uint32_t now) {
  if(registryDirty && registryRetry.due(now)) {
    if(writeRegistry()) { registryDirty=false; registryRetry.succeeded(); }
    else { registryRetry.failed(now); Serial.printf("ERR PAIRING_NVS retry_ms=%lu\n",(unsigned long)registryRetry.delay()); }
  }
}
void loadRegistry() {
  sweetmeter::RegistryBlob blob;
  if(nvsReady && preferences.getBytesLength("pairs")==sizeof(blob) &&
     preferences.getBytes("pairs",&blob,sizeof(blob))==sizeof(blob) && registry.decode(blob)) return;
  if(nvsReady && preferences.isKey("pairs")) {
    // Fail closed: never fall back to trusting a bare host ID.
    Serial.println("ERR PAIRING_CORRUPT"); registry=sweetmeter::Registry{}; return;
  }
  String host=nvsReady?preferences.getString("host",""):String(""), name=nvsReady?preferences.getString("name",""):String("");
  registry.migrateLegacy(host.c_str(),host.length(),name.c_str());
  if(registry.count) Serial.println("PAIRING LEGACY_SELECTION awaiting secret");
}
// This link earned its bonds (see bond_policy.h).
void earnBond() {
  portENTER_CRITICAL(&snapshotMux); linkBonds.earned(seenGeneration); portEXIT_CRITICAL(&snapshotMux);
}
// Remove bonds created by links that ended without earning them. Runs in the
// worker, always before advertising again, and never touches other bonds.
void removeUnearnedBonds() {
  uint8_t pending[sweetmeter::bondListCapacity][6]; size_t count;
  portENTER_CRITICAL(&snapshotMux);
  count=bondRemovalCount; memcpy(pending,bondRemovals,count*6); bondRemovalCount=0;
  portEXIT_CRITICAL(&snapshotMux);
  for(size_t i=0;i<count;++i) esp_ble_remove_bond_device(pending[i]);
  if(count) Serial.printf("BLE BONDS removed_unearned=%u\n",unsigned(count));
}
void advertise() { removeUnearnedBonds(); BLEDevice::startAdvertising(); }
void powerOffPanel() {
  // Q9 disconnects LCD ground, not VDD. Avoid supplying a ground return via SPI.
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS}) pinMode(pin, INPUT);
  digitalWrite(POWER, LOW); digitalWrite(LED, LOW);
}

void enterSleep(unsigned int seconds) {
  if (registryDirty && writeRegistry()) registryDirty = false;
  if (bleStarted) {
    if (connected) meterServer->disconnect(meterServer->getConnId());
    BLEDevice::deinit(true);
  }
  // Deep sleep keeps time on the drifting RC clock; see clockStillValid().
  sleptAt = int64_t(time(nullptr));
  powerOffPanel();
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  pinMode(2, INPUT_PULLUP);
  bool armTop = true;
  if (seconds == 0) {
    Serial.println("POWER WAIT_RELEASE");
    // EXT0 is level triggered. Require a stable release to avoid waking at once.
    unsigned long releasedAt = millis();
    while (millis() - releasedAt < 50) {
      if (digitalRead(2) == LOW) releasedAt = millis();
      delay(10);
    }
    // Always arm the button after the release wait: a press that lands after
    // it simply wakes the meter again instead of leaving no wake source.
  } else {
    esp_sleep_enable_timer_wakeup(uint64_t(seconds) * 1000000);
    // A held key at timed sleep would wake at once; the timer still wakes it.
    armTop = digitalRead(2) == HIGH;
  }
  if (armTop) {
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
void centered(int y, const char *text) { textAt(125 - int(strlen(text)) * 3, y, text); }
void powerOff(const char *reason) {
  memset(frame, 0xff, FRAME_SIZE);
  textAt(116, 31, "OFF");
  centered(55, reason);
  centered(74, "Press top again to wake up.");
  displayFrame();
  enterSleep(0);
}
// Shown once per discharge (RTC flag) or when the owner presses the top key;
// later timer wakes check the gauge without starting BLE or the panel.
void lowBatterySleep(bool userWake) {
  if (!lowBatteryShown || userWake) {
    memset(frame, 0xff, FRAME_SIZE);
    textAt(89, 31, "BATTERY LOW");
    centered(55, "Charge the meter.");
    centered(74, "It restarts when charged.");
    displayFrame(); lowBatteryShown = true;
  }
  lowBatteryLatched = true;
  Serial.printf("POWER LOW_BATTERY percent=%d mv=%d\n", batteryPercent, batteryMillivolts);
  enterSleep(300);
}
void batteryIcon() {
  box(229, 2, 17, 9, false); box(230, 3, 15, 7, true); box(246, 4, 2, 5, false);
  if (batteryPercent < 0) { textAt(235, 1, "?", false); return; }
  int bars = batteryPercent == 0 ? 0 : (batteryPercent + 24) / 25;
  for (int i = 0; i < bars; ++i) box(231 + i * 3, 4, 2, 5, false);
}
void drawMenu() {
  memset(frame,0xff,FRAME_SIZE); box(0,0,250,14,true);
  if(removeConfirm>=0 && unsigned(removeConfirm)<discovery.count) {
    const sweetmeter::Computer &c=discovery.computers[removeConfirm];
    textAt(4,1,"REMOVE COMPUTER",false);
    textAt(4,30,c.name); textAt(220,30,c.id+32);
    textAt(4,55,c.paired>=0?"It must pair again to be used.":"It is only removed from this list.");
    textAt(4,80,"Press rocker: remove");
    textAt(4,98,"Bottom: keep");
    return;
  }
  if(keyConfirm>=0 && unsigned(keyConfirm)<discovery.count) {
    // A paired computer sent a different pairing secret. Legitimate after it
    // forgot this meter; otherwise someone is imitating it.
    const sweetmeter::Computer &c=discovery.computers[keyConfirm];
    textAt(4,1,"NEW KEY",false);
    textAt(4,20,c.name); textAt(220,20,c.id+32);
    textAt(4,40,"This computer sent a new key.");
    textAt(4,56,"Accept only if you reset it.");
    textAt(4,80,"Press rocker: accept new key");
    textAt(4,98,"Bottom: cancel");
    return;
  }
  textAt(4,1,"SELECT COMPUTER",false);
  const sweetmeter::Computer *highlighted=discovery.count?&discovery.computers[discovery.selection]:nullptr;
  if(highlighted && highlighted->outdated) textAt(4,16,String("Update Sweetmeter on ")+highlighted->name);
  else textAt(4,16,discovery.count?"Press: use     Hold 3s: remove":"Open Sweetmeter on your computer.");
  unsigned first=discovery.selection>=4?discovery.selection-3:0;
  for(unsigned i=first;i<discovery.count && i<first+4;++i) {
    const sweetmeter::Computer &c=discovery.computers[i];
    int y=32+(i-first)*17; bool chosen=i==discovery.selection;
    if(chosen) box(2,y,246,15,true);
    // '>' current computer, '+' new (not yet paired), '!' needs attention:
    // conflicting identity, a changed key, or an app that must be updated.
    bool attention=c.conflict||c.keyChanged||c.outdated;
    const char *mark=attention?"!":c.paired<0?"+":c.paired==registry.selected?">":"";
    const char *label=c.conflict?"CONFLICT":c.outdated?"UPDATE APP":c.keyChanged?"NEW KEY":"";
    textAt(5,y+2,mark,!chosen); textAt(14,y+2,c.name,!chosen);
    textAt(216-6*int(strlen(label)),y+2,label,!chosen); textAt(220,y+2,c.id+32,!chosen);
  }
  if(!discovery.count) textAt(4,43,"Waiting for computers...");
  textAt(4,107,"Top: rescan    Bottom: back");
}
void drawScreen() {
  using sweetmeter::OtaState;
  if(ota.active()) {
    memset(frame,0xff,FRAME_SIZE); box(0,0,250,14,true); textAt(4,1,"FIRMWARE UPDATE",false);
    textAt(4,23,"Keep USB power connected.");
    const auto &s=ota.status;
    String stage=s.state==OtaState::Metadata?"Reading update...":s.state==OtaState::Preparing?"Preparing...":
      s.state==OtaState::Image?"Installing...":s.state==OtaState::Verifying?"Verifying...":"Restarting...";
    textAt(4,44,stage);
    unsigned percent=s.total?uint64_t(s.offset)*100/s.total:0;
    box(4,65,242,17,true); box(6,67,238,13,false);
    if(s.state>=OtaState::Image) box(6,67,238*percent/100,13,true);
    textAt(4,98,s.state>=OtaState::Image?String(percent)+"%":"Please wait");
  } else if(discovery.open) {
    drawMenu();
  } else {
    memcpy(frame,dashboard,FRAME_SIZE);
    if(!hasFrame) {
      textAt(4,37,"Open Sweetmeter on your computer.");
      textAt(4,55,"Hold bottom 3s to select it.");
      textAt(4,77,"github.com/luvxinc/Sweetmeter");
    }
    box(0,0,250,13,true); textAt(3,1,"v " SWEETMETER_VERSION,false);
    textAt(90,1,"BT",false);
    int rssi=connected&&isAuthorized()&&signalReadAt&&millis()-signalReadAt<30000?signalRssi:127;
    int bars=rssi>20||rssi<-127?0:rssi>=-60?4:rssi>=-70?3:rssi>=-80?2:1;
    for(int i=0;i<4;++i) { int height=i<bars?2+i*2:1; box(105+i*4,10-height,2,height,false); }
    if(refreshRequest.marker) textAt(78,1,"*",false);
    // An unsynced clock (including after a long deep sleep) is never shown.
    char stamp[24]="----/--/-- --:--";
    if(clockSynced) { time_t now=time(nullptr)+timezoneOffset; tm local; gmtime_r(&now,&local); strftime(stamp,sizeof(stamp),"%Y/%m/%d %H:%M",&local); }
    textAt(124,1,stamp,false); batteryIcon();
    if(notices.notice) {
      const char *text=notices.text(); size_t length=sweetmeter::bannerChars(text);
      box(10,40,230,40,true); box(12,42,226,36,false);
      textAt(sweetmeter::bannerTextX(text),55,String(text).substring(0,length));
    }
  }
  if(memcmp(frame,panelFrame,FRAME_SIZE) || !panelReady) displayFrame();
  uiDirty=false;
}
void buttonTask(void *) {
  // Polling at 100 Hz is kept deliberately: the pinned build has no tickless
  // idle (CONFIG_PM_ENABLE is off), so the 1 kHz FreeRTOS tick dominates
  // anyway, and this debouncer is the hardware-validated behavior.
  const int pins[]={2,1,6,4,5}; MeterButton buttons[]={{1,64},{2,32},{4},{8},{16,128}};
  for(int pin:pins) pinMode(pin,INPUT_PULLUP);
  while(true) {
    uint8_t events=0;
    for(int i=0;i<5;++i) events|=buttons[i].update(digitalRead(pins[i])==LOW,millis());
    if(events) { __atomic_fetch_or(&keyEvents,events,__ATOMIC_RELAXED); wakeWorker(); }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
void dropLink() {
  // Let the queued notification leave Bluedroid before releasing the link.
  delay(80);
  if(connected) { droppedGeneration=seenGeneration; meterServer->disconnect(meterServer->getConnId()); }
}
// Scan response: name (with "-PAIR" while the menu is open) and the capability
// marker (advertising.h), so computers can tell this firmware from pre-secret
// firmware and see an open menu without connecting.
void setScanResponse(bool menu) {
  BLEAdvertisementData response; uint8_t raw[sweetmeter::advertisingLimit];
  size_t length=sweetmeter::buildScanResponse(raw,advertisedName,menu);
  if(length) response.addData(std::string(reinterpret_cast<const char*>(raw),length));
  else response.setName(advertisedName);
  BLEDevice::getAdvertising()->setScanResponseData(response);
}
void setAdvertisedMenu(bool menu) {
  setScanResponse(menu);
  if(!connected) advertise();
}
void closeDiscovery() {
  // A computer connected while the menu closes may have lost the race with
  // the owner's choice: it keeps the bond its OS stored (bond_policy.h).
  if(connected) earnBond();
  menuClosedAt=millis(); menuEverClosed=true;
  discovery.close(); removeConfirm=keyConfirm=-1; receiving=pendingFrame=false; linkAuth.revoke();
  disconnectSleep.freshGrace(millis()); uiDirty=true; setAdvertisedMenu(false);
  if(connected) meterServer->disconnect(meterServer->getConnId());
  updateDeviceSnapshot();
}
void startComputerScan() {
  if(ota.active()) return;
  uint32_t nonce; do { nonce=esp_random(); } while(!nonce);
  discovery.begin(millis(),nonce,registry); removeConfirm=keyConfirm=-1;
  // Opening the menu revokes the current link's authorization.
  linkAuth.revoke();
  receiving=pendingFrame=false; discoveryReleaseAt=millis(); uiDirty=true;
  uint8_t event[9]={'D'}; put32(event+1,nonce); put32(event+5,60000); notifyControl(event,9);
  setAdvertisedMenu(true);
  Serial.println("UI DISCOVERY"); updateDeviceSnapshot();
}
// Rocker press in the menu: switch to a paired computer instantly, or pair a
// newly registered one (the least recently used pairing is replaced when full).
// A paired computer that sent a different secret needs a second, explicit
// confirmation before the new secret replaces the stored one.
void selectComputer(unsigned i) {
  if(!discovery.selectable(i)) return;
  const sweetmeter::Computer &c=discovery.computers[i];
  int index=c.paired;
  if(index<0 || c.registered) index=registry.add(c.id,c.name,c.secret,c.hasSecret);
  registry.select(index); saveRegistrySoon();
  Serial.printf("UI SELECT paired=%u\n",registry.count);
  closeDiscovery();
}
void chooseComputer() {
  unsigned i=discovery.selection;
  if(!discovery.selectable(i)) return;
  if(discovery.needsKeyConfirmation(i)) { keyConfirm=int(i); uiDirty=true; return; }
  selectComputer(i);
}
void acceptNewKey() {
  unsigned i=unsigned(keyConfirm); keyConfirm=-1; uiDirty=true;
  // The row may have become a conflict meanwhile; selectComputer() checks.
  if(i<discovery.count && discovery.computers[i].keyChanged) selectComputer(i);
}
void confirmRemove() {
  unsigned i=unsigned(removeConfirm); removeConfirm=-1; uiDirty=true;
  if(i>=discovery.count) return;
  int paired=discovery.computers[i].paired;
  if(paired>=0) { registry.remove(paired); saveRegistrySoon(); }
  discovery.removeAt(i,paired>=0);
  Serial.printf("UI REMOVE paired=%u\n",registry.count); updateDeviceSnapshot();
}
void onAuthorized() {
  disconnectSleep.targetConnected(); idlePower.seen(millis());
  earnBond();
  Serial.println("BLE AUTHORIZED");
}
void helloReply(uint8_t opcode,const sweetmeter::AuthOutcome &outcome) {
  uint8_t reply[2]={opcode,outcome.result}; notifyControl(reply,2);
  if(outcome.changed && linkAuth.authorized()) onAuthorized();
  if(outcome.result==sweetmeter::helloNotSelected) earnBond();  // proved a paired secret
  updateDeviceSnapshot();
  if(outcome.drop) { Serial.printf("BLE HELLO_REJECTED result=%u\n",outcome.result); dropLink(); }
}
void processControl(const uint8_t *p,size_t n) {
  using namespace sweetmeter;
  if(!n || sleepCommitted) return;
  if(p[0]=='J' || p[0]=='j' || p[0]=='K') {
    uint32_t sid=0,next=0; unsigned before=discovery.count;
    uint8_t result=discovery.handle(p,n,millis(),sid,next,linkPeer); registrationReply(result,sid,next);
    // A completed registration keeps its bond; so does an old app told to
    // update, which registers again after updating.
    if(p[0]=='K' && (result==RegOk || result==RegOutdated)) earnBond();
    if(before!=discovery.count || (p[0]=='K' && !result)) uiDirty=true;
    if(result || p[0]=='K') dropLink();
    return;
  }
  if(p[0]=='H') { helloReply('H',linkAuth.legacyHello(p,n,registry,discovery.open,ota.active(),legacyWindow,millis())); return; }
  if(p[0]=='P') {
    uint8_t challenge[challengeSize];
    portENTER_CRITICAL(&snapshotMux); memcpy(challenge,linkChallenge,sizeof(challenge)); portEXIT_CRITICAL(&snapshotMux);
    helloReply('H',linkAuth.proofHello(p,n,registry,challenge,deviceSerial,discovery.open,ota.active()));
    return;
  }
  if(p[0]=='Y') {
    // Trust on first use for a selection made by pre-secret firmware: the
    // secret is durable before the link is authorized.
    if(!linkAuth.provisionAllowed(p,n) || !registry.current()) { helloReply('Y',linkAuth.provisionRejected()); return; }
    Registry previous=registry;
    const PairedComputer current=*registry.current();
    registry.select(registry.add(current.host,current.name,p+1,true));
    bool stored=writeRegistry();
    if(stored) registryDirty=false; else registry=previous;
    helloReply('Y',linkAuth.provisioned(stored));
    return;
  }
  if(!isAuthorized() || discovery.open) {
    if(p[0]=='B') frameBeginReply(7,n>=5?read32(p+1):0,n>=9?read32(p+5):0);
    return;
  }
  if(p[0]=='T') {
    if(n==9 && !ota.active()) {
      // Sub-2-second differences are transport jitter; stepping back for them
      // could redraw the previous minute (display_policy.h).
      int64_t incoming=int64_t(read32(p+1));
      if(clockNeedsStep(clockSynced,int64_t(time(nullptr)),incoming)) { timeval tv={time_t(incoming),0}; settimeofday(&tv,nullptr); }
      timezoneOffset=int32_t(read32(p+5)); clockSynced=true; sleptTotal=0;
      if(clockGeneration!=seenGeneration) { clockGeneration=seenGeneration; minuteRedraw.firstClock(millis()); }
    }
  } else if(p[0]=='B') {
    uint32_t seq=n>=5?read32(p+1):0,checksum=n>=9?read32(p+5):0;
    if(n!=11 || ota.active() || pendingFrame || receiving || sweetmeter::u16(p+9)!=FRAME_SIZE) { frameBeginReply(2,seq,checksum); return; }
    if(criticalBattery()) { frameBeginReply(6,seq,checksum); return; }
    esp_bd_addr_t peer; copyPeer(peer); meterServer->updateConnParams(peer,12,24,0,600);
    incomingSequence=seq; incomingCRC=checksum; received=0; receiving=true; packetAt=millis();
    // Ready is an application ACK: the worker has finished any prior panel job.
    // Protocol-4 hosts cannot enqueue framebuffer data before this notification.
    frameBeginReply(0,seq,checksum);
  } else if(p[0]=='u') {
    if(n==2 && notices.result(p[1],millis())) uiDirty=true;
  } else if(p[0]=='C') {
    uint32_t seq=n>=5?read32(p+1):0;
    if(n!=5 || !receiving || pendingFrame || received!=FRAME_SIZE || seq!=incomingSequence || crc32(incomingFrame,FRAME_SIZE)!=incomingCRC) {
      receiving=false; bleReply(3,seq,incomingCRC);
    } else { receiving=false; pendingFrame=true; }
  } else bleReply(3,0,0);
}
void processFrameData(const uint8_t *p,size_t n) {
  if(!isAuthorized() || ota.active() || discovery.open) return;
  if(!receiving || pendingFrame || n<=2) { receiving=false; bleReply(4,incomingSequence,incomingCRC); return; }
  if(sweetmeter::u16(p)!=received || n-2>FRAME_SIZE-received) { receiving=false; bleReply(4,incomingSequence,incomingCRC); return; }
  memcpy(incomingFrame+received,p+2,n-2); received+=n-2; packetAt=millis();
}
// Apply a connect/disconnect observed from the BTC callbacks. Called before
// each packet so a packet is never handled with the previous link's state.
void syncLink(uint32_t now) {
  uint32_t generation=__atomic_load_n(&linkGeneration,__ATOMIC_ACQUIRE);
  if(generation==seenGeneration) return;
  bool wasAuthorized=linkAuth.authorized(); seenGeneration=generation; linkAuth.reset(); receiving=pendingFrame=false;
  if(wasAuthorized) disconnectSleep.targetDisconnected(now);
  serviceChangeSent=false; discoveryReleaseAt=0; discovery.resetRegistration(); copyPeer(linkPeer);
  if(notices.link(generation)) uiDirty=true;
  if(connected && !discovery.open &&
     sweetmeter::menuRaceEarnsBond(false,connectionAt,menuClosedAt,menuEverClosed)) earnBond();
  if(!connected) { advertise(); Serial.println("BLE DISCONNECTED"); }
  updateDeviceSnapshot();
}
void setupBluetooth() {
  nvsReady=preferences.begin("quota-meter",false);
  bootHealth.initialize(preferences,nvsReady);
  loadRegistry();
  // Earlier builds kept a "peers" list for bond eviction; bonds are now
  // removed only per link (bond_policy.h), so drop the stale key.
  if(nvsReady && preferences.isKey("peers")) preferences.remove("peers");
  uint64_t mac=ESP.getEfuseMac();
  for(int i=0;i<6;++i) { uint8_t byte=uint8_t(mac>>(8*i)); sweetmeter::hexEncode(&byte,1,deviceSerial+2*i); }
  packetQueue=xQueueCreate(4,sizeof(HostPacket)); rejectQueue=xQueueCreate(4,sizeof(HostPacket));
  bool allocated=packetQueue && rejectQueue && xTaskCreate(buttonTask,"buttons",2048,nullptr,1,nullptr)==pdPASS;
  if(!allocated && bootHealth.pending) bootHealth.fail();
  if(!allocated) { Serial.println("ERR REQUIRED_ALLOCATION"); while(true) delay(1000); }
  memset(dashboard,0xff,FRAME_SIZE);
  snprintf(advertisedName,sizeof(advertisedName),"Sweetmeter-%04X",unsigned(mac&0xffff));
  BLEDevice::init(advertisedName); bleStarted=true;
  BLEDevice::setCustomGapHandler([](esp_gap_ble_cb_event_t event,esp_ble_gap_cb_param_t *parameters) {
    if(event==ESP_GAP_BLE_READ_RSSI_COMPLETE_EVT && connected) {
      esp_bd_addr_t peer; copyPeer(peer);
      if(!memcmp(parameters->read_rssi_cmpl.remote_addr,peer,sizeof(peer))) {
        signalRssi=parameters->read_rssi_cmpl.status==ESP_BT_STATUS_SUCCESS?parameters->read_rssi_cmpl.rssi:127;
        signalReadAt=millis();
      }
    }
  });
  BLEDevice::setCustomGattsHandler([](esp_gatts_cb_event_t event,esp_gatt_if_t interface,esp_ble_gatts_cb_param_t *parameters) {
    if(event==ESP_GATTS_REG_EVT && parameters->reg.status==ESP_GATT_OK) gattInterface=interface;
    if(event==ESP_GATTS_START_EVT) bleServiceStarted=parameters->start.status==ESP_GATT_OK;
  });
  BLEDevice::setMTU(185); BLEDevice::setEncryptionLevel(ESP_BLE_SEC_ENCRYPT);
  BLESecurity *security=new BLESecurity(); security->setAuthenticationMode(ESP_LE_AUTH_REQ_SC_BOND);
  security->setCapability(ESP_IO_CAP_NONE); security->setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK|ESP_BLE_ID_KEY_MASK);
  security->setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK|ESP_BLE_ID_KEY_MASK);
  meterServer=BLEDevice::createServer();
  if(!meterServer && bootHealth.pending) bootHealth.fail();
  if(!meterServer) { Serial.println("ERR BLE_SERVER"); while(true) delay(1000); }
  meterServer->setCallbacks(new ServerCallbacks());
  // Reserve enough handles for seven characteristics and two CCCDs.
  BLEService *service=meterServer->createService(BLEUUID(SERVICE_UUID),32);
  controlCharacteristic=service->createCharacteristic(CONTROL_UUID,BLECharacteristic::PROPERTY_WRITE|BLECharacteristic::PROPERTY_NOTIFY);
  controlCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED);
  controlCccd=new BLE2902(); controlCharacteristic->addDescriptor(controlCccd); controlCharacteristic->setCallbacks(new PacketCallbacks(DashboardControl));
  auto *dataCharacteristic=service->createCharacteristic(DATA_UUID,BLECharacteristic::PROPERTY_WRITE);
  dataCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED); dataCharacteristic->setCallbacks(new PacketCallbacks(DashboardData));
  auto *statusCharacteristic=service->createCharacteristic(STATUS_UUID,BLECharacteristic::PROPERTY_READ);
  statusCharacteristic->setAccessPermissions(ESP_GATT_PERM_READ_ENCRYPTED); statusCharacteristic->setCallbacks(new StatusCallbacks());
  auto *otaControl=service->createCharacteristic(OTA_CONTROL_UUID,BLECharacteristic::PROPERTY_WRITE);
  otaControl->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED); otaControl->setCallbacks(new PacketCallbacks(OtaControl));
  auto *otaData=service->createCharacteristic(OTA_DATA_UUID,BLECharacteristic::PROPERTY_WRITE);
  otaData->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED); otaData->setCallbacks(new PacketCallbacks(OtaData));
  otaStatusCharacteristic=service->createCharacteristic(OTA_STATUS_UUID,BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_NOTIFY);
  otaStatusCharacteristic->setAccessPermissions(ESP_GATT_PERM_READ_ENCRYPTED);
  otaStatusCccd=new BLE2902(); otaStatusCharacteristic->addDescriptor(otaStatusCccd); otaStatusCharacteristic->setCallbacks(new OtaStatusCallbacks());
  publishOta(ota.status,true); updateDeviceSnapshot(); service->start();
  BLEAdvertising *advertising=BLEDevice::getAdvertising();
  BLEAdvertisementData advertisement;
  advertisement.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC|ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
  advertisement.setCompleteServices(BLEUUID(SERVICE_UUID));
  advertising->setAdvertisementData(advertisement); setScanResponse(false);
  advertising->setMinInterval(800); advertising->setMaxInterval(1600); advertise();
  drawScreen();
  bool bleReady=bleServiceStarted && service->getHandle()!=0 && controlCharacteristic->getHandle()!=0 && otaStatusCharacteristic->getHandle()!=0;
  bootHealth.finish(allocated && nvsReady && panelReady && bleReady);
  idlePower.seen(millis());
  updateDeviceSnapshot();
  Serial.printf("READY SWEETMETER %s protocol=4 paired=%u wake=%d reset=%d\n",SWEETMETER_VERSION,registry.count,
                int(esp_sleep_get_wakeup_cause()),int(esp_reset_reason()));
}
void handleKeys(uint8_t keys,uint32_t now) {
  if(keys) idlePower.seen(now);
  if(keys&64) { if(ota.active()) ota.cancel(); sleepCommitted=true; powerOff("Release the top button."); }
  if(ota.active()) return;
  if(keys&32) { startComputerScan(); return; }
  if(discovery.open && removeConfirm>=0) {
    if(keys&16) confirmRemove();
    else if(keys) { removeConfirm=-1; uiDirty=true; }
    return;
  }
  if(discovery.open && keyConfirm>=0) {
    if(keys&16) acceptNewKey();
    else if(keys) { keyConfirm=-1; uiDirty=true; }
    return;
  }
  if(discovery.open) {
    if(keys&1) startComputerScan();
    if((keys&4)&&discovery.count) { discovery.selection=(discovery.selection+discovery.count-1)%discovery.count; uiDirty=true; }
    if((keys&8)&&discovery.count) { discovery.selection=(discovery.selection+1)%discovery.count; uiDirty=true; }
    if(keys&16) { if(discovery.count) chooseComputer(); return; }
    if((keys&128)&&discovery.count) { removeConfirm=int(discovery.selection); uiDirty=true; }
    if(keys&2) closeDiscovery();
    return;
  }
  if(keys&1) {
    // With a computer the answering frame is drawn, not the press (one
    // refresh); without one the "*" marker acknowledges it now.
    bool linked=connected&&isAuthorized();
    if(refreshRequest.press(now,linked)) uiDirty=true;
    if(linked) { uint8_t request='R'; notifyControl(&request,1); }
    else if(!connected) advertise();
  }
  if(keys&128) {
    // Holding the rocker asks the selected computer to check and install firmware.
    uiDirty=true;
    if(notices.hold(registry.selected>=0,connected&&isAuthorized(),now,seenGeneration)) { uint8_t request='U'; notifyControl(&request,1); }
  }
}
void bluetoothLoop() {
  using namespace sweetmeter;
  static uint32_t batteryAt=0,snapshotAt=0,otaDrawAt=0,signalPollAt=0;
  static unsigned otaDrawPercent=0;
  static bool wasOta=false;
  uint32_t now=millis();
  syncLink(now);
  // Trigger rediscovery after an encrypted status read. Arduino exposes the
  // registered GATT interface through its supported custom event callback.
  if(connected && firstProbeAt && !serviceChangeSent && gattInterface!=ESP_GATT_IF_NONE) {
    serviceChangeSent=true; esp_bd_addr_t peer; copyPeer(peer);
    esp_err_t result=esp_ble_gatts_send_service_change_indication(gattInterface,peer);
    Serial.printf("BLE SERVICE_CHANGED result=%d\n",int(result));
  }
  if(now-batteryAt>=10000) { readBattery(); batteryAt=now; }
  if(connected && isAuthorized() && !ota.active() && now-signalPollAt>=10000) {
    signalPollAt=now; esp_bd_addr_t peer; copyPeer(peer); esp_ble_gap_read_rssi(peer);
  }
  ota.tick();
  updateOtaRadio();
  if(ota.exited) {
    ota.exited=false; uiDirty=true;
    notices.otaEnded(ota.status.state==OtaState::Error,now);
    if(!connected || !isAuthorized()) disconnectSleep.freshGrace(now);
  }
  if(criticalBattery() && !ota.commitCritical) { if(ota.active()) ota.terminal(OtaError::Power,0); lowBatterySleep(false); }
  bool wasMenu=discovery.open; discovery.tick(now);
  if(wasMenu && !discovery.open) closeDiscovery();
  if(discovery.open && connected &&
     ((discoveryReleaseAt && expired(now,discoveryReleaseAt,2000)) || expired(now,connectionAt,15000) ||
       (firstProbeAt && expired(now,firstProbeAt,8000)))) {
    discoveryReleaseAt=0; meterServer->disconnect(meterServer->getConnId());
  }
  // The old connection alone receives the 2 second D-event release deadline.
  if(!connected) discoveryReleaseAt=0;
  // Outside the menu a link must authorize within 10 s, so a stray phone or a
  // stale OS auto-connection cannot hold the single link.
  if(connected && !isAuthorized() && !discovery.open && !ota.active() && droppedGeneration!=seenGeneration &&
     expired(now,connectionAt,10000)) {
    droppedGeneration=seenGeneration; Serial.println("BLE DROP unauthorized");
    meterServer->disconnect(meterServer->getConnId());
  }
  if(!ota.active() && !discovery.open && disconnectSleep.expired(now)) {
    sleepCommitted=true; Serial.println("POWER AUTO_OFF grace_ms=30000"); powerOff("Bluetooth disconnected.");
  }
  if(idlePower.expired(now,connected&&isAuthorized(),discovery.open,ota.active())) {
    sleepCommitted=true; Serial.println("POWER IDLE_OFF minutes=30"); powerOff("No computer connected.");
  }
  legacyWindow.open(now);  // latches the migration window closed after ten minutes
  persistPending(now);
  uint8_t keys=__atomic_exchange_n(&keyEvents,uint8_t(0),__ATOMIC_RELAXED);
  handleKeys(keys,now);
  HostPacket packet;
  for(unsigned handled=0;handled<4 && xQueueReceive(packetQueue,&packet,0)==pdTRUE;++handled) {
    syncLink(millis());
    if(packet.generation!=seenGeneration || !connected) continue;
    if(packet.invalid) {
      if(packet.kind==OtaControl || packet.kind==OtaData) {
        uint32_t sid=packet.kind==OtaData?(packet.size>=4?read32(packet.bytes):0):(packet.size>=5?read32(packet.bytes+1):0);
        ota.malformed(sid,packet.kind==OtaData?'d':packet.bytes[0]);
      } else {
        if(packet.kind==DashboardControl && packet.size && (packet.bytes[0]=='J'||packet.bytes[0]=='j'||packet.bytes[0]=='K')) discovery.resetRegistration();
        if(packet.kind==DashboardData) receiving=false;
        rejectPacket(packet.kind,packet.bytes,packet.size,false);
      }
    } else switch(packet.kind) {
      case DashboardControl: processControl(packet.bytes,packet.size); break;
      case DashboardData: processFrameData(packet.bytes,packet.size); break;
      case OtaControl: ota.control(packet.bytes,packet.size); break;
      case OtaData: ota.data(packet.bytes,packet.size); break;
    }
  }
  // Packets the callback could not queue get their BUSY replies from here.
  while(xQueueReceive(rejectQueue,&packet,0)==pdTRUE) {
    syncLink(millis());
    if(packet.generation==seenGeneration && connected) {
      if(packet.kind==DashboardData) receiving=false;
      rejectPacket(packet.kind,packet.bytes,packet.size,true);
    }
  }
  if(uint32_t lost=__atomic_exchange_n(&droppedRejects,0u,__ATOMIC_RELAXED)) Serial.printf("ERR REJECT_OVERFLOW count=%lu\n",(unsigned long)lost);
  // M and every synchronous cancel/error can change OTA state in this batch.
  // Switch radio mode before any progress-panel draw or the next host packet.
  updateOtaRadio();
  if(pendingFrame && !ota.active()) {
    // Frames are ACKed as soon as they are stored. The panel is redrawn with
    // the next minute's clock tick, so a frame every minute plus the clock
    // costs one full refresh per minute. Only the first dashboard after boot
    // and one that answers a refresh press are drawn now.
    uint8_t result=6;
    if(!criticalBattery()) {
      memcpy(dashboard,incomingFrame,FRAME_SIZE);
      bool first=!hasFrame; hasFrame=true; lastCRC=incomingCRC;
      bool answersPress=refreshRequest.frame();
      if(drawFrameNow(first,answersPress)) {
        drawScreen(); minuteRedraw.painted(displayedMinute(int64_t(time(nullptr)),timezoneOffset));
        result=panelReady?frameDisplayed:5;
      } else result=frameDeferred;
    }
    bleReply(result,incomingSequence,incomingCRC); pendingFrame=false;
    esp_bd_addr_t peer; copyPeer(peer); meterServer->updateConnParams(peer,48,72,4,600);
    Serial.printf("BLE ACK %lu %08lx result=%u\n",(unsigned long)incomingSequence,(unsigned long)incomingCRC,result);
  }
  now=millis();
  if(receiving && expired(now,packetAt,15000)) receiving=false;
  // Removing the refresh marker or a result banner waits for the minute draw.
  if(refreshRequest.tick(now)==NoticeChange::DrawNow) uiDirty=true;
  if(notices.tick(now)==NoticeChange::DrawNow) uiDirty=true;
  bool active=ota.active();
  if(active) {
    unsigned percent=ota.status.total?uint64_t(ota.status.offset)*100/ota.status.total:0;
    if(!wasOta || (ota.status.state>=OtaState::Image && percent>=otaDrawPercent+10 && expired(now,otaDrawAt,5000))) {
      drawScreen(); otaDrawAt=millis(); otaDrawPercent=percent;
    }
  } else {
    if(wasOta) uiDirty=true;
    otaDrawPercent=0;
    // At most one scheduled full refresh per minute; drawScreen() skips the
    // panel entirely when nothing visible changed.
    int64_t minute=displayedMinute(int64_t(time(nullptr)),timezoneOffset);
    if(!receiving && (uiDirty || (!discovery.open && minuteRedraw.due(minute,now)))) { drawScreen(); minuteRedraw.painted(minute); }
  }
  wasOta=active;
  bootHealth.retry(millis());
  if(expired(millis(),snapshotAt,discovery.open?250:1000) || keys) { updateDeviceSnapshot(); snapshotAt=millis(); }
  // Sleep until a packet, button, connection event or the next deadline check
  // instead of spinning every millisecond.
  if(uxQueueMessagesWaiting(packetQueue) || uxQueueMessagesWaiting(rejectQueue)) taskYIELD();
  else ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(receiving||active||discovery.open||(connected&&!isAuthorized())?50:250));
}
void meterWorker(void *) { setupBluetooth(); while(true) bluetoothLoop(); }
