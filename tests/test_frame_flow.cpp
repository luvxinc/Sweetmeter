#include <cassert>
#include <deque>
#include <cstdio>

// Discrete-event regression for the real four-packet callback queue + blocking
// e-ink hardware. A GATT response arrives on enqueue; it is deliberately faster
// than the 2-second panel operation. This is a transport scheduling model, not
// a claim that the physical panel or BLE stack has been exercised.
enum class Event { Hello, Clock, Begin, Data, Commit };
struct Result { unsigned dropped=0, received=0; bool acknowledged=false; };
Result exercise(bool awaitBeginReady,bool deferDrawWhileReceiving,bool drawAfterHello,bool minuteDuringReceive) {
  constexpr unsigned chunks=223; // 4000-byte frame at minimum-MTU 18-byte payload.
  std::deque<Event> queue;
  Result result;
  bool helloAck=false,beginReady=false,receiving=false,clockSent=false,beginSent=false,commitSent=false;
  bool uiDirty=false,minuteTriggered=false;
  unsigned sent=0,busyUntil=0;
  auto enqueue=[&](Event event) { if(queue.size()==4) ++result.dropped; else queue.push_back(event); };
  enqueue(Event::Hello);
  for(unsigned tick=0;tick<12000 && !result.acknowledged;++tick) {
    // The host explicitly waits for H, then sends T/B. Only protocol 4 waits for b.
    if(helloAck) {
      if(!clockSent) {enqueue(Event::Clock);clockSent=true;}
      else if(!beginSent) {enqueue(Event::Begin);beginSent=true;}
      else if(!awaitBeginReady || beginReady) {
        if(sent<chunks) {enqueue(Event::Data);++sent;}
        else if(!commitSent) {enqueue(Event::Commit);commitSent=true;}
      }
    }
    if(tick<busyUntil) continue;
    if(!queue.empty()) {
      Event event=queue.front();queue.pop_front();
      switch(event) {
        case Event::Hello: helloAck=true;uiDirty=drawAfterHello;break;
        case Event::Clock:break;
        case Event::Begin: receiving=true;beginReady=true;break;
        case Event::Data:
          if(receiving)++result.received;
          if(minuteDuringReceive && result.received==10 && !minuteTriggered) {uiDirty=true;minuteTriggered=true;}
          break;
        case Event::Commit:
          if(receiving && result.received==chunks) result.acknowledged=true;
          receiving=false;break;
      }
    }
    if(uiDirty && (!deferDrawWhileReceiving || !receiving)) {busyUntil=tick+2000;uiDirty=false;}
  }
  return result;
}
int main() {
  const auto oldHello=exercise(false,false,true,false);
  assert(oldHello.dropped>0 && !oldHello.acknowledged);
  const auto readyHello=exercise(true,true,true,false);
  assert(readyHello.dropped==0 && readyHello.acknowledged && readyHello.received==223);
  const auto readyWithoutDrawGuard=exercise(true,false,false,true);
  assert(readyWithoutDrawGuard.dropped>0 && !readyWithoutDrawGuard.acknowledged);
  const auto minuteGuard=exercise(true,true,false,true);
  assert(minuteGuard.dropped==0 && minuteGuard.acknowledged && minuteGuard.received==223);
  puts("Frame queue timing regression passed: b-ready + receive draw guard required");
}
