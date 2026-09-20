// ESP application-description ABI from Espressif ESP-IDF 4.4.7 (Apache-2.0).
// Own the descriptor and accessors so the prebuilt Arduino descriptor cannot
// silently substitute its core version for the signed Sweetmeter version.
#include <esp_ota_ops.h>
#include <esp_attr.h>
#include <esp_idf_version.h>
#include "sweetmeter_release.h"
#define SM_STRINGIFY_IMPL(x) #x
#define SM_STRINGIFY(x) SM_STRINGIFY_IMPL(x)
extern "C" {
extern const esp_app_desc_t esp_app_desc __attribute__((section(".rodata_desc"))) = {
  ESP_APP_DESC_MAGIC_WORD,0,{0,0},SWEETMETER_VERSION,SWEETMETER_PROJECT_NAME,"","",
  SM_STRINGIFY(ESP_IDF_VERSION_MAJOR) "." SM_STRINGIFY(ESP_IDF_VERSION_MINOR) "." SM_STRINGIFY(ESP_IDF_VERSION_PATCH),
  {0},{0}
};
const esp_app_desc_t *esp_ota_get_app_description(void) { return &esp_app_desc; }
static uint8_t DRAM_ATTR cachedElfHash[32];
__attribute__((constructor)) void esp_ota_init_app_elf_sha256(void) {
  // esptool patches this field after linking; volatile prevents constant folding.
  const volatile uint8_t *source=esp_app_desc.app_elf_sha256;
  for(unsigned i=0;i<sizeof(cachedElfHash);++i) cachedElfHash[i]=source[i];
}
int IRAM_ATTR esp_ota_get_app_elf_sha256(char *dst,size_t size) {
  if(!dst || !size) return 0;
  size_t count=(size-1)/2; if(count>sizeof(cachedElfHash)) count=sizeof(cachedElfHash);
  for(size_t i=0;i<count;++i) {
    unsigned hi=cachedElfHash[i]>>4,lo=cachedElfHash[i]&15;
    dst[2*i]=hi<10?'0'+hi:'a'+hi-10; dst[2*i+1]=lo<10?'0'+lo:'a'+lo-10;
  }
  dst[2*count]=0; return 2*count+1;
}
}
