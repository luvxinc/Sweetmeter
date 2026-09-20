#pragma once
#ifdef SWEETMETER_NATIVE_TEST
#include "firmware_stubs.h"
#else
#include <esp_ota_ops.h>
#include <esp_timer.h>
#include <mbedtls/pk.h>
#include <mbedtls/sha256.h>
#include <Preferences.h>
#include "ota_protocol.h"
#include "sweetmeter_release.h"
#endif
#include "ota_protocol.h"

#ifndef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
#error Sweetmeter requires a rollback-enabled SDK and bootloader
#endif
#if (SWEETMETER_TEST_HEALTH_FAIL || SWEETMETER_TEST_RESET_BEFORE_CONFIRM) && !SWEETMETER_TEST_BUILD
#error Hardware acceptance hooks require an explicit test build
#endif
#if defined(CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK) || defined(CONFIG_SECURE_BOOT)
#error This bootstrap must not enable irreversible eFuse security settings
#endif

namespace sweetmeter {
constexpr Version runningVersion{SWEETMETER_VERSION_YEAR,SWEETMETER_VERSION_MONTH,SWEETMETER_VERSION_SEQUENCE};
enum class UpdateStage : uint32_t { Intent=1, Selected, Booted, Success, Failed, Rollback };
struct UpdateRecord {
  uint32_t magic=0x34574d53; UpdateStage stage=UpdateStage::Intent;
  Version version{}; uint32_t address=0; uint8_t digest[32]{}; uint32_t checksum=0;
};
static_assert(sizeof(UpdateRecord)==60,"Update record format must remain stable");
inline uint32_t recordCrc(const UpdateRecord &r) {
  return ::crc32(reinterpret_cast<const uint8_t*>(&r),offsetof(UpdateRecord,checksum));
}
class BootHealth {
 public:
  const char *health="valid", *lastUpdate="none"; char target[24]{};
  bool pending=false, recovery=false, nvsReady=false; Preferences *prefs=nullptr;
  UpdateRecord record{}; bool hasRecord=false;
  bool save(UpdateStage stage) {
    UpdateRecord next=record; next.stage=stage; next.checksum=recordCrc(next);
    if(!nvsReady || prefs->putBytes("update",&next,sizeof(next))!=sizeof(next)) return false;
    UpdateRecord check{};
    if(prefs->getBytes("update",&check,sizeof(check))!=sizeof(check) || memcmp(&check,&next,sizeof(next))) return false;
    record=next; hasRecord=true; return true;
  }
  bool intent(const Metadata &m,const esp_partition_t *partition) {
    UpdateRecord previous=record; bool previouslyPresent=hasRecord;
    record={}; record.version=m.version; record.address=partition->address;
    memcpy(record.digest,m.digest,32);
    if(!save(UpdateStage::Intent)) { record=previous; hasRecord=previouslyPresent; return false; }
    // A retry belongs to its old transaction and must never relabel this intent.
    retrySuccess_=retryRollback_=false;
    versionText(m.version,target,sizeof(target)); lastUpdate="pending"; return true;
  }
  void initialize(Preferences &storage,bool available) {
    prefs=&storage; nvsReady=available;
    if(available && prefs->getBytesLength("update")==sizeof(record) &&
       prefs->getBytes("update",&record,sizeof(record))==sizeof(record) &&
       record.magic==0x34574d53 && recordCrc(record)==record.checksum &&
       validVersion(record.version) && uint32_t(record.stage)>=1 && uint32_t(record.stage)<=6) {
      hasRecord=true; versionText(record.version,target,sizeof(target)); lastUpdate="pending";
    }
    const esp_partition_t *running=esp_ota_get_running_partition();
    esp_ota_img_states_t state=ESP_OTA_IMG_UNDEFINED;
    bool gotState=running && esp_ota_get_state_partition(running,&state)==ESP_OK;
    if(gotState && state==ESP_OTA_IMG_PENDING_VERIFY) {
      pending=true; health="pending";
      // A timer task independently resets a candidate if init/display/BLE hangs.
      esp_timer_create_args_t args{}; args.callback=[](void*){ esp_restart(); }; args.name="ota-health";
      if(esp_timer_create(&args,&watchdog_)!=ESP_OK || esp_timer_start_once(watchdog_,30000000)!=ESP_OK) fail();
      if(!hasRecord || running->address!=record.address || compare(record.version,runningVersion)!=0 || !save(UpdateStage::Booted)) fail();
#if SWEETMETER_TEST_RESET_BEFORE_CONFIRM
      // Hardware acceptance build only: production tooling rejects this flag.
      Serial.println("OTA TEST RESET_BEFORE_CONFIRM"); Serial.flush(); esp_restart();
#endif
      return;
    }
    if(!hasRecord) return;
    if(running && running->address==record.address && compare(record.version,runningVersion)==0 && gotState && state==ESP_OTA_IMG_VALID) {
      if(save(UpdateStage::Success)) lastUpdate="success"; else retrySuccess_=true;
      return;
    }
    if(record.stage==UpdateStage::Failed) { lastUpdate="failed"; return; }
    if(record.stage==UpdateStage::Rollback) { lastUpdate="rollback"; return; }
    const esp_partition_t *candidate=targetPartition();
    esp_ota_img_states_t candidateState=ESP_OTA_IMG_UNDEFINED;
    if(compare(runningVersion,record.version)<0 && candidate &&
       esp_ota_get_state_partition(candidate,&candidateState)==ESP_OK &&
       (candidateState==ESP_OTA_IMG_INVALID || candidateState==ESP_OTA_IMG_ABORTED) &&
       (record.stage==UpdateStage::Selected || record.stage==UpdateStage::Booted)) {
      if(save(UpdateStage::Rollback)) lastUpdate="rollback"; else retryRollback_=true;
    }
  }
  void finish(bool localChecks) {
    if(!pending) { if(!localChecks) health="failed"; return; }
#if SWEETMETER_TEST_HEALTH_FAIL
    localChecks=false;
#endif
    if(!localChecks || !nvsReady) fail();
    if(esp_ota_mark_app_valid_cancel_rollback()!=ESP_OK) fail();
    pending=false; health="valid";
    if(watchdog_) { esp_timer_stop(watchdog_); esp_timer_delete(watchdog_); watchdog_=nullptr; }
    if(save(UpdateStage::Success)) lastUpdate="success"; else { lastUpdate="pending"; retrySuccess_=true; }
    Serial.printf("OTA HEALTH valid version=%s persisted=%s\n",SWEETMETER_VERSION,lastUpdate);
  }
  void retry(uint32_t now) {
    if(!expired(now,retryAt_,5000)) return;
    retryAt_=now;
    if(retrySuccess_ && save(UpdateStage::Success)) { retrySuccess_=false; lastUpdate="success"; }
    if(retryRollback_ && save(UpdateStage::Rollback)) { retryRollback_=false; lastUpdate="rollback"; }
  }
  [[noreturn]] void fail() {
    health="failed"; Serial.println("OTA HEALTH failed; rolling back"); Serial.flush();
    // Keep Selected/Booted evidence so the previous application can prove rollback.
    esp_err_t error=esp_ota_mark_app_invalid_rollback_and_reboot();
    Serial.printf("OTA ROLLBACK error=%d; awaiting recovery\n",int(error));
    // No alternate boot slot: do not confirm or run the defective candidate.
    while(true) delay(1000);
  }
 private:
  esp_timer_handle_t watchdog_=nullptr; uint32_t retryAt_=0; bool retrySuccess_=false,retryRollback_=false;
  const esp_partition_t *targetPartition() {
    for(int slot=0;slot<16;++slot) {
      const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_APP,esp_partition_subtype_t(ESP_PARTITION_SUBTYPE_APP_OTA_0+slot),nullptr);
      if(p && p->address==record.address) return p;
    }
    return nullptr;
  }
};
struct OtaInputs {
  bool authorized=false,connected=false,menu=false,frameBusy=false,critical=false;
  int battery=-1; uint32_t generation=0;
};
class OtaManager {
 public:
  OtaStatus status{}; BootHealth &boot;
  OtaInputs (*inputs)(); void (*publish)(const OtaStatus&,bool); bool (*cancelWaiting)(uint32_t);
  bool exited=false, commitCritical=false;
  OtaManager(BootHealth &b,OtaInputs(*i)(),void(*p)(const OtaStatus&,bool),bool(*c)(uint32_t)) : boot(b),inputs(i),publish(p),cancelWaiting(c) {}
  bool active() const { return status.active(); }
  void malformed(uint32_t sid,uint8_t opcode) { invalid(OtaError::Malformed,sid,opcode); }
  void rejection(OtaError error,uint32_t sid,uint8_t opcode) {
    OtaStatus rejected=status; rejected.session=sid; rejected.opcode=opcode; rejected.error=error;
    publish(rejected,false);
  }
  void control(const uint8_t *p,size_t n) {
    const uint8_t op=n?p[0]:0; const uint32_t sid=n>=5?u32(p+1):0;
    OtaInputs ctx=inputs();
    if(n<5) { rejection(OtaError::Malformed,0,op); return; }
    if(!ctx.authorized || !ctx.connected) { rejection(OtaError::Unauthorized,sid,op); return; }
    if(!sid || n>maxPacket) { invalid(OtaError::Malformed,sid,op); return; }
    if(active() && sid!=status.session) { rejection(op=='M'?OtaError::Busy:OtaError::Session,sid,op); return; }
    if(op=='M') { begin(p,n,ctx); return; }
    if(sid!=status.session || !status.session) { rejection(OtaError::Session,sid,op); return; }
    if(op=='Q') {
      if(n!=5) { invalid(OtaError::Malformed,sid,op); return; }
      status.opcode=op; publish(status,true); return;
    }
    if(op=='X') {
      if(n!=5) { invalid(OtaError::Malformed,sid,op); return; }
      if(!status.cancellable() || commitCritical) { rejection(OtaError::State,sid,op); return; }
      cancel(); return;
    }
    if(op=='m') {
      if(status.state!=OtaState::Metadata) { invalid(OtaError::State,sid,op); return; }
      if(n<=9) { invalid(OtaError::Malformed,sid,op); return; }
      if(u32(p+5)!=status.offset) { invalid(OtaError::Offset,sid,op); return; }
      if(n-9>status.total-status.offset) { invalid(OtaError::Malformed,sid,op); return; }
      memcpy(envelope_+status.offset,p+9,n-9); status.offset+=n-9; lastAt_=millis(); emit(op); return;
    }
    if(op=='S') {
      if(n!=5) { invalid(OtaError::Malformed,sid,op); return; }
      if(status.state!=OtaState::Metadata) { invalid(OtaError::State,sid,op); return; }
      if(status.offset!=status.total) { terminal(OtaError::Incomplete,op); return; }
      prepare(); return;
    }
    if(op=='F') {
      if(n!=5) { invalid(OtaError::Malformed,sid,op); return; }
      if(status.state!=OtaState::Image) { invalid(OtaError::State,sid,op); return; }
      if(status.offset!=status.total) { terminal(OtaError::Incomplete,op); return; }
      finish(); return;
    }
    invalid(OtaError::Malformed,sid,op);
  }
  void data(const uint8_t *p,size_t n) {
    uint32_t sid=n>=4?u32(p):0;
    OtaInputs ctx=inputs();
    if(n<4) { rejection(OtaError::Malformed,0,'d'); return; }
    if(!ctx.authorized || !ctx.connected) { rejection(OtaError::Unauthorized,sid,'d'); return; }
    if(!sid) { invalid(OtaError::Malformed,sid,'d'); return; }
    if(sid!=status.session) { rejection(OtaError::Session,sid,'d'); return; }
    if(n<=8 || n>maxPacket) { invalid(OtaError::Malformed,sid,'d'); return; }
    if(status.state!=OtaState::Image) { invalid(OtaError::State,sid,'d'); return; }
    if(u32(p+4)!=status.offset) { terminal(OtaError::Offset,'d'); return; }
    if(n-8>status.total-status.offset) { terminal(OtaError::Malformed,'d'); return; }
    if(interrupted()) return;
    esp_err_t written=esp_ota_write(handle_,p+8,n-8);
    if(written!=ESP_OK || mbedtls_sha256_update_ret(&hash_,p+8,n-8)!=0) {
      terminal(written==ESP_ERR_OTA_VALIDATE_FAILED?OtaError::Image:OtaError::Flash,'d'); return;
    }
    status.offset+=n-8; lastAt_=millis(); emit('d');
  }
  void tick() { if(status.cancellable()) interrupted(); }
  void cancel() { cleanup(); status.state=OtaState::Cancelled; status.error=OtaError::Ok; exited=true; emit('X'); }
  void terminal(OtaError error,uint8_t opcode) {
    cleanup(); status.state=OtaState::Error; status.error=error; exited=true; emit(opcode);
  }
 private:
  uint8_t envelope_[maxEnvelope]{}; Metadata metadata_{}; Version companion_{};
  uint32_t startedAt_=0,lastAt_=0,operationAt_=0,generation_=0;
  const esp_partition_t *partition_=nullptr; esp_ota_handle_t handle_=0;
  bool ownsHandle_=false,hashReady_=false; mbedtls_sha256_context hash_{};
  void emit(uint8_t opcode) { status.opcode=opcode; publish(status,true); }
  void invalid(OtaError error,uint32_t sid,uint8_t opcode) {
    if(active() && sid==status.session && !commitCritical) terminal(error,opcode);
    else rejection(error,sid,opcode);
  }
  void cleanup() {
    if(ownsHandle_) { esp_ota_abort(handle_); ownsHandle_=false; handle_=0; }
    if(hashReady_) { mbedtls_sha256_free(&hash_); hashReady_=false; }
  }
  bool interrupted() {
    OtaInputs ctx=inputs(); uint32_t now=millis();
    if(!ctx.connected || ctx.generation!=generation_) { terminal(OtaError::Disconnected,status.opcode); return true; }
    if(ctx.critical) { terminal(OtaError::Power,status.opcode); return true; }
    uint32_t limit=status.state==OtaState::Preparing?60000:30000;
    uint32_t reference=(status.state==OtaState::Preparing || status.state==OtaState::Verifying)?operationAt_:lastAt_;
    if(expired(now,startedAt_,7200000) || expired(now,reference,limit)) { terminal(OtaError::Timeout,status.opcode); return true; }
    if(cancelWaiting(status.session)) { cancel(); return true; }
    return false;
  }
  void begin(const uint8_t *p,size_t n,const OtaInputs &ctx) {
    uint32_t sid=u32(p+1);
    if(active()) { terminal(OtaError::State,'M'); return; }
    if(ctx.menu || ctx.frameBusy || boot.recovery || boot.pending) { rejection(OtaError::Busy,sid,'M'); return; }
    if(n!=20 || u16(p+5)<170 || u16(p+5)>maxEnvelope || (p[19]&~1)) {
      rejection(OtaError::Malformed,sid,'M'); return;
    }
    Version companion=versionAt(p+7);
    if(!validVersion(companion)) { rejection(OtaError::Companion,sid,'M'); return; }
    if(!(p[19]&1) || ctx.critical || (ctx.battery>=0 && ctx.battery<20)) { rejection(OtaError::Power,sid,'M'); return; }
    cleanup(); status={}; status.state=OtaState::Metadata; status.session=sid; status.total=u16(p+5);
    companion_=companion; generation_=ctx.generation; startedAt_=lastAt_=millis(); exited=false;
    memset(envelope_,0,sizeof(envelope_)); emit('M');
  }
  bool signatureValid() {
    mbedtls_pk_context key; mbedtls_pk_init(&key); uint8_t digest[32];
    bool ok=strictSignature(envelope_+162,u16(envelope_+160)) && mbedtls_sha256_ret(envelope_,headerSize,digest,0)==0 &&
      mbedtls_pk_parse_public_key(&key,reinterpret_cast<const unsigned char*>(SWEETMETER_PUBLIC_KEY_PEM),sizeof(SWEETMETER_PUBLIC_KEY_PEM))==0 &&
      mbedtls_pk_can_do(&key,MBEDTLS_PK_ECDSA) && mbedtls_pk_ec(key)->grp.id==MBEDTLS_ECP_DP_SECP256R1 &&
      mbedtls_pk_verify(&key,MBEDTLS_MD_SHA256,digest,sizeof(digest),envelope_+162,u16(envelope_+160))==0;
    mbedtls_pk_free(&key); return ok;
  }
  void prepare() {
    partition_=esp_ota_get_next_update_partition(nullptr);
    if(!partition_ || partition_->address==esp_ota_get_running_partition()->address) { terminal(OtaError::Size,'S'); return; }
    OtaError error=parseEnvelope(envelope_,status.total,runningVersion,companion_,partition_->size,SWEETMETER_TRUSTED_KEY_ID,metadata_);
    if(error!=OtaError::Ok) { terminal(error,'S'); return; }
    if(!signatureValid()) { terminal(OtaError::Signature,'S'); return; }
    status.signature=true;
    if(interrupted()) return;
    status.state=OtaState::Preparing; status.offset=0; status.total=metadata_.size; operationAt_=millis(); emit('S');
    esp_err_t result=esp_ota_begin(partition_,metadata_.size,&handle_);
    ownsHandle_=result==ESP_OK;
    if(interrupted()) return;
    if(result!=ESP_OK) { terminal(OtaError::Flash,'S'); return; }
    mbedtls_sha256_init(&hash_); hashReady_=true;
    if(mbedtls_sha256_starts_ret(&hash_,0)!=0) { terminal(OtaError::Flash,'S'); return; }
    status.state=OtaState::Image; lastAt_=millis(); emit('S');
  }
  bool restoreRunning() {
    const esp_partition_t *running=esp_ota_get_running_partition(), *selected=esp_ota_get_boot_partition();
    if(running && selected && selected->address==running->address) return true;
    if(!running || esp_ota_set_boot_partition(running)!=ESP_OK) return false;
    selected=esp_ota_get_boot_partition();
    return selected && selected->address==running->address && esp_ota_mark_app_valid_cancel_rollback()==ESP_OK;
  }
  void selectionFailed() {
    bool restored=restoreRunning(); boot.recovery=!restored;
    if(restored && boot.save(UpdateStage::Failed)) boot.lastUpdate="failed";
    else boot.lastUpdate="pending";
    commitCritical=false; terminal(OtaError::Flash,'F');
  }
  void finish() {
    status.state=OtaState::Verifying; operationAt_=millis(); emit('F');
    uint8_t digest[32];
    if(mbedtls_sha256_finish_ret(&hash_,digest)!=0) { terminal(OtaError::Flash,'F'); return; }
    if(memcmp(digest,metadata_.digest,32)) { terminal(OtaError::Digest,'F'); return; }
    if(interrupted()) return;
    esp_err_t result=esp_ota_end(handle_); ownsHandle_=false; handle_=0;
    if(result!=ESP_OK) { terminal(result==ESP_ERR_OTA_VALIDATE_FAILED?OtaError::Image:OtaError::Flash,'F'); return; }
    if(interrupted()) return;
    esp_app_desc_t description{}; char expectedVersion[24]; versionText(metadata_.version,expectedVersion,sizeof(expectedVersion));
    if(esp_ota_get_partition_description(partition_,&description)!=ESP_OK) { terminal(OtaError::Image,'F'); return; }
    if(strnlen(description.version,sizeof(description.version))==sizeof(description.version) || strcmp(description.version,expectedVersion)) {
      terminal(OtaError::Version,'F'); return;
    }
    if(!boot.intent(metadata_,partition_)) { terminal(OtaError::Flash,'F'); return; }
    if(interrupted()) return;
    commitCritical=true;
    if(esp_ota_set_boot_partition(partition_)!=ESP_OK) { selectionFailed(); return; }
    if(!boot.save(UpdateStage::Selected)) { selectionFailed(); return; }
    cleanup(); status.state=OtaState::Rebooting; status.offset=status.total; emit('F');
    Serial.printf("OTA REBOOT target=%s slot=0x%lx\n",boot.target,(unsigned long)partition_->address);
    delay(500); esp_restart();
  }
};
}
