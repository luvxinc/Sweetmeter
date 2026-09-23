#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <vector>
uint32_t crc32(const uint8_t *bytes,size_t n) {
  uint32_t crc=0xffffffff;
  for(size_t i=0;i<n;++i) { crc^=bytes[i];for(int bit=0;bit<8;++bit)crc=(crc>>1)^((crc&1)?0xedb88320:0); }
  return ~crc;
}
#define SWEETMETER_NATIVE_TEST
#include "../firmware/src/ota_runtime.h"
#include "../firmware/src/discovery.h"
#include "../firmware/src/disconnect_sleep.h"
#include "fixtures/protocol4_fixture.h"
using namespace sweetmeter;
static OtaInputs context;
static std::vector<OtaStatus> events;
static bool wantCancel=false;
static OtaInputs input() { return context; }
static void output(const OtaStatus &s,bool) { events.push_back(s); }
static bool cancelled(uint32_t) { bool yes=wantCancel;wantCancel=false;return yes; }
struct Fixture {
 Preferences storage; BootHealth boot; OtaManager manager;
 Fixture():manager(boot,input,output,cancelled) {
  fake::reset(); context=OtaInputs{};context.authorized=context.connected=true;context.generation=1;
  events.clear();wantCancel=false;boot.initialize(storage,true);memcpy(fake::digest,protocol4_fixture_header+92,32);
 }
 void op(char c) { uint8_t p[5]={uint8_t(c)};put32(p+1,123);manager.control(p,sizeof(p)); }
 void begin() { uint8_t p[20]={'M'};put32(p+1,123);p[5]=sizeof(protocol4_fixture_envelope);put32(p+7,2026);put32(p+11,9);put32(p+15,1);p[19]=1;manager.control(p,20); }
 void metadata() {
  begin();
  for(size_t at=0;at<sizeof(protocol4_fixture_envelope);) {
   size_t count=sizeof(protocol4_fixture_envelope)-at;if(count>11)count=11;
   uint8_t p[20]={'m'};put32(p+1,123);put32(p+5,at);memcpy(p+9,protocol4_fixture_envelope+at,count);manager.control(p,count+9);at+=count;
  }
 }
 void prepared() { metadata();op('S');assert(manager.status.state==OtaState::Image); }
 void image() {
  prepared();
  for(size_t at=0;at<sizeof(protocol4_fixture_image);) {
   size_t count=sizeof(protocol4_fixture_image)-at;if(count>12)count=12;
   uint8_t p[20];put32(p,123);put32(p+4,at);memcpy(p+8,protocol4_fixture_image+at,count);manager.data(p,count+8);at+=count;
  }
 }
};
static void parserTests() {
 Metadata m;assert(parseEnvelope(protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Ok);
 assert(m.size==512 && m.version.sequence==2);
 assert(strictSignature(protocol4_fixture_signature,sizeof(protocol4_fixture_signature)));
 uint8_t bad[234];memcpy(bad,protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope));
 bad[15]=1;assert(parseEnvelope(bad,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Metadata);
 memcpy(bad,protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope));bad[60]=1;
 assert(parseEnvelope(bad,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Board);
 assert(parseEnvelope(protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope),{2026,9,2},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Version);
 assert(parseEnvelope(protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,8,100},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Companion);
 assert(compare({2026,10,1},{2026,9,999})>0);assert(compare({2027,1,1},{2026,12,999})>0);
 assert(!strictSignature((const uint8_t*)"12345678",8));
 const uint8_t nonminimal[]={0x30,7,2,2,0,1,2,1,1};assert(!strictSignature(nonminimal,sizeof(nonminimal)));
 OtaStatus status;status.state=OtaState::Image;status.session=0x12345678;status.offset=65538;status.total=100000;status.opcode='d';status.signature=true;
 uint8_t wire[20];status.encode(wire);assert(!memcmp(wire,protocol4_fixture_status,20));
}
static void otaTests() {
 { Fixture f;f.begin();assert(f.manager.status.state==OtaState::Metadata);f.op('S');assert(f.manager.status.error==OtaError::Incomplete && fake::begins==0); }
 { Fixture f;f.metadata();fake::validSignature=false;f.op('S');assert(f.manager.status.error==OtaError::Signature && fake::begins==0); }
 { Fixture f;context.battery=19;f.begin();assert(events.back().error==OtaError::Power && !f.manager.active()); }
 { Fixture f;f.prepared();uint8_t p[9]={};put32(p,123);put32(p+4,1);f.manager.data(p,9);assert(f.manager.status.error==OtaError::Offset && fake::aborts==1 && fake::writes==0); }
 { Fixture f;f.prepared();uint8_t p[9]={};put32(p,321);f.manager.data(p,9);assert(f.manager.status.state==OtaState::Image && events.back().error==OtaError::Session && fake::aborts==0); }
 { Fixture f;f.prepared();uint8_t p[9]={};put32(p,123);fake::writeError=1;f.manager.data(p,9);assert(f.manager.status.offset==0 && fake::aborts==1); }
 { Fixture f;f.image();fake::digest[0]^=1;f.op('F');assert(f.manager.status.error==OtaError::Digest && fake::ends==0 && fake::aborts==1 && fake::selections==0); }
 { Fixture f;f.image();fake::endError=ESP_ERR_OTA_VALIDATE_FAILED;f.op('F');assert(f.manager.status.error==OtaError::Image && fake::ends==1 && fake::aborts==0 && fake::selections==0); }
 { Fixture f;f.image();strcpy(fake::imageVersion,"2026.9.3");f.op('F');assert(f.manager.status.error==OtaError::Version && fake::selections==0); }
 { Fixture f;f.prepared();context.connected=false;f.manager.tick();assert(f.manager.status.error==OtaError::Disconnected && fake::aborts==1); }
 { Fixture f;f.prepared();++context.generation;f.manager.tick();assert(f.manager.status.error==OtaError::Disconnected && fake::aborts==1); }
 { Fixture f;f.prepared();fake::now+=30000;f.manager.tick();assert(f.manager.status.error==OtaError::Timeout && fake::aborts==1); }
 { Fixture f;f.prepared();context.critical=true;f.manager.tick();assert(f.manager.status.error==OtaError::Power && fake::aborts==1); }
 { Fixture f;f.prepared();f.op('X');assert(f.manager.status.state==OtaState::Cancelled && fake::aborts==1); }
 { Fixture f;f.metadata();fake::onBegin=[] {wantCancel=true;};f.op('S');assert(f.manager.status.state==OtaState::Cancelled && fake::aborts==1); }
 { Fixture f;f.metadata();fake::onBegin=[] {fake::now+=60000;};f.op('S');assert(f.manager.status.error==OtaError::Timeout && fake::aborts==1); }
 { Fixture f;f.image();fake::onEnd=[] {wantCancel=true;};f.op('F');assert(f.manager.status.state==OtaState::Cancelled && fake::aborts==0 && fake::selections==0); }
 { Fixture f;f.image();f.storage.fail=true;f.op('F');assert(f.manager.status.error==OtaError::Flash && fake::selections==0); }
 { Fixture f;f.image();fake::selectError=1;f.op('F');assert(f.manager.status.error==OtaError::Flash && fake::selected==0 && !strcmp(f.boot.lastUpdate,"failed")); }
 { Fixture f;f.image();fake::selectError=1;fake::selectionChangesOnError=true;f.op('F');assert(f.manager.status.error==OtaError::Flash && f.boot.recovery && !strcmp(f.boot.lastUpdate,"pending")); }
 { Fixture f;f.image();f.storage.failWrite=2;f.op('F');assert(f.manager.status.error==OtaError::Flash && fake::selected==0 && fake::marks==1); }
 { Fixture f;f.image();bool rebooted=false;try {f.op('F');}catch(Restart&){rebooted=true;} assert(rebooted && fake::selected==1 && f.manager.status.state==OtaState::Rebooting && f.boot.record.stage==UpdateStage::Selected); }
 // Q may inspect progress but does not keep a stalled session alive.
 { Fixture f;f.begin();fake::now+=29000;f.op('Q');fake::now+=1000;f.manager.tick();assert(f.manager.status.error==OtaError::Timeout); }
}
static void bootTests() {
 // Resolve the reset window after mark-valid but before persisting success.
 { Fixture f;Metadata m;m.version={2026,9,1};assert(f.boot.intent(m,&fake::slots[0]));BootHealth after;after.initialize(f.storage,true);assert(!strcmp(after.lastUpdate,"success")); }
 // Version mismatch alone or Intent alone is insufficient rollback evidence.
 { Fixture f;Metadata m;m.version={2026,9,2};assert(f.boot.intent(m,&fake::slots[1]));fake::states[1]=ESP_OTA_IMG_ABORTED;BootHealth after;after.initialize(f.storage,true);assert(!strcmp(after.lastUpdate,"pending")); }
 { Fixture f;Metadata m;m.version={2026,9,2};assert(f.boot.intent(m,&fake::slots[1]));assert(f.boot.save(UpdateStage::Selected));fake::states[1]=ESP_OTA_IMG_ABORTED;BootHealth after;after.initialize(f.storage,true);assert(!strcmp(after.lastUpdate,"rollback"));BootHealth next;next.initialize(f.storage,true);assert(!strcmp(next.lastUpdate,"rollback")); }
 // Every pending image must fail without a matching durable record.
 { Fixture f;fake::states[0]=ESP_OTA_IMG_PENDING_VERIFY;bool reset=false;try{BootHealth after;after.initialize(f.storage,true);}catch(Restart&){reset=true;}assert(reset && fake::states[0]==ESP_OTA_IMG_INVALID); }
 { Fixture f;Metadata m;m.version={2026,9,1};assert(f.boot.intent(m,&fake::slots[0]));fake::states[0]=ESP_OTA_IMG_PENDING_VERIFY;BootHealth after;after.initialize(f.storage,true);assert(after.pending);after.finish(true);assert(fake::marks==1 && !strcmp(after.lastUpdate,"success")); }
 // A failed success persistence retry must not relabel a newer transaction.
 { Fixture f;Metadata m;m.version={2026,9,1};assert(f.boot.intent(m,&fake::slots[0]));f.storage.fail=true;BootHealth after;after.initialize(f.storage,true);assert(!strcmp(after.lastUpdate,"pending"));f.storage.fail=false;m.version={2026,9,2};assert(after.intent(m,&fake::slots[1]));after.retry(6000);assert(after.record.stage==UpdateStage::Intent && !strcmp(after.lastUpdate,"pending")); }
}
static void radioTests() {
 OtaRadioPolicy radio;
 assert(radio.update(false,true)==RadioChange::None);
 assert(radio.update(true,true)==RadioChange::Fast);
 assert(radio.update(true,true)==RadioChange::None);
 assert(radio.update(false,true)==RadioChange::Idle);
 assert(radio.update(false,true)==RadioChange::None);
 assert(radio.update(true,true)==RadioChange::Fast);
 assert(radio.update(false,false)==RadioChange::None);
 assert(radio.update(false,true)==RadioChange::None);
 assert(radio.update(true,true)==RadioChange::Fast);
}
static void discoveryTests() {
 Discovery d;const char *id="7a1e1000-ff1b-4d9f-a023-0123456789ab";const uint8_t peer[6]={1,2,3,4,5,6};
 Registry none;
 uint8_t secret[32];for(int i=0;i<32;++i)secret[i]=uint8_t(i+1);
 d.begin(100,123,none);uint8_t body[72];memcpy(body,id,36);body[36]=3;memcpy(body+37,"Mac",3);memcpy(body+40,secret,32);
 uint8_t begin[11]={'J'};put32(begin+1,456);put32(begin+5,123);begin[9]=72;uint32_t sid,next;
 auto registerBody=[&](Discovery &target,uint32_t session,const uint8_t *bytes,size_t size,uint32_t now,const uint8_t *from)->uint8_t {
  uint8_t b[11]={'J'};put32(b+1,session);put32(b+5,target.nonce);b[9]=uint8_t(size);
  uint8_t r=target.handle(b,11,now,sid,next,from);if(r)return r;
  for(size_t at=0;at<size;) {uint8_t p[20]={'j'};put32(p+1,session);put32(p+5,at);size_t n=size-at;if(n>11)n=11;memcpy(p+9,bytes+at,n);r=target.handle(p,n+9,now,sid,next,from);if(r)return r;at+=n;}
  uint8_t k[5]={'K'};put32(k+1,session);return target.handle(k,5,now,sid,next,from);
 };
 assert(d.handle(begin,11,100,sid,next,peer)==0 && next==0);
 for(size_t at=0;at<72;) {uint8_t p[20]={'j'};put32(p+1,456);put32(p+5,at);size_t n=72-at;if(n>11)n=11;memcpy(p+9,body+at,n);assert(d.handle(p,n+9,101,sid,next,peer)==0);at+=n;}
 uint8_t commit[5]={'K'};put32(commit+1,456);assert(d.handle(commit,5,102,sid,next,peer)==0 && next==72 && d.count==1 && !strcmp(d.computers[0].id,id));
 assert(d.computers[0].hasSecret && !memcmp(d.computers[0].secret,secret,32));
 // A J announcing a length outside both body ranges is malformed.
 begin[9]=58;assert(d.handle(begin,11,103,sid,next,peer)==RegMalformed);
 begin[9]=69;assert(d.handle(begin,11,103,sid,next,peer)==RegMalformed);
 begin[9]=37;assert(d.handle(begin,11,103,sid,next,peer)==RegMalformed);
 // Same secret re-registration updates; a second secret for one ID is a conflict.
 assert(registerBody(d,500,body,72,104,peer)==RegOk && d.count==1);
 const uint8_t other[6]={9,9,9,9,9,9};uint8_t forged[72];memcpy(forged,body,72);forged[71]^=0x55;
 assert(registerBody(d,501,forged,72,105,other)==RegConflict && d.computers[0].conflict && !d.selectable(0));
 assert(!memcmp(d.computers[0].secret,secret,32));
 // A zero secret is malformed.
 uint8_t zero[72];memcpy(zero,body,40);memset(zero+40,0,32);zero[35]='c';
 assert(registerBody(d,502,zero,72,106,other)==RegMalformed);
 // One sender cannot fill the list.
 Discovery s;s.begin(0,9,none);
 for(int i=0;i<3;++i){uint8_t b[72];memcpy(b,body,72);b[35]=uint8_t('0'+i);assert(registerBody(s,600+i,b,72,1,peer)==(i<2?RegOk:RegRateLimited));}
 assert(s.count==2);
 // Only an address that completed a registration counts for the menu-race
 // bond grace; a failed attempt (conflict, malformed) does not.
 assert(d.registeredPeer(peer) && !d.registeredPeer(other));
 assert(d.remaining(60100)==0);d.tick(60100);assert(!d.open);
 assert(d.registeredPeer(peer));  // still known right after the window closed
 // Paired computers are listed first (selected, then most recent) with their
 // stored secrets; a paired computer that re-registers with a different secret
 // is marked and needs an explicit confirmation before it can be switched to.
 Registry paired;uint8_t stored[32];memset(stored,7,32);
 int mac=paired.add(id,"Mac",stored,true);int pc=paired.add("7a1e1000-ff1b-4d9f-a023-00000000000a","Office",stored,true);
 paired.add("7a1e1000-ff1b-4d9f-a023-00000000000b","Old",stored,true);paired.select(mac);paired.select(pc);paired.select(mac);
 d.begin(10,7,paired);assert(d.count==3 && !d.registeredPeer(peer) && d.computers[0].paired==mac && !strcmp(d.computers[1].name,"Office") && !strcmp(d.computers[2].name,"Old"));
 assert(d.computers[0].hasSecret && d.computers[0].secret[0]==7 && !d.computers[0].registered);
 assert(d.selectable(0) && !d.needsKeyConfirmation(0));
 assert(registerBody(d,700,body,72,11,peer)==RegOk && d.count==3 && !memcmp(d.computers[0].secret,secret,32) && d.computers[0].registered);
 assert(d.computers[0].keyChanged && d.needsKeyConfirmation(0) && d.selectable(0));
 // Removing a registry entry renumbers later rows.
 d.removeAt(1,true);assert(d.count==2 && d.computers[1].paired==1 && !strcmp(d.computers[1].name,"Old"));
 put32(begin+1,456);put32(begin+5,7);begin[9]=72;assert(d.handle(begin,11,10,sid,next,peer)==0);d.tick(5020);assert(d.handle(commit,5,5021,sid,next,peer)==RegSession);
 assert(!hostId((const uint8_t*)"7a1e1000-ff1b-4d9f-a023-0123456789aZ",36));
 DisconnectSleep sleep;sleep.freshGrace(1);assert(!sleep.counting());sleep.targetConnected();sleep.freshGrace(500);assert(!sleep.expired(30499));assert(sleep.expired(30500));
}
static void registrationVariantTests() {
 const char *id="7a1e1000-ff1b-4d9f-a023-0123456789ab";const uint8_t peer[6]={1,2,3,4,5,6};uint32_t sid,next;
 auto registerBody=[&](Discovery &target,uint32_t session,const uint8_t *bytes,size_t size,const uint8_t *from)->uint8_t {
  uint8_t b[11]={'J'};put32(b+1,session);put32(b+5,target.nonce);b[9]=uint8_t(size);
  uint8_t r=target.handle(b,11,1,sid,next,from);if(r)return r;
  for(size_t at=0;at<size;) {uint8_t p[20]={'j'};put32(p+1,session);put32(p+5,at);size_t n=size-at;if(n>11)n=11;memcpy(p+9,bytes+at,n);r=target.handle(p,n+9,1,sid,next,from);if(r)return r;at+=n;}
  uint8_t k[5]={'K'};put32(k+1,session);return target.handle(k,5,1,sid,next,from);
 };
 uint8_t secret[32];memset(secret,0x21,32);
 uint8_t modern[72];memcpy(modern,id,36);modern[36]=3;memcpy(modern+37,"Mac",3);memcpy(modern+40,secret,32);
 uint8_t legacy[45];memcpy(legacy,id,36);legacy[36]=8;memcpy(legacy+37,"Old Mac!",8);
 { // An old companion's 38..57-byte body is recorded as "update the app", never paired.
  Registry none;Discovery d;d.begin(0,11,none);
  assert(registerBody(d,1,legacy,45,peer)==RegOutdated && next==45 && d.count==1);
  assert(d.computers[0].outdated && !d.selectable(0) && !strcmp(d.computers[0].name,"Old Mac!") && !d.computers[0].hasSecret);
  // It retries: the row is updated, not duplicated, and never counts as a registration.
  for(int i=0;i<3;++i) assert(registerBody(d,2+i,legacy,45,peer)==RegOutdated && d.count==1);
  // Malformed legacy bodies are still malformed.
  uint8_t bad[45];memcpy(bad,legacy,45);bad[36]=9;assert(registerBody(d,9,bad,45,peer)==RegMalformed);
  memcpy(bad,legacy,45);bad[40]=0x01;assert(registerBody(d,10,bad,45,peer)==RegMalformed);
  // After updating, the same computer registers with a secret and becomes selectable.
  assert(registerBody(d,20,modern,72,peer)==RegOk && d.count==1 && !d.computers[0].outdated && d.selectable(0));
  // A late old-app registration cannot hide a registration with a secret.
  const uint8_t other[6]={7};assert(registerBody(d,21,legacy,45,other)==RegOutdated && !d.computers[0].outdated && d.selectable(0));
 }
 { // A paired computer running an old app can still be switched back to.
  Registry paired;int index=paired.add(id,"Mac",secret,true);paired.select(index);
  Discovery d;d.begin(0,12,paired);
  assert(registerBody(d,1,legacy,45,peer)==RegOutdated && d.count==1 && d.computers[0].outdated && d.selectable(0));
 }
 { // Same-secret re-registration of a paired computer needs no confirmation;
   // a different one does, and a third different secret is a conflict.
  Registry paired;int index=paired.add(id,"Mac",secret,true);paired.select(index);
  Discovery d;d.begin(0,13,paired);
  assert(registerBody(d,1,modern,72,peer)==RegOk && d.computers[0].registered && !d.computers[0].keyChanged && !d.needsKeyConfirmation(0));
  Discovery e;e.begin(0,14,paired);uint8_t fresh[72];memcpy(fresh,modern,72);memset(fresh+40,0x42,32);
  assert(registerBody(e,1,fresh,72,peer)==RegOk && e.computers[0].keyChanged && e.needsKeyConfirmation(0));
  assert(!memcmp(e.computers[0].secret,fresh+40,32) && paired.entries[index].secret[0]==0x21);  // stored secret untouched
  const uint8_t other[6]={8};uint8_t third[72];memcpy(third,modern,72);memset(third+40,0x43,32);
  assert(registerBody(e,2,third,72,other)==RegConflict && e.computers[0].conflict && !e.selectable(0) && !e.needsKeyConfirmation(0));
  // A legacy selection without a secret gains one without a confirmation.
  Registry migrated;migrated.migrateLegacy(id,36,"Mac");Discovery m;m.begin(0,15,migrated);
  assert(registerBody(m,1,modern,72,peer)==RegOk && !m.computers[0].keyChanged && m.selectable(0));
 }
}
static void keyTableTests() {
 // The header's key ID selects its own embedded public key.
 uint8_t envelope[234];memcpy(envelope,protocol4_fixture_envelope,sizeof(protocol4_fixture_envelope));
 Metadata m;assert(parseEnvelope(envelope,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Ok && m.keyIndex==0);
 envelope[132]='2';assert(parseEnvelope(envelope,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Ok && m.keyIndex==1);
 envelope[132]='3';assert(parseEnvelope(envelope,sizeof(protocol4_fixture_envelope),{2026,9,1},{2026,9,1},0x330000,SWEETMETER_TRUSTED_KEY_IDS,SWEETMETER_TRUSTED_KEY_COUNT,m)==OtaError::Signature);
 { Fixture f;f.metadata();f.op('S');assert(f.manager.status.state==OtaState::Image && fake::parsedKey==(const unsigned char*)SWEETMETER_TRUSTED_KEY_PEMS[0]); }
}
int main() { parserTests();otaTests();bootTests();radioTests();discoveryTests();registrationVariantTests();keyTableTests();puts("OTA firmware state, failure, discovery and rollback tests passed"); }
