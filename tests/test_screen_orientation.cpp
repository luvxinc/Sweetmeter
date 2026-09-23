#include <array>
#include <cassert>
#include "../firmware/src/screen_orientation.h"

using Frame = std::array<uint8_t, 4000>;
bool black(const Frame &frame, size_t x, size_t y) {
  return !(frame[x * 16 + y / 8] & (0x80 >> (y % 8)));
}
Frame rotate(const Frame &source) {
  Frame result;
  for (size_t i = 0; i < result.size(); ++i)
    result[i] = sweetmeter::rotatedPanelByte(source.data(), i);
  return result;
}
int main() {
  Frame source;
  source.fill(0xff);
  // Asymmetric corners, byte boundaries and interior pixels detect mirroring,
  // one-axis flips and the six-pixel shift from rotating 128 instead of 122.
  for (size_t x = 0; x < 250; ++x)
    for (size_t y = 0; y < 122; ++y)
      if ((x * 13 + y * 7) % 31 < 9)
        source[x * 16 + y / 8] &= ~(0x80 >> (y % 8));
  const Frame original = source;
  const Frame result = rotate(source);
  assert(source == original);
  for (size_t x = 0; x < 250; ++x) {
    for (size_t y = 0; y < 122; ++y)
      assert(black(result, x, y) == black(source, 249 - x, 121 - y));
    assert((result[x * 16 + 15] & 0x3f) == 0x3f);
  }
  assert(rotate(result) == original);
  // Padding is never visible, even if a received frame has black padding bits.
  for (size_t x = 0; x < 250; ++x) source[x * 16 + 15] &= 0xc0;
  assert(rotate(source) == result);
}
