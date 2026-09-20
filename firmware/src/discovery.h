#pragma once
#include "ota_protocol.h"
namespace sweetmeter {
struct Computer { char id[37]{}; char name[21]{}; };
class Discovery {
 public:
  Computer computers[8]{}; unsigned count=0, selection=0;
  bool open=false; uint32_t nonce=0, openedAt=0;
  void begin(uint32_t now,uint32_t random,const char *oldId,const char *oldName) {
    open=true; openedAt=now; nonce=random?random:1; count=selection=0; resetRegistration();
    if(hostId((const uint8_t*)oldId,strlen(oldId)) && hostName((const uint8_t*)oldName,strlen(oldName)))
      add((const uint8_t*)oldId,(const uint8_t*)oldName,strlen(oldName));
  }
  void close() { open=false; nonce=0; resetRegistration(); }
  uint32_t remaining(uint32_t now) const { return open && !expired(now,openedAt,60000)?60000-(now-openedAt):0; }
  void tick(uint32_t now) { if(open && !remaining(now)) close(); if(session_ && expired(now,lastAt_,5000)) resetRegistration(); }
  uint8_t handle(const uint8_t *p,size_t n,uint32_t now,uint32_t &sid,uint32_t &next) {
    sid=n>=5?u32(p+1):0; next=received_;
    if(n<5 || !sid || n>maxPacket) return fail(1);
    if(!open || !remaining(now)) return fail(2);
    if(p[0]=='J') {
      if(n!=11 || u16(p+9)<38 || u16(p+9)>57) return fail(1);
      if(u32(p+5)!=nonce) return fail(2);
      if(session_) return fail(6);
      session_=sid; total_=u16(p+9); received_=0; next=0; lastAt_=now; return 0;
    }
    if(sid!=session_ || !session_) return fail(3);
    if(p[0]=='j') {
      if(n<=9) return fail(1);
      if(u32(p+5)!=received_) return fail(4);
      if(n-9>total_-received_) return fail(1);
      memcpy(body_+received_,p+9,n-9); received_+=n-9; next=received_; lastAt_=now; return 0;
    }
    if(p[0]!='K' || n!=5 || received_!=total_ || total_<38) return fail(1);
    size_t nameSize=body_[36];
    if(total_!=37+nameSize || !hostId(body_,36) || !hostName(body_+37,nameSize)) return fail(1);
    uint8_t result=add(body_,body_+37,nameSize)?0:5;
    next=received_; resetRegistration(); return result;
  }
  void resetRegistration() { session_=0; total_=received_=0; }
 private:
  uint32_t session_=0,lastAt_=0; size_t total_=0,received_=0; uint8_t body_[57]{};
  uint8_t fail(uint8_t error) { resetRegistration(); return error; }
  bool add(const uint8_t *id,const uint8_t *name,size_t size) {
    unsigned i=0; while(i<count && memcmp(computers[i].id,id,36)) ++i;
    if(i==8) return false;
    memcpy(computers[i].id,id,36); computers[i].id[36]=0;
    memcpy(computers[i].name,name,size); computers[i].name[size]=0;
    if(i==count) ++count;
    return true;
  }
};
}
