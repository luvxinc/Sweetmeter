#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace sweetmeter {
constexpr size_t statusLimit = 512;
constexpr char challengePlaceholder[] = "00000000000000000000000000000000";

// Device status (0004). It never names the selected host: a nearby central
// could otherwise replay it. The challenge is a placeholder here; the GATT read
// callback patches the current link's challenge in at `challengeOffset`.
struct StatusFields {
  const char *firmware = "", *board = "", *serial = "", *health = "", *lastUpdate = "", *target = "";
  bool selected = false, secured = false, critical = false, clockSynced = false, menu = false, ota = false;
  int battery = -1, millivolts = -1, rssi = 127;
  unsigned interval = 60, computers = 0;
  uint32_t nonce = 0, remaining = 0;
};

// Returns the JSON length, or -1 if it would not fit (never truncated).
inline int formatStatus(char *out, size_t size, const StatusFields &f, size_t &challengeOffset) {
  int length = snprintf(out, size,
    "{\"protocol\":4,\"firmware\":\"%s\",\"board\":\"%s\",\"auth\":1,\"serial\":\"%s\",\"selected\":%s,"
    "\"secured\":%s,\"challenge\":\"%s\",\"battery_percent\":%d,\"battery_mv\":%d,\"interval\":%u,"
    "\"critical\":%s,\"charge_state\":\"unknown\",\"clock_synced\":%s,\"menu\":%s,\"discovery_nonce\":%lu,"
    "\"discovery_remaining_ms\":%lu,\"computers\":%u,\"ota\":%s,\"boot_health\":\"%s\",\"last_update\":\"%s\","
    "\"ota_target\":\"%s\"",
    f.firmware, f.board, f.serial, f.selected ? "true" : "false", f.secured ? "true" : "false",
    challengePlaceholder, f.battery, f.millivolts, f.interval, f.critical ? "true" : "false",
    f.clockSynced ? "true" : "false", f.menu ? "true" : "false", (unsigned long)f.nonce,
    (unsigned long)f.remaining, f.computers, f.ota ? "true" : "false", f.health, f.lastUpdate, f.target);
  if (length < 0 || size_t(length) + 1 >= size || size_t(length) + 1 >= statusLimit) return -1;
  // Optional diagnostics are appended only when they fit; required fields never truncate.
  int extra = f.rssi != 127 ? snprintf(out + length, size - size_t(length), ",\"rssi\":%d}", f.rssi) : -1;
  if (extra < 0 || size_t(length + extra) >= size || size_t(length + extra) >= statusLimit) {
    out[length++] = '}'; out[length] = 0;
  } else {
    length += extra;
  }
  const char *field = strstr(out, "\"challenge\":\"");
  if (!field) return -1;
  challengeOffset = size_t(field - out) + 13;
  return length;
}
}  // namespace sweetmeter
