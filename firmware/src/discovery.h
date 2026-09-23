#pragma once
#include "ota_protocol.h"
#include "pairing.h"
namespace sweetmeter {
// One row of the physical computer menu: either an already paired computer
// (instant switch) or a computer that registered while this window is open.
struct Computer {
  char id[37]{}; char name[21]{};
  uint8_t secret[secretSize]{};
  int paired=-1;  // registry index, or -1 for a new computer
  // registered: a registration arrived in this window (paired rows start
  // without one). conflict: two different secrets claimed this ID.
  // keyChanged: a paired computer registered a different secret; the owner
  // must confirm it explicitly before it replaces the stored one.
  // outdated: an old companion (pre-secret registration body) asked to pair;
  // it can only be shown "Update Sweetmeter on <name>".
  bool hasSecret=false, registered=false, conflict=false, keyChanged=false, outdated=false;
};
// Registration J results.
enum RegistrationResult : uint8_t {
  RegOk, RegMalformed, RegClosed, RegSession, RegOffset, RegFull, RegBusy, RegRateLimited, RegConflict,
  RegOutdated
};
class Discovery {
 public:
  static constexpr size_t minBody=36+1+1+secretSize, maxBody=36+1+20+secretSize;  // 70..89
  // Body without a secret, sent by companions older than pairing secrets.
  static constexpr size_t minLegacyBody=36+1+1, maxLegacyBody=36+1+20;  // 38..57
  static constexpr unsigned capacity=12, perPeerLimit=2, peerSlots=8;
  Computer computers[capacity]{}; unsigned count=0, selection=0;
  bool open=false; uint32_t nonce=0, openedAt=0;
  // Paired computers are listed first: the selected one, then most recent.
  void begin(uint32_t now,uint32_t random,const Registry &registry) {
    open=true; openedAt=now; nonce=random?random:1; count=selection=0; peers_=0; resetRegistration();
    for(auto &c:computers) c=Computer{};
    bool used[maxPaired]{};
    for(unsigned n=0;n<registry.count;++n) {
      int pick=-1;
      if(!n && registry.selected>=0) pick=registry.selected;
      else for(unsigned i=0;i<registry.count;++i)
        if(!used[i] && (pick<0 || registry.entries[i].lastUsed>registry.entries[pick].lastUsed)) pick=int(i);
      used[pick]=true;
      const PairedComputer &p=registry.entries[pick]; Computer &c=computers[count++];
      memcpy(c.id,p.host,36); memcpy(c.name,p.name,strnlen(p.name,20)); c.paired=pick;
      if(p.hasSecret) { memcpy(c.secret,p.secret,secretSize); c.hasSecret=true; }
    }
  }
  void close() { open=false; nonce=0; resetRegistration(); }
  uint32_t remaining(uint32_t now) const { return open && !expired(now,openedAt,60000)?60000-(now-openedAt):0; }
  void tick(uint32_t now) { if(open && !remaining(now)) close(); if(session_ && expired(now,lastAt_,5000)) resetRegistration(); }
  // A paired row stays selectable (switching back needs no new registration);
  // a new row from an outdated companion cannot be paired.
  bool selectable(unsigned i) const {
    return i<count && !computers[i].conflict && !(computers[i].outdated && computers[i].paired<0);
  }
  // Choosing this row replaces a stored secret: ask the owner first.
  bool needsKeyConfirmation(unsigned i) const { return selectable(i) && computers[i].keyChanged; }
  // Drop a row; when its registry entry was deleted, renumber later entries.
  void removeAt(unsigned i,bool registryRemoved) {
    if(i>=count) return;
    int removed=computers[i].paired;
    for(unsigned j=i;j+1<count;++j) computers[j]=computers[j+1];
    computers[--count]=Computer{};
    if(registryRemoved && removed>=0) for(unsigned j=0;j<count;++j) if(computers[j].paired>removed) --computers[j].paired;
    if(selection>=count) selection=count?count-1:0;
  }
  // `peer` is the connection's BLE address; it bounds list stuffing per sender.
  uint8_t handle(const uint8_t *p,size_t n,uint32_t now,uint32_t &sid,uint32_t &next,const uint8_t *peer) {
    sid=n>=5?u32(p+1):0; next=received_;
    if(n<5 || !sid || n>maxPacket) return fail(RegMalformed);
    if(!open || !remaining(now)) return fail(RegClosed);
    if(p[0]=='J') {
      if(n!=11) return fail(RegMalformed);
      size_t total=u16(p+9);
      if(!((total>=minBody && total<=maxBody) || (total>=minLegacyBody && total<=maxLegacyBody)))
        return fail(RegMalformed);
      if(u32(p+5)!=nonce) return fail(RegClosed);
      if(session_) return fail(RegBusy);
      if(peerRegistrations(peer)>=perPeerLimit) return fail(RegRateLimited);
      session_=sid; total_=u16(p+9); received_=0; next=0; lastAt_=now; memcpy(peer_,peer,6); return RegOk;
    }
    if(sid!=session_ || !session_) return fail(RegSession);
    if(p[0]=='j') {
      if(n<=9) return fail(RegMalformed);
      if(u32(p+5)!=received_) return fail(RegOffset);
      if(n-9>total_-received_) return fail(RegMalformed);
      memcpy(body_+received_,p+9,n-9); received_+=n-9; next=received_; lastAt_=now; return RegOk;
    }
    if(p[0]!='K' || n!=5 || received_!=total_) return fail(RegMalformed);
    size_t nameSize=body_[36];
    if(total_<=maxLegacyBody) {
      // An old companion: remember who it is so the menu can say which
      // computer needs the Sweetmeter update, but never pair it.
      if(total_!=37+nameSize || !hostId(body_,36) || !hostName(body_+37,nameSize)) return fail(RegMalformed);
      uint8_t result=addOutdated(body_,body_+37,nameSize);
      next=received_; resetRegistration(); return result;
    }
    const uint8_t *secret=body_+37+nameSize;
    if(total_!=37+nameSize+secretSize || !hostId(body_,36) || !hostName(body_+37,nameSize) || !usableSecret(secret))
      return fail(RegMalformed);
    uint8_t result=add(body_,body_+37,nameSize,secret);
    if(!result) countPeer(peer_);
    next=received_; resetRegistration(); return result;
  }
  void resetRegistration() { session_=0; total_=received_=0; }
  // This connection address completed a registration in the current (or the
  // just-closed) window; kept until the next begin(). See bond_policy.h.
  bool registeredPeer(const uint8_t *peer) const {
    for(unsigned i=0;i<peers_;++i) if(!memcmp(peerList_[i].address,peer,6)) return peerList_[i].count>0;
    return false;
  }
 private:
  struct Peer { uint8_t address[6]; uint8_t count; };
  uint32_t session_=0,lastAt_=0; size_t total_=0,received_=0; uint8_t body_[maxBody]{}, peer_[6]{};
  Peer peerList_[peerSlots]{}; unsigned peers_=0;
  uint8_t fail(uint8_t error) { resetRegistration(); return error; }
  unsigned peerRegistrations(const uint8_t *peer) const {
    for(unsigned i=0;i<peers_;++i) if(!memcmp(peerList_[i].address,peer,6)) return peerList_[i].count;
    return peers_==peerSlots?perPeerLimit:0;
  }
  void countPeer(const uint8_t *peer) {
    for(unsigned i=0;i<peers_;++i) if(!memcmp(peerList_[i].address,peer,6)) { ++peerList_[i].count; return; }
    if(peers_<peerSlots) { memcpy(peerList_[peers_].address,peer,6); peerList_[peers_++].count=1; }
  }
  uint8_t add(const uint8_t *id,const uint8_t *name,size_t size,const uint8_t *secret) {
    unsigned i=0; while(i<count && memcmp(computers[i].id,id,36)) ++i;
    if(i==capacity) return RegFull;
    Computer &c=computers[i];
    if(i<count && c.registered && !constantTimeEqual(c.secret,secret,secretSize)) {
      // Two registrations claim one ID with different secrets: an impostor
      // knows the ID. That row cannot be selected in this window.
      c.conflict=true; return RegConflict;
    }
    // A paired computer that re-registers with a different secret (after
    // forgetting this meter, or an impostor that knows its ID) is marked; the
    // new secret replaces the stored one only after an explicit confirmation.
    if(i<count && c.paired>=0 && c.hasSecret && !constantTimeEqual(c.secret,secret,secretSize)) c.keyChanged=true;
    memcpy(c.id,id,36); c.id[36]=0;
    memset(c.name,0,sizeof(c.name)); memcpy(c.name,name,size);
    memcpy(c.secret,secret,secretSize); c.hasSecret=true; c.registered=true; c.outdated=false;
    if(i==count) ++count;
    return RegOk;
  }
  uint8_t addOutdated(const uint8_t *id,const uint8_t *name,size_t size) {
    unsigned i=0; while(i<count && memcmp(computers[i].id,id,36)) ++i;
    if(i==capacity) return RegFull;
    Computer &c=computers[i];
    // A registration with a secret in this window wins over an old app's.
    if(i<count && c.registered) return RegOutdated;
    if(i==count) { c=Computer{}; memcpy(c.id,id,36); ++count; }
    memset(c.name,0,sizeof(c.name)); memcpy(c.name,name,size);
    c.outdated=true;
    return RegOutdated;
  }
};
}
