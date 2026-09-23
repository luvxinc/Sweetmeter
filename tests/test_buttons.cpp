#include "../firmware/src/buttons.h"
#include <cassert>
#include <iostream>

int main() {
  MeterButton top(1, 64);
  assert(top.update(false, 0) == 0);
  assert(top.update(true, 100) == 0);
  assert(top.update(false, 110) == 0); // Contact bounce must not refresh.
  assert(top.update(true, 120) == 0);
  assert(top.update(true, 150) == 0);
  assert(top.update(false, 200) == 0);
  assert(top.update(false, 230) == 1);
  assert(top.update(false, 260) == 0);
  assert(top.update(true, 300) == 0);
  assert(top.update(true, 330) == 0);
  assert(top.update(true, 3329) == 0);
  assert(top.update(true, 3330) == 64);
  assert(top.update(true, 8000) == 0);
  assert(top.update(false, 8010) == 0);
  assert(top.update(false, 8040) == 0); // No refresh after shutdown gesture.

  MeterButton wake(1, 64);
  assert(wake.update(true, 0) == 0);
  assert(wake.update(true, 6000) == 0); // Held wake key cannot re-enter sleep.
  assert(wake.update(false, 6010) == 0);
  assert(wake.update(false, 6040) == 0);
  assert(wake.update(true, 6100) == 0);
  assert(wake.update(true, 6130) == 0);
  assert(wake.update(false, 6200) == 0);
  assert(wake.update(false, 6230) == 1);

  MeterButton bottom(2, 32);
  assert(bottom.update(false, 0xffffff00u) == 0);
  assert(bottom.update(true, 0xfffffff0u) == 0);
  assert(bottom.update(true, 14) == 0); // Debounce crosses millis rollover.
  assert(bottom.update(true, 3013) == 0);
  assert(bottom.update(true, 3014) == 32);
  assert(bottom.update(false, 3020) == 0);
  assert(bottom.update(false, 3050) == 0);

  MeterButton wheel(16);
  assert(wheel.update(false, 0) == 0);
  assert(wheel.update(true, 10) == 0);
  assert(wheel.update(true, 40) == 16);
  assert(wheel.update(true, 4000) == 0);
  assert(wheel.update(false, 4100) == 0);
  assert(wheel.update(false, 4130) == 0);

  MeterButton rocker(16, 128);
  assert(rocker.update(false, 0) == 0);
  assert(rocker.update(true, 10) == 0);
  assert(rocker.update(true, 40) == 0); // Selection waits for release.
  assert(rocker.update(false, 100) == 0);
  assert(rocker.update(false, 130) == 16);
  assert(rocker.update(true, 200) == 0);
  assert(rocker.update(true, 230) == 0);
  assert(rocker.update(true, 3230) == 128); // Hold requests a firmware check.
  assert(rocker.update(false, 3300) == 0);
  assert(rocker.update(false, 3330) == 0); // No selection after the hold.
  std::cout << "PASS: short/long press, bounce, wake hold, rollover, wheel, rocker hold\n";
}
