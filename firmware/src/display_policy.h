#pragma once
#include <stdint.h>
#include "update_notice.h"

// When the e-paper panel is redrawn. Every draw is a full two-pass refresh, so
// in steady state the panel refreshes at most once per minute; only
// user-visible events draw immediately.
namespace sweetmeter {
// Frame A result 0 = drawn now; 1 = accepted, drawn with the next minute
// boundary (the clock redraw). Only the first dashboard after boot and a
// dashboard answering a physical refresh press are user-visible immediately.
constexpr uint8_t frameDisplayed = 0, frameDeferred = 1;
inline bool drawFrameNow(bool firstFrame, bool userRefresh) { return firstFrame || userRefresh; }
// A draw normally skips the panel when nothing visible changed. The frame that
// answers a top-button press is refreshed even if identical: that refresh is
// the press's only visible acknowledgement (one refresh per press).
inline bool panelRefreshNeeded(bool contentChanged, bool panelReady, bool answersPress) {
  return contentChanged || !panelReady || answersPress;
}

// The companion sends T every 30 seconds. Its whole-second clock and the BLE
// latency make it differ from the meter's by up to a second; stepping the
// clock back for that could redraw the previous minute. Small differences are
// ignored once the clock is synchronized.
constexpr int64_t clockStepToleranceSeconds = 2;
inline bool clockNeedsStep(bool synced, int64_t current, int64_t incoming) {
  if (!synced) return true;
  int64_t difference = incoming - current;
  return difference >= clockStepToleranceSeconds || difference <= -clockStepToleranceSeconds;
}
inline int64_t displayedMinute(int64_t seconds, int32_t utcOffset) {
  int64_t local = seconds + utcOffset;
  return local >= 0 ? local / 60 : -((-local + 59) / 60);
}

// The scheduled clock redraw: only when the displayed minute advances, or the
// clock jumps by more than a minute (a new T, a timezone change). A link's first
// T defers it briefly so the first frame, which usually follows within a
// second or two, is drawn together with the new time in one refresh.
class MinuteRedraw {
 public:
  static constexpr uint32_t firstClockDeferMs = 5000;
  void painted(int64_t minute) { minute_ = minute; painted_ = true; }
  void firstClock(uint32_t now) { deferUntil_ = now + firstClockDeferMs; deferring_ = true; }
  bool due(int64_t minute, uint32_t now) {
    if (deferring_) {
      if (int32_t(now - deferUntil_) < 0) return false;
      deferring_ = false;
    }
    if (!painted_) return true;
    return minute > minute_ || minute < minute_ - 1;
  }
 private:
  int64_t minute_ = 0;
  uint32_t deferUntil_ = 0;
  bool painted_ = false, deferring_ = false;
};

// Top-button refresh. With an authorized computer the press is not drawn: the
// answering frame is drawn at once instead (one refresh, not two). If no frame
// arrives within 10 s, the "*" marker is drawn so the press is visibly
// acknowledged. Without a computer the marker is drawn immediately. A frame
// arriving within 30 s of the press still counts as its answer.
class RefreshRequest {
 public:
  static constexpr uint32_t fallbackMs = 10000, answerMs = 30000;
  bool marker = false;  // "*" in the header
  // Returns true when the press itself must be drawn now.
  bool press(uint32_t now, bool linked) {
    pending_ = true; at_ = now;
    if (linked) return false;
    marker = true; return true;
  }
  bool pending() const { return pending_; }
  // A frame arrived: true when it answers a press and must be drawn now.
  bool frame() {
    if (!pending_) return false;
    pending_ = false; marker = false;
    return true;
  }
  NoticeChange tick(uint32_t now) {
    if (!pending_) return NoticeChange::None;
    uint32_t elapsed = uint32_t(now - at_);
    if (elapsed >= answerMs) {
      pending_ = false;
      if (!marker) return NoticeChange::None;
      marker = false; return NoticeChange::DrawLater;  // removed with the minute draw
    }
    if (!marker && elapsed >= fallbackMs) { marker = true; return NoticeChange::DrawNow; }
    return NoticeChange::None;
  }
 private:
  uint32_t at_ = 0;
  bool pending_ = false;
};
}  // namespace sweetmeter
