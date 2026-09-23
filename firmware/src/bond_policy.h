#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace sweetmeter {
// Arduino's BLEServer asks every central that connects to encrypt, so Bluedroid
// bonds strangers too, and once more than CONFIG_BT_SMP_MAX_BONDS (15) exist it
// silently deletes the least recently used bond (btc_ble_storage.c,
// _btc_storage_save) -- possibly a paired computer's.
//
// Root cause fix: a bond survives only a link that earned it. The bond list is
// read when a link connects; when a link ends without earning its bonds, only
// the bonds that appeared during that link (present now, absent from the
// snapshot) are removed. Bonds are compared by the identity address Bluedroid
// stores, so no address resolution is needed. If either list could not be
// read, nothing is removed. Bluedroid's own LRU remains the backstop.
//
// A link earns its bonds when the central proves a pairing secret (authorized,
// or "paired but not selected"), completes a menu registration (including an
// old app's registration that is told to update), or -- for a central that
// completed a registration from the same address in the window that just
// closed -- when the menu closed during the link or shortly before it began,
// so a computer that lost the race with the owner's selection keeps the bond
// its OS already stored. Any other central loses its bond.
constexpr size_t bondListCapacity = 16;              // >= CONFIG_BT_SMP_MAX_BONDS
constexpr uint32_t bondMenuGraceMs = 10000;          // after the menu closes

struct BondList {
  uint8_t addresses[bondListCapacity][6]{};
  uint8_t count = 0;
  bool valid = false;  // false: the list could not be read
  bool contains(const uint8_t *address) const {
    for (size_t i = 0; i < count; ++i) if (!memcmp(addresses[i], address, 6)) return true;
    return false;
  }
};

// Bonds present in `after` that were absent from `before`; none unless both
// lists were read.
inline size_t bondsAddedDuring(const BondList &before, const BondList &after, uint8_t (*out)[6]) {
  if (!before.valid || !after.valid) return 0;
  size_t added = 0;
  for (size_t i = 0; i < after.count && i < bondListCapacity; ++i)
    if (!before.contains(after.addresses[i])) memcpy(out[added++], after.addresses[i], 6);
  return added;
}

// Tracks the single current link. connected()/ended() run in the Bluetooth
// callback task, earned() in the worker; the caller serializes them.
class LinkBonds {
 public:
  void connected(uint32_t generation, const BondList &snapshot) {
    generation_ = generation; before_ = snapshot; earned_ = false; active_ = true;
  }
  void earned(uint32_t generation) { if (active_ && generation == generation_) earned_ = true; }
  bool isEarned() const { return earned_; }
  // Returns how many bonds of the ended link to remove (written to `out`).
  size_t ended(uint32_t generation, const BondList &now, uint8_t (*out)[6]) {
    if (!active_ || generation != generation_) return 0;
    active_ = false;
    return earned_ ? 0 : bondsAddedDuring(before_, now, out);
  }
 private:
  BondList before_;
  uint32_t generation_ = 0;
  bool active_ = false, earned_ = false;
};

// The menu closed while this link was open, or the link began within the
// grace period after it closed, and this central registered in that window:
// it may have lost the race with the owner's choice. A central that merely
// happened to be connected (or connected just after) earns nothing.
inline bool menuRaceEarnsBond(bool menuClosedDuringLink, uint32_t linkStartedAt, uint32_t menuClosedAt,
                              bool menuEverClosed, bool peerRegistered) {
  return peerRegistered && (menuClosedDuringLink ||
         (menuEverClosed && uint32_t(linkStartedAt - menuClosedAt) < bondMenuGraceMs));
}
}  // namespace sweetmeter
