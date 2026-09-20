#include "../firmware/src/disconnect_sleep.h"
#include <cassert>
#include <iostream>

int main() {
  DisconnectSleep timer;
  timer.targetDisconnected(0);
  assert(!timer.expired(600000)); // Initial setup has never connected to a target.
  timer.targetConnected();
  assert(!timer.expired(600000)); // Connected devices do not time out.
  timer.targetDisconnected(1000);
  assert(!timer.expired(30999));
  assert(timer.expired(31000));

  timer.targetConnected();
  timer.targetDisconnected(40000);
  timer.targetConnected(); // Recovery during the grace cancels shutdown.
  assert(!timer.counting());
  assert(!timer.expired(70001));
  timer.targetDisconnected(100000);
  timer.targetDisconnected(120000); // Other/disconnected peers cannot extend it.
  assert(!timer.expired(129999));
  assert(timer.expired(130000));

  timer.targetConnected();
  timer.targetDisconnected(0xfffffff0u);
  assert(!timer.expired(29983));
  assert(timer.expired(29984)); // millis wraps without losing the deadline.
  timer.targetConnected();
  assert(!timer.expired(90000));
  std::cout << "PASS: initial pairing, connected idle, 30s boundary, reconnect cancellation, peer churn, rollover\n";
}
