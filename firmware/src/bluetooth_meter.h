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
#include <freertos/queue.h>
#include "screen_font.h"
#include "buttons.h"
#include "disconnect_sleep.h"
#include "discovery.h"
#include "ota_runtime.h"

const char *SERVICE_UUID="7a1e0001-ff1b-4d9f-a023-47c7752c1a01";
const char *CONTROL_UUID="7a1e0002-ff1b-4d9f-a023-47c7752c1a01";
const char *DATA_UUID="7a1e0003-ff1b-4d9f-a023-47c7752c1a01";
const char *STATUS_UUID="7a1e0004-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_CONTROL_UUID="7a1e0005-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_DATA_UUID="7a1e0006-ff1b-4d9f-a023-47c7752c1a01";
const char *OTA_STATUS_UUID="7a1e0007-ff1b-4d9f-a023-47c7752c1a01";
BLECharacteristic *controlCharacteristic=nullptr,*otaStatusCharacteristic=nullptr;
BLEServer *meterServer=nullptr;
esp_bd_addr_t peerAddress={};
Preferences preferences;
String selectedHost,selectedName;
bool authorized=false,hostDirty=false;
volatile bool connected=false;
volatile uint32_t linkGeneration=0,connectionAt=0,firstProbeAt=0;
volatile esp_gatt_if_t gattInterface=ESP_GATT_IF_NONE;
volatile bool bleServiceStarted=false;
RTC_DATA_ATTR int32_t timezoneOffset=0;
RTC_DATA_ATTR bool clockSynced=false;
volatile uint8_t keyEvents=0;
uint8_t dashboard[FRAME_SIZE],incomingFrame[FRAME_SIZE];
size_t received=0;
uint32_t incomingSequence=0,incomingCRC=0,packetAt=0,refreshRequestedAt=0;
bool receiving=false,pendingFrame=false,uiDirty=true,sleepCommitted=false;
int batteryPercent=-1,batteryMillivolts=-1;
DisconnectSleep disconnectSleep;
sweetmeter::Discovery discovery;
sweetmeter::BootHealth bootHealth;
portMUX_TYPE snapshotMux=portMUX_INITIALIZER_UNLOCKED;
char deviceSnapshot[512]{};
uint8_t otaSnapshot[20]{};
QueueHandle_t packetQueue=nullptr;
struct HostPacket { uint32_t generation; uint16_t size; uint8_t kind; bool invalid; uint8_t bytes[182]; };
enum PacketKind : uint8_t { DashboardControl, DashboardData, OtaControl, OtaData };
void drawScreen();
uint32_t read32(const uint8_t *p) { return sweetmeter::u32(p); }
void put32(uint8_t *p,uint32_t value) { sweetmeter::put32(p,value); }
void notifyControl(uint8_t *bytes,size_t size) {
  if(connected && controlCharacteristic) { controlCharacteristic->setValue(bytes,size); controlCharacteristic->notify(); }
}
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
  if(connected && otaStatusCharacteristic) { otaStatusCharacteristic->setValue(bytes,20); otaStatusCharacteristic->notify(); }
}
sweetmeter::OtaInputs otaInputs() {
  sweetmeter::OtaInputs value; value.authorized=authorized; value.connected=connected;
  value.menu=discovery.open; value.frameBusy=receiving||pendingFrame;
  value.critical=batteryPercent>=0&&(batteryPercent<=5||batteryMillivolts<=3350);
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
void updateOtaRadio() {
  sweetmeter::RadioChange change=otaRadio.update(ota.active(),connected);
  if(change==sweetmeter::RadioChange::Fast) meterServer->updateConnParams(peerAddress,12,24,0,600);
  if(change==sweetmeter::RadioChange::Idle) meterServer->updateConnParams(peerAddress,48,72,4,600);
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


// The only callback work is bounded copying/queueing (and immediate BUSY/error
// notifications when a packet cannot enter the queue). Flash/crypto/UI/NVS all
// belong to the single 16 KiB worker, never BTC_TASK or frameMutex.
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
    registrationReply(busy?6:1,n>=5?read32(p+1):0,0); return;
  }
  if(kind==DashboardControl && n && p[0]=='H') { uint8_t answer[2]={'H',7}; notifyControl(answer,2); return; }
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
    bool hello=kind_==DashboardControl && n>=37 && n<=57 && p[0]=='H';
    HostPacket item{}; item.generation=linkGeneration; item.kind=kind_; item.size=min(n,size_t(182));
    item.invalid=n>182 || (!hello && n>budget);
    if(item.size) memcpy(item.bytes,p,item.size);
    if(!packetQueue || xQueueSend(packetQueue,&item,0)!=pdTRUE) rejectPacket(kind_,p,n,true);
    if(kind_==DashboardControl && n && (p[0]=='J'||p[0]=='j'||p[0]=='K')) {
      uint32_t expected=0; __atomic_compare_exchange_n(&firstProbeAt,&expected,millis(),false,__ATOMIC_RELAXED,__ATOMIC_RELAXED);
    }
  }
};
class StatusCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    static char copy[512];
    portENTER_CRITICAL(&snapshotMux); memcpy(copy,deviceSnapshot,sizeof(copy)); portEXIT_CRITICAL(&snapshotMux);
    characteristic->setValue(copy);
    uint32_t expected=0; __atomic_compare_exchange_n(&firstProbeAt,&expected,millis(),false,__ATOMIC_RELAXED,__ATOMIC_RELAXED);
  }
};
class OtaStatusCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t copy[20]; portENTER_CRITICAL(&snapshotMux); memcpy(copy,otaSnapshot,20); portEXIT_CRITICAL(&snapshotMux);
    characteristic->setValue(copy,20);
  }
};
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override {
    connected=true; connectionAt=millis(); firstProbeAt=0; __atomic_add_fetch(&linkGeneration,1,__ATOMIC_RELEASE);
  }
  void onConnect(BLEServer *server,esp_ble_gatts_cb_param_t *parameters) override {
    memcpy(peerAddress,parameters->connect.remote_bda,sizeof(peerAddress));
    server->updateConnParams(peerAddress,12,24,0,600);
  }
  void onDisconnect(BLEServer *) override {
    connected=false; __atomic_add_fetch(&linkGeneration,1,__ATOMIC_RELEASE);
  }
};
void updateDeviceSnapshot() {
  char next[512];
  int length=snprintf(next,sizeof(next),
    "{\"protocol\":4,\"firmware\":\"%s\",\"board\":\"%s\",\"selected_host\":\"%s\",\"battery_percent\":%d,\"battery_mv\":%d,\"interval\":%u,\"critical\":%s,\"charge_state\":\"unknown\",\"clock_synced\":%s,\"menu\":%s,\"discovery_nonce\":%lu,\"discovery_remaining_ms\":%lu,\"computers\":%u,\"ota\":%s,\"boot_health\":\"%s\",\"last_update\":\"%s\",\"ota_target\":\"%s\"}",
    SWEETMETER_VERSION,sweetmeter::boardId,selectedHost.c_str(),batteryPercent,batteryMillivolts,refreshSeconds(),
    criticalBattery()?"true":"false",clockSynced?"true":"false",discovery.open?"true":"false",
    (unsigned long)(discovery.open?discovery.nonce:0),(unsigned long)discovery.remaining(millis()),discovery.count,
    ota.active()?"true":"false",bootHealth.health,bootHealth.lastUpdate,bootHealth.target);
  if(length<0 || size_t(length)>=sizeof(next)) {
    // Never expose truncated JSON. The fixed bounded fields are covered by tests.
    Serial.println("ERR STATUS_OVERFLOW"); return;
  }
  portENTER_CRITICAL(&snapshotMux); memcpy(deviceSnapshot,next,length+1); portEXIT_CRITICAL(&snapshotMux);
}
void powerOffPanel() {
  // Q9 disconnects LCD ground, not VDD. Avoid supplying a ground return via SPI.
  for (int pin : {SCK_PIN, MOSI_PIN, RST, DC, CS}) pinMode(pin, INPUT);
  digitalWrite(POWER, LOW); digitalWrite(LED, LOW);
}

void enterSleep(unsigned int seconds) {
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
    memset(frame,0xff,FRAME_SIZE); box(0,0,250,14,true); textAt(4,1,"SELECT COMPUTER",false);
    textAt(4,16,"Open Sweetmeter on your computer.");
    unsigned first=discovery.selection>=4?discovery.selection-3:0;
    for(unsigned i=first;i<discovery.count && i<first+4;++i) {
      int y=32+(i-first)*17; bool chosen=i==discovery.selection;
      if(chosen) box(2,y,246,15,true);
      textAt(5,y+2,discovery.computers[i].name,!chosen);
    }
    if(!discovery.count) textAt(4,43,"Waiting for computers...");
    textAt(4,107,"Top: rescan    Bottom: back");
  } else {
    memcpy(frame,dashboard,FRAME_SIZE);
    if(!hasFrame) {
      textAt(4,37,"Open Sweetmeter on your computer.");
      textAt(4,55,"Hold bottom 3s to select it.");
      textAt(4,77,"github.com/luvxinc/Sweetmeter");
    }
    box(0,0,250,13,true); textAt(3,1,"v " SWEETMETER_VERSION,false);
    textAt(90,1,connected&&authorized?"BT":"--",false);
    if(refreshRequestedAt && millis()-refreshRequestedAt<10000) textAt(112,1,"*",false);
    char stamp[24]="----/--/-- --:--";
    if(clockSynced) { time_t now=time(nullptr)+timezoneOffset; tm local; gmtime_r(&now,&local); strftime(stamp,sizeof(stamp),"%Y/%m/%d %H:%M",&local); }
    textAt(124,1,stamp,false); batteryIcon();
  }
  if(memcmp(frame,panelFrame,FRAME_SIZE) || !panelReady) displayFrame();
  uiDirty=false;
}
void buttonTask(void *) {
  const int pins[]={2,1,6,4,5}; MeterButton buttons[]={{1,64},{2,32},{4},{8},{16}};
  for(int pin:pins) pinMode(pin,INPUT_PULLUP);
  while(true) {
    for(int i=0;i<5;++i) {
      uint8_t event=buttons[i].update(digitalRead(pins[i])==LOW,millis());
      if(event) __atomic_fetch_or(&keyEvents,event,__ATOMIC_RELAXED);
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
void closeDiscovery() {
  discovery.close(); receiving=pendingFrame=false; authorized=false;
  disconnectSleep.freshGrace(millis()); uiDirty=true;
  if(connected) meterServer->disconnect(meterServer->getConnId());
}
uint32_t discoveryReleaseAt=0;
void startComputerScan() {
  if(ota.active()) return;
  uint32_t nonce; do { nonce=esp_random(); } while(!nonce);
  discovery.begin(millis(),nonce,selectedHost.c_str(),selectedName.c_str());
  authorized=false; receiving=pendingFrame=false; discoveryReleaseAt=millis(); uiDirty=true;
  uint8_t event[9]={'D'}; put32(event+1,nonce); put32(event+5,60000); notifyControl(event,9);
  Serial.println("UI DISCOVERY"); updateDeviceSnapshot();
}
void processControl(const uint8_t *p,size_t n) {
  if(!n || sleepCommitted) return;
  if(p[0]=='J' || p[0]=='j' || p[0]=='K') {
    uint32_t sid=0,next=0; unsigned before=discovery.count;
    uint8_t result=discovery.handle(p,n,millis(),sid,next); registrationReply(result,sid,next);
    if(before!=discovery.count || p[0]=='K') uiDirty=true;
    if(result || p[0]=='K') {
      // Allow the notify to leave Bluedroid before releasing this candidate.
      delay(80); if(connected) meterServer->disconnect(meterServer->getConnId());
    }
    return;
  }
  if(p[0]=='H') {
    bool wasAuthorized=authorized; authorized=false;
    if(!discovery.open && !ota.active() && n>=37 && n<=57 && sweetmeter::hostId(p+1,36) &&
       (n==37 || sweetmeter::hostName(p+37,n-37)) && selectedHost.length()==36 && !memcmp(selectedHost.c_str(),p+1,36)) {
      authorized=true; disconnectSleep.targetConnected();
    } else if(wasAuthorized) disconnectSleep.targetDisconnected(millis());
    uint8_t hello[2]={'H',uint8_t(authorized?0:7)}; notifyControl(hello,2); uiDirty=true; return;
  }
  if(!authorized || discovery.open) {
    if(p[0]=='B') frameBeginReply(7,n>=5?read32(p+1):0,n>=9?read32(p+5):0);
    return;
  }
  if(p[0]=='T') {
    if(n==9 && !ota.active()) { timeval tv={time_t(read32(p+1)),0}; settimeofday(&tv,nullptr); timezoneOffset=int32_t(read32(p+5)); clockSynced=true; }
  } else if(p[0]=='B') {
    uint32_t seq=n>=5?read32(p+1):0,checksum=n>=9?read32(p+5):0;
    if(n!=11 || ota.active() || pendingFrame || receiving || sweetmeter::u16(p+9)!=FRAME_SIZE) { frameBeginReply(2,seq,checksum); return; }
    if(criticalBattery()) { frameBeginReply(6,seq,checksum); return; }
    meterServer->updateConnParams(peerAddress,12,24,0,600);
    incomingSequence=seq; incomingCRC=checksum; received=0; receiving=true; packetAt=millis();
    // Ready is an application ACK: the worker has finished any prior panel job.
    // Protocol-4 hosts cannot enqueue framebuffer data before this notification.
    frameBeginReply(0,seq,checksum);
  } else if(p[0]=='C') {
    uint32_t seq=n>=5?read32(p+1):0;
    if(n!=5 || !receiving || pendingFrame || received!=FRAME_SIZE || seq!=incomingSequence || crc32(incomingFrame,FRAME_SIZE)!=incomingCRC) {
      receiving=false; bleReply(3,seq,incomingCRC);
    } else { receiving=false; pendingFrame=true; }
  } else bleReply(3,0,0);
}
void processFrameData(const uint8_t *p,size_t n) {
  if(!authorized || ota.active() || discovery.open) return;
  if(!receiving || pendingFrame || n<=2) { receiving=false; bleReply(4,incomingSequence,incomingCRC); return; }
  if(sweetmeter::u16(p)!=received || n-2>FRAME_SIZE-received) { receiving=false; bleReply(4,incomingSequence,incomingCRC); return; }
  memcpy(incomingFrame+received,p+2,n-2); received+=n-2; packetAt=millis();
}
void setupBluetooth() {
  bool nvs=preferences.begin("quota-meter",false);
  bootHealth.initialize(preferences,nvs);
  selectedHost=preferences.getString("host",""); selectedName=preferences.getString("name","");
  if(!sweetmeter::hostId(reinterpret_cast<const uint8_t*>(selectedHost.c_str()),selectedHost.length())) { selectedHost=""; selectedName=""; }
  if(selectedName.length() && !sweetmeter::hostName(reinterpret_cast<const uint8_t*>(selectedName.c_str()),selectedName.length())) selectedName="Computer";
  packetQueue=xQueueCreate(4,sizeof(HostPacket));
  bool allocated=packetQueue && xTaskCreate(buttonTask,"buttons",2048,nullptr,1,nullptr)==pdPASS;
  if(!allocated && bootHealth.pending) bootHealth.fail();
  if(!allocated) { Serial.println("ERR REQUIRED_ALLOCATION"); while(true) delay(1000); }
  memset(dashboard,0xff,FRAME_SIZE);
  Wire.begin(40,41,100000); Wire.setTimeOut(25); readBattery();
  char name[24]; snprintf(name,sizeof(name),"Sweetmeter-%04X",unsigned(ESP.getEfuseMac()&0xffff));
  BLEDevice::init(name);
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
  controlCharacteristic->addDescriptor(new BLE2902()); controlCharacteristic->setCallbacks(new PacketCallbacks(DashboardControl));
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
  otaStatusCharacteristic->addDescriptor(new BLE2902()); otaStatusCharacteristic->setCallbacks(new OtaStatusCallbacks());
  publishOta(ota.status,true); updateDeviceSnapshot(); service->start();
  BLEAdvertising *advertising=BLEDevice::getAdvertising();
  BLEAdvertisementData advertisement,response;
  advertisement.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC|ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
  advertisement.setCompleteServices(BLEUUID(SERVICE_UUID)); response.setName(name);
  advertising->setAdvertisementData(advertisement); advertising->setScanResponseData(response);
  advertising->setMinInterval(800); advertising->setMaxInterval(1600); BLEDevice::startAdvertising();
  drawScreen();
  bool bleReady=bleServiceStarted && service->getHandle()!=0 && controlCharacteristic->getHandle()!=0 && otaStatusCharacteristic->getHandle()!=0;
  bootHealth.finish(allocated && nvs && panelReady && bleReady);
  updateDeviceSnapshot();
  Serial.printf("READY SWEETMETER %s protocol=4 wake=%d reset=%d\n",SWEETMETER_VERSION,int(esp_sleep_get_wakeup_cause()),int(esp_reset_reason()));
}
void bluetoothLoop() {
  using namespace sweetmeter;
  static uint32_t seenGeneration=0,batteryAt=0,snapshotAt=0,otaDrawAt=0;
  static unsigned otaDrawPercent=0;
  static bool serviceChangeSent=false,wasOta=false;
  static time_t paintedMinute=-1;
  uint32_t now=millis(),generation=linkGeneration;
  if(generation!=seenGeneration) {
    bool wasAuthorized=authorized; seenGeneration=generation; authorized=false; receiving=pendingFrame=false;
    if(wasAuthorized) disconnectSleep.targetDisconnected(now);
    serviceChangeSent=false; discoveryReleaseAt=0; discovery.resetRegistration(); uiDirty=true;
    if(!connected) { BLEDevice::startAdvertising(); Serial.println("BLE DISCONNECTED"); }
  }
  // Trigger rediscovery after an encrypted status read. Arduino exposes the
  // registered GATT interface through its supported custom event callback.
  if(connected && firstProbeAt && !serviceChangeSent && gattInterface!=ESP_GATT_IF_NONE) {
    serviceChangeSent=true;
    esp_err_t result=esp_ble_gatts_send_service_change_indication(gattInterface,peerAddress);
    Serial.printf("BLE SERVICE_CHANGED result=%d\n",int(result));
  }
  if(now-batteryAt>=10000) { readBattery(); batteryAt=now; }
  ota.tick();
  updateOtaRadio();
  if(ota.exited) {
    ota.exited=false; uiDirty=true;
    if(!connected || !authorized) disconnectSleep.freshGrace(now);
  }
  if(criticalBattery() && !ota.commitCritical) { if(ota.active()) ota.terminal(OtaError::Power,0); enterSleep(300); }
  bool wasMenu=discovery.open; discovery.tick(now);
  if(wasMenu && !discovery.open) closeDiscovery();
  if(discovery.open && connected &&
     ((discoveryReleaseAt && expired(now,discoveryReleaseAt,2000)) || expired(now,connectionAt,15000) ||
       (firstProbeAt && expired(now,firstProbeAt,8000)))) {
    discoveryReleaseAt=0; meterServer->disconnect(meterServer->getConnId());
  }
  // The old connection alone receives the 2 second D-event release deadline.
  if(!connected) discoveryReleaseAt=0;
  if(!ota.active() && !discovery.open && disconnectSleep.expired(now)) {
    sleepCommitted=true; Serial.println("POWER AUTO_OFF grace_ms=30000"); powerOff(true);
  }
  if(hostDirty) {
    bool saved=preferences.putString("host",selectedHost)==selectedHost.length() && preferences.putString("name",selectedName)==selectedName.length();
    if(saved) hostDirty=false; else Serial.println("ERR HOST_NVS");
  }
  uint8_t keys=__atomic_exchange_n(&keyEvents,uint8_t(0),__ATOMIC_RELAXED);
  if(keys&64) { if(ota.active()) ota.cancel(); powerOff(); }
  if(!ota.active()) {
    if(keys&32) startComputerScan();
    if(discovery.open) {
      if(keys&1) startComputerScan();
      if((keys&4)&&discovery.count) { discovery.selection=(discovery.selection+discovery.count-1)%discovery.count; uiDirty=true; }
      if((keys&8)&&discovery.count) { discovery.selection=(discovery.selection+1)%discovery.count; uiDirty=true; }
      if((keys&16)&&discovery.count) {
        selectedHost=discovery.computers[discovery.selection].id; selectedName=discovery.computers[discovery.selection].name;
        hostDirty=true; closeDiscovery(); Serial.println("UI SELECT");
      }
      if(keys&2) closeDiscovery();
    } else if(keys&1) {
      refreshRequestedAt=now; uiDirty=true;
      if(connected&&authorized) { uint8_t request='R'; notifyControl(&request,1); }
      else if(!connected) BLEDevice::startAdvertising();
    }
  }
  HostPacket packet;
  for(unsigned handled=0;handled<4 && xQueueReceive(packetQueue,&packet,0)==pdTRUE;++handled) {
    if(packet.generation!=linkGeneration || !connected) continue;
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
  // M and every synchronous cancel/error can change OTA state in this batch.
  // Switch radio mode before any progress-panel draw or the next host packet.
  updateOtaRadio();
  if(pendingFrame && !ota.active()) {
    memcpy(dashboard,incomingFrame,FRAME_SIZE);
    uint8_t result=criticalBattery()?6:0;
    if(!result) { hasFrame=true; lastCRC=incomingCRC; refreshRequestedAt=0; drawScreen(); if(!panelReady) result=5; }
    bleReply(result,incomingSequence,incomingCRC); pendingFrame=false;
    meterServer->updateConnParams(peerAddress,48,72,4,600);
    Serial.printf("BLE ACK %lu %08lx result=%u\n",(unsigned long)incomingSequence,(unsigned long)incomingCRC,result);
  }
  now=millis();
  if(receiving && expired(now,packetAt,15000)) receiving=false;
  if(refreshRequestedAt && expired(now,refreshRequestedAt,10000)) { refreshRequestedAt=0; uiDirty=true; }
  bool active=ota.active();
  if(active) {
    unsigned percent=ota.status.total?uint64_t(ota.status.offset)*100/ota.status.total:0;
    if(!wasOta || (ota.status.state>=OtaState::Image && percent>=otaDrawPercent+10 && expired(now,otaDrawAt,5000))) {
      drawScreen(); otaDrawAt=millis(); otaDrawPercent=percent;
    }
  } else {
    if(wasOta) uiDirty=true;
    otaDrawPercent=0;
    time_t minute=time(nullptr)/60;
    if(!receiving && (uiDirty || (!discovery.open && minute!=paintedMinute))) { drawScreen(); paintedMinute=minute; }
  }
  wasOta=active;
  bootHealth.retry(millis());
  if(expired(millis(),snapshotAt,100) || keys) { updateDeviceSnapshot(); snapshotAt=millis(); }
  if(uxQueueMessagesWaiting(packetQueue)) taskYIELD(); else delay(1);
}
void meterWorker(void *) { setupBluetooth(); while(true) bluetoothLoop(); }
