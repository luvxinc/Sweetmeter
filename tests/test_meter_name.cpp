#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../firmware/src/meter_name.h"
#include "../firmware/src/advertising.h"
using namespace sweetmeter;

static bool valid(const char *text) { return validMeterName(reinterpret_cast<const uint8_t*>(text),strlen(text)); }
static bool validBytes(const uint8_t *bytes,size_t n) { return validMeterName(bytes,n); }

static void nameRules() {
  assert(valid("Desk meter") && valid("A") && valid("Sweetmeter-CF24") && valid("0123456789abcdef"));
  assert(!valid("0123456789abcdefg"));  // 17 bytes
  assert(!valid("") && !validMeterName(nullptr,3));
  // Five CJK characters are 15 bytes; six are 18.
  assert(valid("\xE5\x8A\x9E\xE5\x85\xAC\xE5\xAE\xA4\xE7\x9A\x84\xE8\xA1\xA8"));
  assert(!valid("\xE5\x8A\x9E\xE5\x85\xAC\xE5\xAE\xA4\xE7\x9A\x84\xE8\xA1\xA8\xE8\xA1\xA8"));
  assert(valid("\xF0\x9F\x8D\xAC sweet") && valid("\xC3\xA9t\xC3\xA9"));  // 4-byte emoji, 2-byte Latin
  assert(valid("\xF4\x8F\xBF\xBF") && valid("\xEF\xBF\xBD"));  // U+10FFFF and U+FFFD are well formed
  // Malformed UTF-8.
  const uint8_t overlong2[]={'a',0xC0,0x80},overlong3[]={0xE0,0x80,0x80},overlong4[]={0xF0,0x80,0x80,0x80};
  const uint8_t overlongSlash[]={0xC1,0xBF},surrogate[]={0xED,0xA0,0x80},tooHigh[]={0xF4,0x90,0x80,0x80};
  const uint8_t f5[]={0xF5,0x80,0x80,0x80},stray[]={'a',0x80,'b'},truncated2[]={'a',0xC3},truncated3[]={0xE5,0x8A};
  const uint8_t truncated4[]={0xF0,0x9F,0x8D},badContinuation[]={0xE5,0x41,0x9E};
  struct Sample { const uint8_t *bytes; size_t size; };
  const Sample malformed[]={{overlong2,3},{overlong3,3},{overlong4,4},{overlongSlash,2},{surrogate,3},{tooHigh,4},
                            {f5,4},{stray,3},{truncated2,2},{truncated3,2},{truncated4,3},{badContinuation,3}};
  for(const Sample &sample:malformed) assert(!validBytes(sample.bytes,sample.size));
  // Control characters: C0, DEL and C1 (U+0085 as C2 85).
  const uint8_t c0[]={'a',0x01,'b'},nul[]={'a',0x00},del[]={'a',0x7F},c1[]={'a',0xC2,0x85},c1Last[]={0xC2,0x9F};
  const uint8_t firstAfterC1[]={0xC2,0xA0};  // U+00A0 is allowed
  assert(!validBytes(c0,3) && !validBytes(nul,2) && !validBytes(del,2) && !validBytes(c1,3) && !validBytes(c1Last,2));
  assert(validBytes(firstAfterC1,2) && !valid("tab\there"));
  // Surrounding spaces and the open-menu suffix.
  assert(!valid(" meter") && !valid("meter ") && valid("my meter"));
  assert(!valid("Meter-PAIR") && !valid("-PAIR") && valid("Meter-PAIRS") && valid("Meter-pair"));
}
static void defaultName() {
  char name[32];
  defaultMeterName("d405927bcf24",name,sizeof(name));
  assert(!strcmp(name,"Sweetmeter-CF24") && strlen(name)==defaultNameSize && valid(name));
  defaultMeterName("d405927bbf38",name,sizeof(name));
  assert(!strcmp(name,"Sweetmeter-BF38"));
  char small[15];defaultMeterName("d405927bcf24",small,sizeof(small));assert(!small[0]);
  defaultMeterName("cf2",name,sizeof(name));assert(!name[0]);
  assert(screenPrintable("Sweetmeter-CF24") && screenPrintable("~ !") && !screenPrintable(""));
  assert(!screenPrintable("\xE5\x8A\x9E") && !screenPrintable("a\x7F"));
}
static void renamePacket() {
  const uint8_t *name=nullptr;size_t length=99;
  uint8_t packet[20]={'L',4,'D','e','s','k'};
  assert(parseRename(packet,6,name,length)==renameOk && name==packet+2 && length==4);
  assert(parseRename(packet,5,name,length)==renameInvalid && !name && !length);  // size mismatch
  assert(parseRename(packet,7,name,length)==renameInvalid);
  uint8_t restore[2]={'L',0};
  assert(parseRename(restore,2,name,length)==renameOk && length==0);
  assert(parseRename(restore,1,name,length)==renameInvalid && parseRename(nullptr,2,name,length)==renameInvalid);
  uint8_t other[6]={'X',4,'D','e','s','k'};assert(parseRename(other,6,name,length)==renameInvalid);
  uint8_t full[18]={'L',16};memcpy(full+2,"0123456789abcdef",16);
  assert(parseRename(full,18,name,length)==renameOk && length==16);
  uint8_t tooLong[19]={'L',17};memcpy(tooLong+2,"0123456789abcdefg",17);
  assert(parseRename(tooLong,19,name,length)==renameInvalid);
  uint8_t invalid[5]={'L',3,'a',0x01,'b'};assert(parseRename(invalid,5,name,length)==renameInvalid);
  uint8_t spaced[4]={'L',2,'a',' '};assert(parseRename(spaced,4,name,length)==renameInvalid);
}
static void scanResponse() {
  // A 16-byte UTF-8 name: 2 + 16 + 7 = 25 bytes, 30 with "-PAIR".
  const char *cjk="\xE5\x8A\x9E\xE5\x85\xAC\xE5\xAE\xA4\xE7\x9A\x84\xE8\xA1\xA8!";
  assert(strlen(cjk)==meterNameMax && valid(cjk));
  uint8_t raw[advertisingLimit];
  assert(buildScanResponse(raw,cjk,false)==25 && raw[0]==17 && raw[1]==0x09 && !memcmp(raw+2,cjk,16));
  assert(buildScanResponse(raw,cjk,true)==30 && raw[0]==22 && !memcmp(raw+18,"-PAIR",5));
  assert(raw[23]==6 && raw[24]==0xFF && raw[27]=='S' && raw[28]=='M' && raw[29]==(markerAuth|markerMenu));
  static_assert(longestScanResponse==30 && longestScanResponse<=advertisingLimit,"scan response");
  char name[32];defaultMeterName("d405927bcf24",name,sizeof(name));
  assert(buildScanResponse(raw,name,true)==29);
}
int main() {
  nameRules();defaultName();renamePacket();scanResponse();
  puts("PASS: meter name UTF-8/control/suffix rules, default name from serial, L packet parsing, scan response budget");
}
