#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
uint32_t crc32(const uint8_t *bytes,size_t n) {
  uint32_t crc=0xffffffff;
  for(size_t i=0;i<n;++i) { crc^=bytes[i];for(int bit=0;bit<8;++bit)crc=(crc>>1)^((crc&1)?0xedb88320:0); }
  return ~crc;
}
#define SWEETMETER_NATIVE_TEST
#include "../firmware/src/pairing.h"
using namespace sweetmeter;

static const char *hostA="7a1e1000-ff1b-4d9f-a023-0123456789ab";
static const char *hostB="7a1e1000-ff1b-4d9f-a023-00000000000b";
static const char serial[]="a1b2c3d4e5f6";

static void proofMatchesCompanion() {
  // Vector from Python: hmac.new(bytes(range(1,33)), b"SWM-AUTH-1"+bytes(range(0xa0,0xb0))
  //   + b"a1b2c3d4e5f6" + host, hashlib.sha256).digest()[:16]
  uint8_t secret[32],challenge[16],proof[16];
  for(int i=0;i<32;++i) secret[i]=uint8_t(i+1);
  for(int i=0;i<16;++i) challenge[i]=uint8_t(0xa0+i);
  assert(computeProof(secret,challenge,serial,hostA,proof));
  char hex[33];hexEncode(proof,16,hex);
  assert(!strcmp(hex,"18dfddf998637f8fbb7564618d611077"));
  uint8_t other[16];memcpy(other,proof,16);other[15]^=1;
  assert(constantTimeEqual(proof,proof,16) && !constantTimeEqual(proof,other,16));
}
static void helloPacket(uint8_t *packet,const uint8_t *secret,const uint8_t *challenge,const char *host) {
  packet[0]='P';assert(computeProof(secret,challenge,serial,host,packet+1));
}
static void linkAuthorization() {
  uint8_t secretA[32],secretB[32],challenge[16],packet[17];
  memset(secretA,0x11,32);memset(secretB,0x22,32);memset(challenge,0x5a,16);
  Registry registry;int a=registry.add(hostA,"Mac",secretA,true);int b=registry.add(hostB,"PC",secretB,true);registry.select(a);
  { // Selected computer: authorized once; the challenge is not reusable on this link.
    LinkAuth link;helloPacket(packet,secretA,challenge,hostA);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    assert(o.result==helloOk && o.changed && !o.drop && link.authorized());
    assert(link.proofHello(packet,17,registry,challenge,serial,false,false).result==helloOk);
  }
  { // Paired but not selected: told so, dropped, never authorized.
    LinkAuth link;helloPacket(packet,secretB,challenge,hostB);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    assert(o.result==helloNotSelected && o.drop && !link.authorized());
    registry.select(b);LinkAuth next;assert(next.proofHello(packet,17,registry,challenge,serial,false,false).result==helloOk);
    registry.select(a);
  }
  { // Wrong secret, replayed proof for another challenge, menu open, bad length.
    LinkAuth link;uint8_t wrong[32];memset(wrong,0x33,32);helloPacket(packet,wrong,challenge,hostA);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    assert(o.result==helloRejected && o.drop && !link.authorized());
    helloPacket(packet,secretA,challenge,hostA);
    assert(link.proofHello(packet,17,registry,challenge,serial,false,false).result==helloRejected);  // one attempt per link
    LinkAuth replay;uint8_t fresh[16];memset(fresh,0x5b,16);
    assert(replay.proofHello(packet,17,registry,fresh,serial,false,false).result==helloRejected);
    LinkAuth menu;assert(menu.proofHello(packet,17,registry,challenge,serial,true,false).result==helloRejected);
    LinkAuth shortPacket;assert(shortPacket.proofHello(packet,16,registry,challenge,serial,false,false).result==helloRejected);
  }
  { // Knowing the selected host ID is no longer enough.
    LegacyWindow window;LinkAuth link;uint8_t legacy[37]={'H'};memcpy(legacy+1,hostA,36);
    AuthOutcome o=link.legacyHello(legacy,37,registry,false,false,window,0);
    assert(o.result==helloRejected && o.drop && !link.authorized());
  }
  { // Hello during OTA never revokes or changes authorization.
    LegacyWindow window;LinkAuth link;helloPacket(packet,secretA,challenge,hostA);
    assert(link.proofHello(packet,17,registry,challenge,serial,false,false).result==helloOk);
    uint8_t legacy[37]={'H'};memcpy(legacy+1,hostB,36);
    AuthOutcome o=link.legacyHello(legacy,37,registry,false,true,window,0);
    assert(o.result==helloOk && !o.drop && link.authorized());
    LinkAuth idle;o=idle.legacyHello(legacy,37,registry,false,true,window,0);
    assert(o.result==helloRejected && !o.drop && idle.state==LinkAuth::State::Open);
  }
}
static void legacyMigration() {
  Registry registry;registry.migrateLegacy(hostA,36,"Mac");
  assert(registry.count==1 && registry.selected==0 && !registry.current()->hasSecret);
  uint8_t legacy[40]={'H'};memcpy(legacy+1,hostA,36);memcpy(legacy+37,"Mac",3);
  LegacyWindow window;
  { // Another host ID is refused and does not use up the migration.
    LinkAuth link;uint8_t other[37]={'H'};memcpy(other+1,hostB,36);
    assert(link.legacyHello(other,37,registry,false,false,window,0).result==helloRejected);
  }
  LinkAuth link;
  AuthOutcome o=link.legacyHello(legacy,40,registry,false,false,window,0);
  assert(o.result==helloProvision && !o.drop && !link.authorized());
  uint8_t y[33]={'Y'};
  assert(!link.provisionAllowed(y,33));  // all-zero secret
  memset(y+1,0x44,32);assert(link.provisionAllowed(y,33) && !link.provisionAllowed(y,32));
  o=link.provisioned(true);assert(o.result==helloOk && o.changed && link.authorized());
  // The first matching legacy hello consumes the migration window.
  LinkAuth second;assert(second.legacyHello(legacy,40,registry,false,false,window,0).result==helloRejected);
  LegacyWindow retry;LinkAuth failed;failed.legacyHello(legacy,40,registry,false,false,retry,0);o=failed.provisioned(false);
  assert(o.result==helloStoreFailed && o.drop && !failed.authorized());
  { // Only within ten minutes of boot; afterwards the owner must pair via the menu.
    LegacyWindow late;assert(late.open(LegacyWindow::windowMs-1));
    LinkAuth link;assert(link.legacyHello(legacy,40,registry,false,false,late,LegacyWindow::windowMs).result==helloRejected);
    // Latched: millis() wrapping around later cannot reopen it.
    assert(!late.open(5) && link.state==LinkAuth::State::Closed);
    LegacyWindow inTime;LinkAuth ok;assert(ok.legacyHello(legacy,40,registry,false,false,inTime,LegacyWindow::windowMs-1).result==helloProvision);
    // A menu-open or OTA hello never consumes it.
    LegacyWindow kept;LinkAuth menu;assert(menu.legacyHello(legacy,40,registry,true,false,kept,0).result==helloRejected);
    LinkAuth otaLink;otaLink.legacyHello(legacy,40,registry,false,true,kept,0);
    LinkAuth later;assert(later.legacyHello(legacy,40,registry,false,false,kept,1).result==helloProvision);
  }
  LinkAuth noHello;assert(!noHello.provisionAllowed(y,33));
  // Once a secret exists, legacy H is refused for good.
  registry.add(hostA,"Mac",y+1,true);
  LegacyWindow fresh;LinkAuth after;assert(after.legacyHello(legacy,40,registry,false,false,fresh,0).result==helloRejected);
  // Invalid legacy NVS data leaves no selection.
  Registry bad;bad.migrateLegacy("not-a-host",10,"Mac");assert(!bad.count && bad.selected<0);
  Registry unnamed;unnamed.migrateLegacy(hostA,36,"");assert(!strcmp(unnamed.entries[0].name,"Computer"));
}
static void registryLruAndPersistence() {
  Registry registry;uint8_t secret[32];
  char host[37];strcpy(host,hostA);
  for(int i=0;i<8;++i){memset(secret,i+1,32);host[35]=char('0'+i);registry.add(host,"PC",secret,true);}
  assert(registry.count==8);
  host[35]='0';registry.select(registry.find(host));  // 0 is now most recent; 1 is least recent
  memset(secret,9,32);host[35]='9';int added=registry.add(host,"New",secret,true);
  assert(registry.count==8 && added==1 && registry.find(host)==1);
  host[35]='1';assert(registry.find(host)<0);
  host[35]='0';assert(registry.selected==registry.find(host));
  // Removing the selected computer clears the selection; later indices shift.
  host[35]='5';int five=registry.find(host);registry.select(five);registry.remove(five);
  assert(registry.count==7 && registry.selected<0 && registry.find(host)<0);
  host[35]='7';registry.select(registry.find(host));int before=registry.selected;
  host[35]='2';registry.remove(registry.find(host));assert(registry.selected==before-1);
  // Blob round trip and corruption detection.
  RegistryBlob blob=registry.encode();Registry copy;assert(copy.decode(blob));
  assert(copy.count==registry.count && copy.selected==registry.selected);
  for(unsigned i=0;i<copy.count;++i) {
    assert(!memcmp(copy.entries[i].host,registry.entries[i].host,37) && !strcmp(copy.entries[i].name,registry.entries[i].name));
    assert(!memcmp(copy.entries[i].secret,registry.entries[i].secret,32) && copy.entries[i].lastUsed==registry.entries[i].lastUsed);
  }
  host[35]='c';int lru=copy.add(host,"Next",secret,true);assert(copy.entries[lru].lastUsed>copy.entries[0].lastUsed);
  RegistryBlob corrupt=blob;corrupt.entries[0].secret[0]^=1;Registry rejected;assert(!rejected.decode(corrupt));
  RegistryBlob badSelection=registry.encode();badSelection.selected=9;
  badSelection.checksum=crc32(reinterpret_cast<const uint8_t*>(&badSelection),offsetof(RegistryBlob,checksum));
  assert(!rejected.decode(badSelection));
  // Paired computers are identified by their proof alone.
  uint8_t challenge[16]={1},packet[17];host[35]='7';int seven=registry.find(host);
  helloPacket(packet,registry.entries[seven].secret,challenge,registry.entries[seven].host);
  assert(registry.matchProof(packet+1,challenge,serial)==seven);
}
static void nonceHello(LinkAuth &link,const uint8_t *nonce) {
  uint8_t packet[17]={'N'};memcpy(packet+1,nonce,16);
  AuthOutcome o=link.nonce(packet,17,false);
  assert(o.quiet && !o.drop && !link.authorized());
}
static void mutualAuthentication() {
  // Vector from Python: hmac.new(bytes(range(1,33)), b"SWM-METER-1"+bytes(range(0xa0,0xb0))
  //   + bytes(range(0x10,0x20)) + b"a1b2c3d4e5f6" + host, hashlib.sha256).digest()[:16]
  uint8_t secret[32],challenge[16],nonce[16],proof[16];char hex[33];
  for(int i=0;i<32;++i) secret[i]=uint8_t(i+1);
  for(int i=0;i<16;++i) { challenge[i]=uint8_t(0xa0+i); nonce[i]=uint8_t(0x10+i); }
  assert(computeMeterProof(secret,challenge,nonce,serial,hostA,proof));
  hexEncode(proof,16,hex);assert(!strcmp(hex,"8b2739822a4b9a04626344b72fad7e91"));
  uint8_t secretA[32],secretB[32],packet[17];memset(secretA,0x11,32);memset(secretB,0x22,32);
  Registry registry;int a=registry.add(hostA,"Mac",secretA,true);registry.add(hostB,"PC",secretB,true);registry.select(a);
  { // After N the selected computer is authorized and the meter proves the same secret.
    LinkAuth link;nonceHello(link,nonce);helloPacket(packet,secretA,challenge,hostA);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    uint8_t expected[16];assert(computeMeterProof(secretA,challenge,nonce,serial,hostA,expected));
    assert(o.result==helloOk && link.authorized() && o.proofLength==16 && !memcmp(o.proof,expected,16));
  }
  { // Without N the reply stays two bytes (companions before mutual authentication).
    LinkAuth link;helloPacket(packet,secretA,challenge,hostA);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    assert(o.result==helloOk && o.proofLength==0 && !o.quiet);
  }
  { // Paired but not selected: 9 with the proof of that computer's own secret.
    LinkAuth link;nonceHello(link,nonce);helloPacket(packet,secretB,challenge,hostB);
    AuthOutcome o=link.proofHello(packet,17,registry,challenge,serial,false,false);
    uint8_t expected[16];assert(computeMeterProof(secretB,challenge,nonce,serial,hostB,expected));
    assert(o.result==helloNotSelected && o.drop && o.proofLength==16 && !memcmp(o.proof,expected,16));
  }
  { // Menu open: after N a paired computer learns it is still known (9), never authorized;
    // an unknown secret gets 7; without N the menu refuses every hello as before.
    LinkAuth known;nonceHello(known,nonce);helloPacket(packet,secretA,challenge,hostA);
    AuthOutcome o=known.proofHello(packet,17,registry,challenge,serial,true,false);
    assert(o.result==helloNotSelected && o.drop && !known.authorized() && o.proofLength==16);
    uint8_t forgotten[32];memset(forgotten,0x33,32);
    LinkAuth unknown;nonceHello(unknown,nonce);helloPacket(packet,forgotten,challenge,hostA);
    o=unknown.proofHello(packet,17,registry,challenge,serial,true,false);
    assert(o.result==helloRejected && o.drop && o.proofLength==0);
    LinkAuth plain;helloPacket(packet,secretA,challenge,hostA);
    assert(plain.proofHello(packet,17,registry,challenge,serial,true,false).result==helloRejected);
  }
  { // N only once, only before the hello, exact length; nothing selected still answers 9.
    LinkAuth twice;nonceHello(twice,nonce);uint8_t n[17]={'N'};
    assert(twice.nonce(n,17,false).result==helloRejected && twice.state==LinkAuth::State::Closed);
    LinkAuth late;helloPacket(packet,secretA,challenge,hostA);late.proofHello(packet,17,registry,challenge,serial,false,false);
    assert(late.nonce(n,17,false).result==helloRejected);
    LinkAuth shortNonce;assert(shortNonce.nonce(n,16,false).result==helloRejected);
    LinkAuth reset;nonceHello(reset,nonce);reset.reset();helloPacket(packet,secretA,challenge,hostA);
    assert(reset.proofHello(packet,17,registry,challenge,serial,false,false).proofLength==0);  // a new link forgets N
    LinkAuth otaLink;assert(otaLink.nonce(n,17,true).quiet && otaLink.state==LinkAuth::State::Open);
    Registry none=registry;none.select(-1);
    LinkAuth unselected;nonceHello(unselected,nonce);helloPacket(packet,secretA,challenge,hostA);
    AuthOutcome o=unselected.proofHello(packet,17,none,challenge,serial,false,false);
    assert(o.result==helloNotSelected && o.proofLength==16);
  }
}
int main() {
  proofMatchesCompanion();linkAuthorization();legacyMigration();registryLruAndPersistence();mutualAuthentication();
  puts("PASS: pairing proof vector, selected/unselected/rejected hello, OTA hello, legacy migration, registry LRU/NVS, mutual proof");
}
