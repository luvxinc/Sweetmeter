#include <cassert>
#include <cstdio>
#include "../firmware/src/link_deadline.h"

using sweetmeter::linkDeadlinePassed;

int main() {
  // Not encrypted yet: a first pairing waits for the computer's user, up to 30 s.
  assert(!linkDeadlinePassed(1000 + 10000, 1000, 0, 10000));
  assert(!linkDeadlinePassed(1000 + 29999, 1000, 0, 10000));
  assert(linkDeadlinePassed(1000 + 30000, 1000, 0, 10000));
  // Encrypted: the usual limit counts from encryption, not from connecting.
  assert(!linkDeadlinePassed(1000 + 21000, 1000, 1000 + 12001, 10000));
  assert(linkDeadlinePassed(1000 + 22001, 1000, 1000 + 12001, 10000));
  // A bonded reconnect encrypts at once, so the limit is effectively unchanged.
  assert(linkDeadlinePassed(1000 + 10301, 1000, 1301, 10000));
  // The menu's discovery link: 15 s from encryption.
  assert(!linkDeadlinePassed(5000 + 20000, 5000, 5000 + 6001, 15000));
  assert(linkDeadlinePassed(5000 + 21001, 5000, 5000 + 6001, 15000));
  // Encryption recorded by the Bluetooth task after the worker sampled `now`:
  // it has just happened, it did not expire long ago.
  assert(!linkDeadlinePassed(24000, 20000, 24050, 10000));
  assert(!linkDeadlinePassed(24000, 24100, 0, 10000));
  // Pairing again cannot extend the link: 30 s + the limit after connecting at most.
  assert(!linkDeadlinePassed(1000 + 38999, 1000, 1000 + 29000, 10000));
  assert(linkDeadlinePassed(1000 + 40000, 1000, 1000 + 35000, 10000));
  assert(linkDeadlinePassed(5000 + 45000, 5000, 5000 + 40000, 15000));
  // millis() wraps around.
  assert(!linkDeadlinePassed(5000u, 0xffffff00u, 0, 10000));
  assert(linkDeadlinePassed(0xffffff00u + 30000u, 0xffffff00u, 0, 10000));
  std::puts("link deadline tests passed");
}
