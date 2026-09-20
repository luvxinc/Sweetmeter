#pragma once
#include <stdint.h>

// Arm only after the selected companion has completed its identity handshake.
// Unrelated connections/disconnections must neither cancel nor extend the grace.
// Caller serializes access between the Bluetooth callback and the main loop.
class DisconnectSleep {
 public:
  static constexpr uint32_t graceMs = 30000;

  void targetConnected() { armed_ = true; counting_ = false; }
  void targetDisconnected(uint32_t now) {
    if (armed_ && !counting_) { counting_ = true; lostAt_ = now; }
  }
  bool counting() const { return counting_; }
  uint32_t elapsed(uint32_t now) const { return uint32_t(now - lostAt_); }
  bool expired(uint32_t now) const { return counting_ && elapsed(now) >= graceMs; }

 private:
  bool armed_ = false, counting_ = false;
  uint32_t lostAt_ = 0;
};
