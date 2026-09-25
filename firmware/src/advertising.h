#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Legacy advertisement (flags + complete 128-bit service UUID list) and the scan
// response: the local name plus a capability marker, so a computer can tell
// this firmware from pre-secret firmware, and see whether its menu is open,
// without connecting (connecting bonds; see bond_policy.h).
//
// Marker: manufacturer-specific data, company ID 0xFFFF (the Bluetooth SIG's
// value for tests/unassigned IDs; Sweetmeter holds no company ID) followed by
// "SM" and one flags byte: bit 0 pairing secrets supported, bit 1 menu open.
// The "-PAIR" name suffix is kept as a fallback for a scanner that reports the
// name but not manufacturer data. Pre-secret firmware sends no marker.
namespace sweetmeter {
constexpr size_t advertisingLimit = 31;
constexpr uint16_t markerCompany = 0xFFFF;
constexpr uint8_t markerAuth = 1, markerMenu = 2;
constexpr char menuNameSuffix[] = "-PAIR";
constexpr size_t advertisementSize = 3 + 18;  // flags AD + complete 128-bit UUID list AD
static_assert(advertisementSize <= advertisingLimit, "Advertisement exceeds 31 bytes");
// Longest scan response: name AD header (2) + a 16-byte owner-chosen name
// (meter_name.h) + "-PAIR" (5) + marker AD (7) = 30 bytes.
constexpr size_t longestScanResponse = 2 + 16 + sizeof(menuNameSuffix) - 1 + 7;
static_assert(longestScanResponse <= advertisingLimit, "A 16-byte name with -PAIR exceeds the scan response");

// Writes the raw scan response; returns its length, or 0 if it would exceed
// 31 bytes (callers then fall back to the name alone).
inline size_t buildScanResponse(uint8_t *out, const char *name, bool menu) {
  size_t base = strlen(name), suffix = menu ? sizeof(menuNameSuffix) - 1 : 0, nameSize = base + suffix;
  size_t total = 2 + nameSize + 7;
  if (total > advertisingLimit) return 0;
  out[0] = uint8_t(nameSize + 1); out[1] = 0x09;  // complete local name
  memcpy(out + 2, name, base); memcpy(out + 2 + base, menuNameSuffix, suffix);
  uint8_t *marker = out + 2 + nameSize;
  marker[0] = 6; marker[1] = 0xFF;  // manufacturer-specific data
  marker[2] = uint8_t(markerCompany & 0xff); marker[3] = uint8_t(markerCompany >> 8);
  marker[4] = 'S'; marker[5] = 'M'; marker[6] = uint8_t(markerAuth | (menu ? markerMenu : 0));
  return total;
}
}  // namespace sweetmeter
