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
#define SWEETMETER_TRUSTED_KEY_ID "release-1"
#define SWEETMETER_TEST_BUILD 0
#define SWEETMETER_TEST_HEALTH_FAIL 0
#define SWEETMETER_TEST_RESET_BEFORE_CONFIRM 0
static const char SWEETMETER_PUBLIC_KEY_PEM[]="stub-public-key";
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
inline int mbedtls_pk_parse_public_key(mbedtls_pk_context*,const unsigned char*,size_t){return 0;}
inline bool mbedtls_pk_can_do(mbedtls_pk_context*,int){return true;}
inline EcStub *mbedtls_pk_ec(mbedtls_pk_context&){static EcStub key;return &key;}
inline int mbedtls_pk_verify(mbedtls_pk_context*,int,const uint8_t*,size_t,const uint8_t*,size_t){return fake::validSignature?0:1;}
