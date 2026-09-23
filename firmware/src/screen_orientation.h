#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sweetmeter {
// Rotate only the 250 x 122 visible pixels. Each column has six padding bits;
// reversing the entire 4000-byte buffer would shift the picture by six pixels.
// Leave the logical frame unchanged for drawing, change detection and BLE CRCs.
inline uint8_t rotatedPanelByte(const uint8_t *frame, size_t index) {
  const size_t sourceX = 249 - index / 16;
  const size_t firstY = (index % 16) * 8;
  uint8_t result = 0xff;
  for (size_t bit = 0; bit < 8 && firstY + bit < 122; ++bit) {
    const size_t sourceY = 121 - firstY - bit;
    if (!(frame[sourceX * 16 + sourceY / 8] & (0x80 >> (sourceY % 8))))
      result &= ~(0x80 >> bit);
  }
  return result;
}
}  // namespace sweetmeter
