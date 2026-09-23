#pragma once
// Deterministic native doubles for state/failure tests. Cryptographic primitives
// are stubbed here; Python fixtures and on-device acceptance verify real crypto.
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <vector>
#include <map>
#include <string>
#include <functional>
#define CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE 1
#define SWEETMETER_VERSION "2026.9.1"
#define SWEETMETER_VERSION_YEAR 2026
#define SWEETMETER_VERSION_MONTH 9
#define SWEETMETER_VERSION_SEQUENCE 1
#define SWEETMETER_TEST_BUILD 0
#define SWEETMETER_TEST_HEALTH_FAIL 0
#define SWEETMETER_TEST_RESET_BEFORE_CONFIRM 0
#define SWEETMETER_TRUSTED_KEY_COUNT 2u
static const char *const SWEETMETER_TRUSTED_KEY_IDS[]={"release-1","release-2"};
static const char *const SWEETMETER_TRUSTED_KEY_PEMS[]={"stub-public-key-1","stub-public-key-2"};
static const unsigned SWEETMETER_TRUSTED_KEY_PEM_SIZES[]={18,18};
using esp_err_t=int; using esp_ota_handle_t=uint32_t; using esp_timer_handle_t=void*;
constexpr int ESP_OK=0, ESP_ERR_OTA_VALIDATE_FAILED=123;
enum esp_ota_img_states_t { ESP_OTA_IMG_NEW,ESP_OTA_IMG_PENDING_VERIFY,ESP_OTA_IMG_VALID,ESP_OTA_IMG_INVALID,ESP_OTA_IMG_ABORTED,ESP_OTA_IMG_UNDEFINED=-1 };
using esp_partition_subtype_t=int;
constexpr int ESP_PARTITION_TYPE_APP=0, ESP_PARTITION_SUBTYPE_APP_OTA_0=16;
struct esp_partition_t { uint32_t address,size; };
struct esp_app_desc_t { char version[32]; };
struct esp_timer_create_args_t { void(*callback)(void*); const char *name; };
struct Restart {};
namespace fake {
inline uint32_t now=100;
inline esp_partition_t slots[2]={{0x10000,0x330000},{0x340000,0x330000}};
inline int running=0,selected=0;
inline esp_ota_img_states_t states[2]={ESP_OTA_IMG_VALID,ESP_OTA_IMG_UNDEFINED};
inline unsigned begins=0,writes=0,ends=0,aborts=0,selections=0,marks=0;
inline int beginError=0,writeError=0,endError=0,selectError=0,markError=0;
inline bool validSignature=true,selectionChangesOnError=false;
inline const unsigned char *parsedKey=nullptr;
inline uint8_t digest[32]{};
inline char imageVersion[32]="2026.9.2";
inline std::function<void()> onBegin,onEnd,onSelect;
inline void reset() {
 now=100;running=selected=0;states[0]=ESP_OTA_IMG_VALID;states[1]=ESP_OTA_IMG_UNDEFINED;
 begins=writes=ends=aborts=selections=marks=0;beginError=writeError=endError=selectError=markError=0;
 validSignature=true;selectionChangesOnError=false;memset(digest,0,32);strcpy(imageVersion,"2026.9.2");
 onBegin=onEnd=onSelect=nullptr;
}
}
inline uint32_t millis() { return fake::now; }
inline void delay(uint32_t ms) { fake::now+=ms; }
[[noreturn]] inline void esp_restart() { throw Restart{}; }
struct SerialStub { template<class... A> void printf(const char*,A...) {} void println(const char*) {} void flush() {} };
inline SerialStub Serial;
class Preferences {
 public:
 std::map<std::string,std::vector<uint8_t>> data; bool fail=false; unsigned writes=0; unsigned failWrite=0;
 size_t getBytesLength(const char *key) { return data[key].size(); }
 size_t getBytes(const char *key,void *out,size_t n) { auto &v=data[key]; if(v.size()>n)return 0; memcpy(out,v.data(),v.size());return v.size(); }
 size_t putBytes(const char *key,const void *p,size_t n) { ++writes;if(fail || writes==failWrite)return 0;data[key]=std::vector<uint8_t>((const uint8_t*)p,(const uint8_t*)p+n);return n; }
};
inline const esp_partition_t *esp_ota_get_running_partition(){return &fake::slots[fake::running];}
inline const esp_partition_t *esp_ota_get_boot_partition(){return &fake::slots[fake::selected];}
inline const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t*){return &fake::slots[1-fake::running];}
inline const esp_partition_t *esp_partition_find_first(int,int subtype,const char*) {int i=subtype-16;return i>=0&&i<2?&fake::slots[i]:nullptr;}
inline esp_err_t esp_ota_get_state_partition(const esp_partition_t *p,esp_ota_img_states_t *out){*out=fake::states[p->address==fake::slots[0].address?0:1];return 0;}
inline esp_err_t esp_ota_begin(const esp_partition_t*,size_t,esp_ota_handle_t *handle){++fake::begins;*handle=1;if(fake::onBegin)fake::onBegin();return fake::beginError;}
inline esp_err_t esp_ota_write(esp_ota_handle_t,const void*,size_t){++fake::writes;return fake::writeError;}
inline esp_err_t esp_ota_end(esp_ota_handle_t){++fake::ends;if(fake::onEnd)fake::onEnd();return fake::endError;}
inline esp_err_t esp_ota_abort(esp_ota_handle_t){++fake::aborts;return 0;}
inline esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p){++fake::selections;if(fake::onSelect)fake::onSelect();if(!fake::selectError||fake::selectionChangesOnError)fake::selected=p->address==fake::slots[0].address?0:1;return fake::selectError;}
inline esp_err_t esp_ota_get_partition_description(const esp_partition_t*,esp_app_desc_t *out){strcpy(out->version,fake::imageVersion);return 0;}
inline esp_err_t esp_ota_mark_app_valid_cancel_rollback(){++fake::marks;if(!fake::markError)fake::states[fake::running]=ESP_OTA_IMG_VALID;return fake::markError;}
inline esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(){fake::states[fake::running]=ESP_OTA_IMG_INVALID;fake::selected=1-fake::running;throw Restart{};}
inline esp_err_t esp_timer_create(const esp_timer_create_args_t*,esp_timer_handle_t *out){*out=(void*)1;return 0;}
inline esp_err_t esp_timer_start_once(esp_timer_handle_t,uint64_t){return 0;}
inline esp_err_t esp_timer_stop(esp_timer_handle_t){return 0;}
inline esp_err_t esp_timer_delete(esp_timer_handle_t){return 0;}
struct mbedtls_sha256_context {}; struct mbedtls_pk_context {};
struct EcStub { struct { int id=1; } grp; };
constexpr int MBEDTLS_PK_ECDSA=1,MBEDTLS_MD_SHA256=1,MBEDTLS_ECP_DP_SECP256R1=1;
inline void mbedtls_sha256_init(mbedtls_sha256_context*){}
inline void mbedtls_sha256_free(mbedtls_sha256_context*){}
inline int mbedtls_sha256_starts_ret(mbedtls_sha256_context*,int){return 0;}
inline int mbedtls_sha256_update_ret(mbedtls_sha256_context*,const uint8_t*,size_t){return 0;}
inline int mbedtls_sha256_finish_ret(mbedtls_sha256_context*,uint8_t *p){memcpy(p,fake::digest,32);return 0;}
inline int mbedtls_sha256_ret(const uint8_t*,size_t,uint8_t *p,int){memset(p,0,32);return 0;}
inline void mbedtls_pk_init(mbedtls_pk_context*){}
inline void mbedtls_pk_free(mbedtls_pk_context*){}
inline int mbedtls_pk_parse_public_key(mbedtls_pk_context*,const unsigned char *key,size_t){fake::parsedKey=key;return 0;}
inline bool mbedtls_pk_can_do(mbedtls_pk_context*,int){return true;}
inline EcStub *mbedtls_pk_ec(mbedtls_pk_context&){static EcStub key;return &key;}
inline int mbedtls_pk_verify(mbedtls_pk_context*,int,const uint8_t*,size_t,const uint8_t*,size_t){return fake::validSignature?0:1;}
// Real SHA-256/HMAC (FIPS 180-4, RFC 2104) so pairing proofs match Python's hmac.
namespace stubsha {
struct Sha256 {
 uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
 uint8_t block[64]{}; size_t used=0; uint64_t bits=0;
 static uint32_t rotr(uint32_t x,int n){return (x>>n)|(x<<(32-n));}
 void compress(){
  static const uint32_t k[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
   0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,
   0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,
   0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,
   0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
   0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,
   0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
  uint32_t w[64];
  for(int i=0;i<16;++i) w[i]=uint32_t(block[4*i])<<24|uint32_t(block[4*i+1])<<16|uint32_t(block[4*i+2])<<8|block[4*i+3];
  for(int i=16;i<64;++i){uint32_t s0=rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>3),s1=rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>10);w[i]=w[i-16]+s0+w[i-7]+s1;}
  uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
  for(int i=0;i<64;++i){
   uint32_t t1=hh+(rotr(e,6)^rotr(e,11)^rotr(e,25))+((e&f)^(~e&g))+k[i]+w[i];
   uint32_t t2=(rotr(a,2)^rotr(a,13)^rotr(a,22))+((a&b)^(a&c)^(b&c));
   hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
  }
  h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;
 }
 void update(const uint8_t *p,size_t n){for(size_t i=0;i<n;++i){block[used++]=p[i];bits+=8;if(used==64){compress();used=0;}}}
 void finish(uint8_t *out){
  uint64_t total=bits; uint8_t pad=0x80; update(&pad,1); pad=0;
  while(used!=56) update(&pad,1);
  for(int i=7;i>=0;--i){uint8_t b=uint8_t(total>>(8*i));update(&b,1);}
  for(int i=0;i<8;++i) for(int j=0;j<4;++j) out[4*i+j]=uint8_t(h[i]>>(24-8*j));
 }
};
}
struct mbedtls_md_info_t { int type; };
inline const mbedtls_md_info_t *mbedtls_md_info_from_type(int type){static const mbedtls_md_info_t sha256{1};return type==MBEDTLS_MD_SHA256?&sha256:nullptr;}
inline int mbedtls_md_hmac(const mbedtls_md_info_t*,const unsigned char *key,size_t keySize,const unsigned char *input,size_t size,unsigned char *output){
 uint8_t block[64]{};
 if(keySize>64){stubsha::Sha256 s;s.update(key,keySize);s.finish(block);} else memcpy(block,key,keySize);
 uint8_t pad[64],inner[32];
 for(int i=0;i<64;++i) pad[i]=block[i]^0x36;
 stubsha::Sha256 in;in.update(pad,64);in.update(input,size);in.finish(inner);
 for(int i=0;i<64;++i) pad[i]=block[i]^0x5c;
 stubsha::Sha256 out;out.update(pad,64);out.update(inner,32);out.finish(output);
 return 0;
}
