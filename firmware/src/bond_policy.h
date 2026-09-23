#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace sweetmeter {
// ESP-IDF 4.4.7 Bluedroid keeps bonds most-recent-first and, once more than
// CONFIG_BT_SMP_MAX_BONDS (15) exist, silently deletes the least recent ones
// (btc_ble_storage.c _btc_storage_save). Stray phones and discovery candidates
// bond too, so the selected computer's bond could be the one that is lost.
// Sweetmeter evicts stale bonds itself before the table is nearly full.
constexpr size_t bondEvictThreshold = 12, recentPeerCount = 8;

// Bond addresses of computers that recently proved a pairing secret (the
// selected one or another paired one). Strays never prove one, so they cannot
// push a paired computer out. Persisted as one small NVS blob.
struct RecentPeers {
  uint8_t addresses[recentPeerCount][6]{};
  uint8_t count = 0;
  // Returns true when the list changed (the caller persists it).
  bool touch(const uint8_t *address) {
    size_t at = 0;
    while (at < count && memcmp(addresses[at], address, 6)) ++at;
    if (at == 0 && count) return false;
    if (at == count) { if (count < recentPeerCount) ++count; at = count - 1; }
    for (size_t i = at; i > 0; --i) memcpy(addresses[i], addresses[i-1], 6);
    memcpy(addresses[0], address, 6);
    return true;
  }
  bool contains(const uint8_t *address) const {
    for (size_t i = 0; i < count && i < recentPeerCount; ++i) if (!memcmp(addresses[i], address, 6)) return true;
    return false;
  }
};

// Mark bonds to remove. Nothing is removed below the threshold; above it every
// bond except the current link and recently authenticated peers goes.
inline size_t chooseBondEvictions(const uint8_t (*bonds)[6], size_t count, const uint8_t *selected,
                                  const uint8_t *current, const RecentPeers &recent, bool *evict) {
  size_t evicted = 0;
  for (size_t i = 0; i < count; ++i) {
    evict[i] = count >= bondEvictThreshold &&
               !(selected && !memcmp(bonds[i], selected, 6)) &&
               !(current && !memcmp(bonds[i], current, 6)) && !recent.contains(bonds[i]);
    if (evict[i]) ++evicted;
  }
  return evicted;
}
}  // namespace sweetmeter
