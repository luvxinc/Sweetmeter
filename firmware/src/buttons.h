#pragma once
#include <stdint.h>

// Short top/bottom presses fire on release so a long press has only one action.
// A key already held at boot must be released before it can produce an event.
class MeterButton {
 public:
  MeterButton(uint8_t shortEvent, uint8_t longEvent = 0)
      : shortEvent_(shortEvent), longEvent_(longEvent) {}

  uint8_t update(bool down, uint32_t now) {
    if (!initialized_) {
      initialized_ = true; raw_ = stable_ = down; blocked_ = down;
      changedAt_ = now; return 0;
    }
    if (down != raw_) { raw_ = down; changedAt_ = now; }
    uint8_t event = 0;
    if (raw_ != stable_ && uint32_t(now - changedAt_) >= 30) {
      stable_ = raw_;
      if (!stable_) {
        if (!blocked_ && !longFired_ && longEvent_) event = shortEvent_;
        blocked_ = false; longFired_ = false;
      } else {
        downAt_ = now;
        if (!blocked_ && !longEvent_) event = shortEvent_;
      }
    }
    if (stable_ && raw_ && !blocked_ && !longFired_ && longEvent_ &&
        uint32_t(now - downAt_) >= 3000) {
      longFired_ = true; event = longEvent_;
    }
    return event;
  }

 private:
  uint8_t shortEvent_, longEvent_;
  bool initialized_ = false, raw_ = false, stable_ = false;
  bool blocked_ = false, longFired_ = false;
  uint32_t changedAt_ = 0, downAt_ = 0;
};
