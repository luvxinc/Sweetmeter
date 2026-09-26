#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Meter name (docs/PROTOCOL.md sections 1 and 2.2). The owner-chosen name is
// 1-16 bytes of well-formed UTF-8 without control characters, surrounding
// spaces or the "-PAIR" menu suffix; without one the meter uses the default
// "Sweetmeter-XXXX" from the last four serial characters (last two MAC bytes).
namespace sweetmeter {
constexpr size_t meterNameMax = 16;
constexpr char defaultNamePrefix[] = "Sweetmeter-";
constexpr size_t defaultNameSize = sizeof(defaultNamePrefix) - 1 + 4;
constexpr uint8_t renameOk = 0, renameInvalid = 1, renameBusy = 2, renameStorage = 5, renameRefused = 7;

// Shortest-form UTF-8 only: no overlong encodings, surrogates or code points
// above U+10FFFF; C0/C1 controls and DEL are refused as well.
inline bool validMeterName(const uint8_t *bytes, size_t length) {
  if (!bytes || !length || length > meterNameMax || bytes[0] == ' ' || bytes[length - 1] == ' ') return false;
  static const char suffix[] = "-PAIR";
  const size_t suffixSize = sizeof(suffix) - 1;
  if (length >= suffixSize && !memcmp(bytes + length - suffixSize, suffix, suffixSize)) return false;
  for (size_t i = 0; i < length;) {
    uint8_t lead = bytes[i];
    uint32_t point; size_t extra;
    if (lead < 0x80) { point = lead; extra = 0; }
    else if (lead >= 0xC2 && lead <= 0xDF) { point = lead & 0x1F; extra = 1; }
    else if (lead >= 0xE0 && lead <= 0xEF) { point = lead & 0x0F; extra = 2; }
    else if (lead >= 0xF0 && lead <= 0xF4) { point = lead & 0x07; extra = 3; }
    else return false;  // stray continuation byte, C0/C1 overlong lead or F5..FF
    if (extra > length - i - 1) return false;
    for (size_t k = 1; k <= extra; ++k) {
      uint8_t next = bytes[i + k];
      if ((next & 0xC0) != 0x80) return false;
      point = (point << 6) | (next & 0x3F);
    }
    static const uint32_t smallest[] = {0, 0x80, 0x800, 0x10000};
    if (point < smallest[extra] || point > 0x10FFFF || (point >= 0xD800 && point <= 0xDFFF)) return false;
    if (point < 0x20 || (point >= 0x7F && point <= 0x9F)) return false;
    i += extra + 1;
  }
  return true;
}

// `out` receives "Sweetmeter-" plus the serial's last four characters in upper case.
inline void defaultMeterName(const char *serial, char *out, size_t size) {
  size_t serialLength = serial ? strlen(serial) : 0;
  if (size < defaultNameSize + 1 || serialLength < 4) { if (size) out[0] = 0; return; }
  memcpy(out, defaultNamePrefix, sizeof(defaultNamePrefix) - 1);
  for (size_t i = 0; i < 4; ++i) {
    char c = serial[serialLength - 4 + i];
    out[sizeof(defaultNamePrefix) - 1 + i] = c >= 'a' && c <= 'z' ? char(c - 'a' + 'A') : c;
  }
  out[defaultNameSize] = 0;
}

// `L:u8, length:u8, name`. Returns renameOk with `name`/`nameLength` set
// (length 0 restores the default name) or renameInvalid.
inline uint8_t parseRename(const uint8_t *packet, size_t size, const uint8_t *&name, size_t &nameLength) {
  name = nullptr; nameLength = 0;
  if (!packet || size < 2 || packet[0] != 'L' || size != 2 + size_t(packet[1]) || packet[1] > meterNameMax) return renameInvalid;
  if (packet[1] && !validMeterName(packet + 2, packet[1])) return renameInvalid;
  name = packet + 2; nameLength = packet[1];
  return renameOk;
}

// The 6x11 screen font covers printable ASCII only.
inline bool screenPrintable(const char *text) {
  if (!text || !*text) return false;
  for (; *text; ++text) if (uint8_t(*text) < 0x20 || uint8_t(*text) > 0x7E) return false;
  return true;
}
}  // namespace sweetmeter
