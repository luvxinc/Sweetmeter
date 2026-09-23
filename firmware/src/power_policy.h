#pragma once
#include <stdint.h>

namespace sweetmeter {
// Critical battery (valid MAX17048 reading only): enter at <=5% or <=3350 mV.
// Once latched, leave only above 7% and 3450 mV so a recovering cell does not
// bounce between a full BLE/panel boot and low-battery sleep.
inline bool batteryCritical(int percent, int millivolts, bool latched) {
  if (percent < 0) return false;
  if (percent <= 5 || millivolts <= 3350) return true;
  return latched && (percent <= 7 || millivolts <= 3450);
}

// Deep sleep keeps time on the internal RC oscillator (about +/-5%). The time
// slept since the last companion T is accumulated in RTC memory; the clock is
// trusted only while that total stays below five minutes (so repeated
// 300-second low-battery sleeps never keep a drifting clock); otherwise it
// shows --:-- until T arrives. T resets the total to zero.
constexpr int64_t clockTrustAfterSleepSeconds = 300;
inline bool clockStillValid(bool synced, bool wokeFromSleep, int64_t now, int64_t sleptAt, int64_t &sleptTotal) {
  if (!synced) return false;
  if (!wokeFromSleep) return true;
  if (sleptAt <= 0 || now < sleptAt || sleptTotal < 0) return false;
  sleptTotal += now - sleptAt;
  return sleptTotal < clockTrustAfterSleepSeconds;
}

// Power off when no selected computer has been authorized for a long period,
// except while the owner is in the selection menu or an update is running.
class IdlePowerOff {
 public:
  static constexpr uint32_t limitMs = 30u * 60u * 1000u;
  void seen(uint32_t now) { lastAt_ = now; }
  bool expired(uint32_t now, bool linked, bool menu, bool ota) {
    if (linked || menu || ota) { lastAt_ = now; return false; }
    return uint32_t(now - lastAt_) >= limitMs;
  }
 private:
  uint32_t lastAt_ = 0;
};

// Exponential retry for a failing persistent write: 1 s, 2 s, ... 60 s.
class RetryBackoff {
 public:
  bool due(uint32_t now) const { return !delay_ || uint32_t(now - at_) >= delay_; }
  void failed(uint32_t now) { delay_ = delay_ ? (delay_ >= 30000 ? 60000 : delay_ * 2) : 1000; at_ = now; }
  void succeeded() { delay_ = 0; }
  uint32_t delay() const { return delay_; }
 private:
  uint32_t delay_ = 0, at_ = 0;
};

}  // namespace sweetmeter
