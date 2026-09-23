#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace sweetmeter {
// Rocker-hold firmware check banner. Codes 2..5 arrive from the companion as `u`.
enum UpdateNotice : uint8_t {
  NoNotice, UpdateChecking, UpdateCurrent, UpdateInstalling, UpdateFailed, UpdateCompanion,
  UpdateNotSelected, UpdateNotConnected
};
enum class NoticeChange : uint8_t { None, DrawNow, DrawLater };

// "Installing" stays until the companion reports the outcome (`u 2`/`u 4`),
// the OTA transfer ends, or this long deadline passes.
constexpr uint32_t noticeCheckTimeoutMs = 45000, noticeVisibleMs = 8000, noticeInstallTimeoutMs = 600000;
constexpr size_t bannerMaxChars = 37;  // 226-pixel banner interior, 6-pixel glyphs

class NoticeState {
 public:
  uint8_t notice = NoNotice;

  // Returns true when the device must notify `U` to the connected companion.
  bool hold(bool selected, bool linked, uint32_t now, uint32_t generation) {
    at_ = now; generation_ = generation;
    if (linked) { notice = UpdateChecking; return true; }
    notice = selected ? UpdateNotConnected : UpdateNotSelected;
    return false;
  }
  // Unsolicited results are ignored; only an outstanding check (or the
  // outcome that the companion reports after "Installing") changes the banner.
  bool result(uint8_t code, uint32_t now) {
    if (code < UpdateCurrent || code > UpdateCompanion) return false;
    bool outstanding = notice == UpdateChecking ||
                       (notice == UpdateInstalling && (code == UpdateFailed || code == UpdateCurrent));
    if (!outstanding) return false;
    notice = code; at_ = now;
    return true;
  }
  // The OTA transfer ended without restarting into the new firmware. A
  // rocker-initiated install that failed says so; a cancelled one just ends.
  bool otaEnded(bool failed, uint32_t now) {
    if (notice != UpdateInstalling) return false;
    notice = failed ? UpdateFailed : NoNotice; at_ = now;
    return true;
  }
  // A new/lost link cannot deliver the answer to a check sent on the old one.
  bool link(uint32_t generation) {
    if (generation == generation_) return false;
    generation_ = generation;
    if (notice != UpdateChecking) return false;
    notice = NoNotice;
    return true;
  }
  NoticeChange tick(uint32_t now) {
    if (notice == UpdateChecking && uint32_t(now - at_) >= noticeCheckTimeoutMs) {
      notice = UpdateFailed; at_ = now; return NoticeChange::DrawNow;
    }
    if (notice == UpdateInstalling) {
      if (uint32_t(now - at_) < noticeInstallTimeoutMs) return NoticeChange::None;
      notice = UpdateFailed; at_ = now; return NoticeChange::DrawNow;
    }
    if (notice > UpdateChecking && uint32_t(now - at_) >= noticeVisibleMs) {
      // Removing a banner is not urgent; it disappears with the next minute draw.
      notice = NoNotice; return NoticeChange::DrawLater;
    }
    return NoticeChange::None;
  }
  const char *text() const {
    static const char *const texts[] = {"", "Checking for updates...", "Firmware is up to date.",
      "Update found. Installing...", "Update check failed.", "Update the Sweetmeter app.",
      "Select a computer first.", "Computer not connected."};
    return notice <= UpdateNotConnected ? texts[notice] : "";
  }

 private:
  uint32_t at_ = 0, generation_ = 0;
};

// Centre text in the 250-pixel banner and never draw past its border.
inline size_t bannerChars(const char *text) {
  size_t length = strlen(text);
  return length > bannerMaxChars ? bannerMaxChars : length;
}
inline int bannerTextX(const char *text) { return 125 - int(bannerChars(text)) * 3; }
}  // namespace sweetmeter
