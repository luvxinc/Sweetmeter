#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>

namespace sweetmeter {
constexpr uint8_t protocol = 4;
constexpr size_t headerSize = 160, maxEnvelope = 234, maxPacket = 182;
constexpr const char boardId[] = "elecrow-crowpanel-2.13-v1.2-jd79661";
enum class OtaState : uint8_t { Idle, Metadata, Preparing, Image, Verifying, Rebooting, Cancelled, Error };
enum class OtaError : uint8_t {
  Ok, Unauthorized, Busy, Malformed, Session, State, Offset, Metadata, Signature,
  Board, Version, Companion, Protocol, Size, Power, Flash, Digest, Incomplete,
  Timeout, Disconnected, Image
};
struct Version { uint32_t year, month, sequence; };
inline bool validVersion(const Version &v) {
  return v.year >= 1000 && v.year <= 9999 && v.month >= 1 && v.month <= 12 && v.sequence != 0;
}
inline int compare(const Version &a, const Version &b) {
  if (a.year != b.year) return a.year < b.year ? -1 : 1;
  if (a.month != b.month) return a.month < b.month ? -1 : 1;
  return a.sequence == b.sequence ? 0 : a.sequence < b.sequence ? -1 : 1;
}
inline uint16_t u16(const uint8_t *p) { return uint16_t(p[0]) | uint16_t(p[1]) << 8; }
inline uint32_t u32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
inline void put32(uint8_t *p, uint32_t value) { for (int i=0;i<4;++i) p[i] = uint8_t(value >> (8*i)); }
inline Version versionAt(const uint8_t *p) { return {u32(p), u32(p+4), u32(p+8)}; }
inline void versionText(const Version &v, char *out, size_t n) {
  snprintf(out, n, "%lu.%lu.%lu", (unsigned long)v.year, (unsigned long)v.month, (unsigned long)v.sequence);
}
inline bool zeroes(const uint8_t *p, size_t n) { for(size_t i=0;i<n;++i) if(p[i]) return false; return true; }
inline bool padded(const uint8_t *p, size_t n, const char *expected) {
  size_t len = strlen(expected);
  return len < n && !memcmp(p, expected, len) && zeroes(p+len, n-len);
}
inline bool hostId(const uint8_t *p, size_t n) {
  if(n != 36 || memcmp(p, "7a1e1000-ff1b-4d9f-a023-", 24)) return false;
  for(size_t i=24;i<36;++i) if(!((p[i]>='0'&&p[i]<='9')||(p[i]>='a'&&p[i]<='f'))) return false;
  return true;
}
inline bool hostName(const uint8_t *p, size_t n) {
  if(n<1 || n>20) return false;
  for(size_t i=0;i<n;++i) if(p[i]<32 || p[i]>126) return false;
  return true;
}
// P-256 ECDSA DER consists of exactly two positive, minimally encoded INTEGERs.
inline bool strictSignature(const uint8_t *p,size_t n) {
  if(n<8 || n>72 || p[0]!=0x30 || p[1]!=n-2) return false;
  size_t at=2;
  for(int field=0;field<2;++field) {
    if(at+2>n || p[at++]!=2) return false;
    size_t size=p[at++];
    if(size<1 || size>33 || at+size>n || (p[at]&0x80) ||
       (size>1 && p[at]==0 && !(p[at+1]&0x80))) return false;
    at+=size;
  }
  return at==n;
}
struct Metadata { Version version{}, minimumCompanion{}; uint32_t size=0; uint8_t digest[32]{}; size_t keyIndex=0; };
// Only structural checks here. Signature verification precedes erase in OtaManager.
// `keyIds` is the compiled-in trusted key table; the header selects one entry.
inline OtaError parseEnvelope(const uint8_t *p, size_t n, const Version &running,
                             const Version &companion, uint32_t capacity,
                             const char *const *keyIds, size_t keyCount, Metadata &out) {
  static const uint8_t magic[8] = {'S','W','M','O','T','A','4',0};
  if(n<170 || n>maxEnvelope || memcmp(p,magic,8) || u16(p+8)!=1 ||
     u16(p+10)!=headerSize || u16(p+14) || !zeroes(p+140,20)) return OtaError::Metadata;
  uint16_t signature = u16(p+160);
  if(signature<8 || signature>72 || n!=size_t(162)+signature) return OtaError::Metadata;
  if(u16(p+12)!=protocol) return OtaError::Protocol;
  if(!padded(p+16,48,boardId)) return OtaError::Board;
  out.keyIndex=keyCount;
  for(size_t i=0;i<keyCount;++i) if(padded(p+124,16,keyIds[i])) { out.keyIndex=i; break; }
  if(out.keyIndex==keyCount) return OtaError::Signature;
  out.version=versionAt(p+64); out.minimumCompanion=versionAt(p+76); out.size=u32(p+88);
  memcpy(out.digest,p+92,32);
  if(!validVersion(out.version) || compare(out.version,running)<=0) return OtaError::Version;
  if(!validVersion(out.minimumCompanion) || !validVersion(companion) || compare(companion,out.minimumCompanion)<0)
    return OtaError::Companion;
  if(out.size==0 || out.size>capacity) return OtaError::Size;
  return OtaError::Ok;
}
struct OtaStatus {
  OtaState state=OtaState::Idle; OtaError error=OtaError::Ok;
  uint32_t session=0, offset=0, total=0; uint8_t opcode=0; bool signature=false;
  bool active() const { return state>=OtaState::Metadata && state<=OtaState::Rebooting; }
  bool cancellable() const { return state>=OtaState::Metadata && state<=OtaState::Verifying; }
  void encode(uint8_t out[20]) const {
    memset(out,0,20); out[0]='O'; out[1]=protocol; out[2]=uint8_t(state); out[3]=uint8_t(error);
    put32(out+4,session); put32(out+8,offset); put32(out+12,total); out[16]=opcode;
    out[17]=(cancellable()?1:0)|(signature?2:0);
  }
};
enum class RadioChange { None, Fast, Idle };
// Invoke after state changes; one fast request per live OTA and one restoration
// on exit. A disconnected link has no connection parameters to modify.
class OtaRadioPolicy {
 public:
  RadioChange update(bool active,bool connected) {
    if(!connected) { active_=false; return RadioChange::None; }
    if(active==active_) return RadioChange::None;
    active_=active; return active?RadioChange::Fast:RadioChange::Idle;
  }
 private:
  bool active_=false;
};
// Wrap-safe deadlines never depend on a host clock or Q/status polling.
inline bool expired(uint32_t now,uint32_t since,uint32_t duration) { return uint32_t(now-since)>=duration; }
}
