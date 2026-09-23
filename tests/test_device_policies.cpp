#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <initializer_list>
#include "../firmware/src/update_notice.h"
#include "../firmware/src/power_policy.h"
#include "../firmware/src/bond_policy.h"
#include "../firmware/src/display_policy.h"
#include "../firmware/src/status_json.h"
#include "../firmware/src/advertising.h"
using namespace sweetmeter;

static void noticeTests() {
  NoticeState n;
  // Distinct messages for "nothing selected" and "selected but not connected".
  assert(!n.hold(false,false,0,1) && n.notice==UpdateNotSelected && !strcmp(n.text(),"Select a computer first."));
  assert(!n.hold(true,false,0,1) && n.notice==UpdateNotConnected && !strcmp(n.text(),"Computer not connected."));
  // Unsolicited `u` is ignored unless a check is outstanding.
  assert(!n.result(UpdateCurrent,10) && n.notice==UpdateNotConnected);
  NoticeState idle;assert(!idle.result(UpdateInstalling,0) && idle.notice==NoNotice);
  assert(n.hold(true,true,100,5) && n.notice==UpdateChecking);
  assert(!n.result(1,110) && !n.result(6,110) && n.notice==UpdateChecking);
  assert(n.result(UpdateInstalling,120) && n.notice==UpdateInstalling);
  assert(!n.result(UpdateCompanion,130) && !n.result(UpdateInstalling,130) && n.result(UpdateFailed,140) && n.notice==UpdateFailed);
  // Visible results clear later (deferred draw); a stalled check fails now.
  assert(n.tick(140+noticeVisibleMs-1)==NoticeChange::None && n.tick(140+noticeVisibleMs)==NoticeChange::DrawLater && !n.notice);
  assert(n.hold(true,true,1000,7));assert(n.tick(1000+noticeCheckTimeoutMs)==NoticeChange::DrawNow && n.notice==UpdateFailed);
  // A new link generation clears an outstanding check immediately.
  assert(n.hold(true,true,2000,7) && !n.link(7) && n.link(8) && n.notice==NoNotice);
  assert(!n.link(9));  // nothing outstanding
  // The banner text always fits inside the 226-pixel box (x 12..238).
  for(uint8_t code=0;code<=UpdateNotConnected;++code) {
    NoticeState s;s.notice=code;int x=bannerTextX(s.text());
    assert(x>=12 && x+int(bannerChars(s.text()))*6<=238);
  }
  const char *longText="0123456789012345678901234567890123456789012345";
  assert(bannerChars(longText)==bannerMaxChars && bannerTextX(longText)>=12);
}
static void installNoticeTests() {
  // "Installing" is not cleared by the 8-second result timer: a transfer takes
  // minutes, and a failure reported much later must still be shown.
  NoticeState n;assert(n.hold(true,true,0,1) && n.result(UpdateInstalling,1000));
  for(uint32_t t=1000;t<1000+noticeInstallTimeoutMs;t+=noticeVisibleMs) assert(n.tick(t)==NoticeChange::None && n.notice==UpdateInstalling);
  assert(!n.link(2) && n.notice==UpdateInstalling);  // a link change does not hide it
  assert(n.result(UpdateFailed,300000) && n.notice==UpdateFailed);
  assert(n.tick(300000+noticeVisibleMs)==NoticeChange::DrawLater && !n.notice);
  // "Up to date" after Installing (the companion found nothing to install) also ends it.
  NoticeState current;current.hold(true,true,0,1);current.result(UpdateInstalling,0);
  assert(current.result(UpdateCurrent,5000) && current.notice==UpdateCurrent);
  // A failed OTA transfer shows the failure on the meter; a cancel just ends it.
  NoticeState failed;failed.hold(true,true,0,1);failed.result(UpdateInstalling,0);
  assert(failed.otaEnded(true,200000) && failed.notice==UpdateFailed);
  assert(!failed.otaEnded(true,200001));  // only while Installing
  NoticeState cancelled;cancelled.hold(true,true,0,1);cancelled.result(UpdateInstalling,0);
  assert(cancelled.otaEnded(false,1) && cancelled.notice==NoNotice);
  NoticeState none;assert(!none.otaEnded(true,0) && none.notice==NoNotice);
  // Without any answer it fails after the long deadline.
  NoticeState stalled;stalled.hold(true,true,0,1);stalled.result(UpdateInstalling,10);
  assert(stalled.tick(10+noticeInstallTimeoutMs-1)==NoticeChange::None);
  assert(stalled.tick(10+noticeInstallTimeoutMs)==NoticeChange::DrawNow && stalled.notice==UpdateFailed);
}
static void powerTests() {
  assert(!batteryCritical(-1,-1,true));
  assert(batteryCritical(5,3900,false) && batteryCritical(50,3350,false) && !batteryCritical(6,3400,false));
  assert(batteryCritical(7,3500,true) && batteryCritical(20,3450,true) && !batteryCritical(8,3451,true));
  int64_t total=0;
  assert(!clockStillValid(false,false,1000,0,total));
  assert(clockStillValid(true,false,1000,0,total) && total==0);  // no sleep: the XTAL-timed clock is kept
  assert(clockStillValid(true,true,1299,1000,total) && total==299);
  total=0;assert(!clockStillValid(true,true,1300,1000,total));  // five minutes is already untrusted
  total=0;assert(!clockStillValid(true,true,900,1000,total) && !clockStillValid(true,true,1000,0,total));
  // Slept time accumulates since the last T: two 200 s sleeps exceed the limit,
  // and repeated 300 s low-battery sleeps can never keep the clock.
  total=0;assert(clockStillValid(true,true,1200,1000,total) && !clockStillValid(true,true,5200,5000,total) && total==400);
  total=0;assert(!clockStillValid(true,true,1300,1000,total));
  IdlePowerOff idle;idle.seen(0);
  assert(!idle.expired(IdlePowerOff::limitMs-1,false,false,false) && idle.expired(IdlePowerOff::limitMs,false,false,false));
  assert(!idle.expired(IdlePowerOff::limitMs,true,false,false));  // a linked computer restarts the period
  assert(!idle.expired(2*IdlePowerOff::limitMs-1,false,false,false));
  assert(!idle.expired(3*IdlePowerOff::limitMs,false,true,false) && !idle.expired(4*IdlePowerOff::limitMs-1,false,false,false));
  assert(!idle.expired(5*IdlePowerOff::limitMs,false,false,true));
  RetryBackoff retry;assert(retry.due(0));retry.failed(0);assert(!retry.due(999) && retry.due(1000));
  for(int i=0;i<10;++i) retry.failed(0);
  assert(retry.delay()==60000 && !retry.due(59999));retry.succeeded();assert(retry.due(1));
}
static BondList bonds(std::initializer_list<uint8_t> ids,bool valid=true) {
  BondList list;list.valid=valid;
  for(uint8_t id:ids){memset(list.addresses[list.count],0,6);list.addresses[list.count][0]=id;list.addresses[list.count][5]=uint8_t(id^0x5a);++list.count;}
  return list;
}
static void bondTests() {
  uint8_t out[bondListCapacity][6];
  // Only bonds that appeared during the link are candidates, in any order.
  assert(bondsAddedDuring(bonds({1,2,3}),bonds({3,9,1,2,8}),out)==2 && out[0][0]==9 && out[1][0]==8);
  assert(bondsAddedDuring(bonds({1,2}),bonds({2,1}),out)==0);
  // A bond Bluedroid's LRU deleted during the link is never "added".
  assert(bondsAddedDuring(bonds({1,2,3}),bonds({2,3,4}),out)==1 && out[0][0]==4);
  // Nothing is removed when either list could not be read.
  assert(bondsAddedDuring(bonds({1},false),bonds({1,2}),out)==0);
  assert(bondsAddedDuring(bonds({1}),bonds({1,2},false),out)==0);
  // First boot of this firmware with many old bonds: an unearned link removes
  // only its own new bond, never the selected computer's or anyone else's.
  BondList full=bonds({1,2,3,4,5,6,7,8,9,10,11,12,13,14});
  LinkBonds link;link.connected(5,full);
  BondList after=bonds({1,2,3,4,5,6,7,8,9,10,11,12,13,14,77});
  assert(link.ended(5,after,out)==1 && out[0][0]==77);
  // A link that earned its bond (proved a secret / registered) keeps it.
  link.connected(6,full);link.earned(6);assert(link.isEarned() && link.ended(6,after,out)==0);
  // A reconnecting paired computer whose bond already existed loses nothing.
  link.connected(7,after);assert(link.ended(7,after,out)==0);
  // Earning is bound to the link generation; a stale earn does not count.
  link.connected(8,full);link.earned(7);assert(!link.isEarned() && link.ended(8,after,out)==1);
  // A second end for the same link, or an end for another link, does nothing.
  assert(link.ended(8,after,out)==0);
  link.connected(9,full);assert(link.ended(10,after,out)==0);
  // The menu race: closing during the link, or a link starting shortly after
  // the menu closed, keeps the bond the central's OS already stored.
  assert(menuRaceEarnsBond(true,0,0,true));
  assert(menuRaceEarnsBond(false,15000,10000,true) && !menuRaceEarnsBond(false,20000,10000,true));
  assert(!menuRaceEarnsBond(false,500,0,false));    // the menu was never open
  assert(!menuRaceEarnsBond(false,9000,10000,true));  // began before it closed (then "during" applies)
}
static void clockDisplayTests() {
  // T jitter below two seconds never steps a synchronized clock.
  assert(clockNeedsStep(false,1000,1000));
  assert(!clockNeedsStep(true,1000,1001) && !clockNeedsStep(true,1000,999));
  assert(clockNeedsStep(true,1000,1002) && clockNeedsStep(true,1000,998));
  assert(displayedMinute(119,0)==1 && displayedMinute(120,0)==2 && displayedMinute(0,-3600)==-60 && displayedMinute(-1,0)==-1);
  // One redraw per displayed minute; a one-second step back across a minute
  // boundary (the old 30 s T behavior) does not redraw the previous minute.
  MinuteRedraw minute;assert(minute.due(100,0));minute.painted(100);
  assert(!minute.due(100,1) && !minute.due(99,2) && minute.due(101,3));minute.painted(101);
  // A jump of more than a minute either way redraws (new T, timezone change).
  assert(minute.due(99,4) && minute.due(500,4));
  // A link's first T defers the redraw about 5 s so the first frame draws once.
  minute.painted(101);minute.firstClock(1000);
  assert(!minute.due(29000000,1000) && !minute.due(29000000,1000+MinuteRedraw::firstClockDeferMs-1));
  minute.painted(29000000);  // the first frame arrived and drew the new time
  assert(!minute.due(29000000,1000+MinuteRedraw::firstClockDeferMs));
  minute.firstClock(2000);assert(minute.due(29000005,2000+MinuteRedraw::firstClockDeferMs));  // no frame: draw after 5 s
  // Frame A results.
  assert(drawFrameNow(true,false) && drawFrameNow(false,true) && !drawFrameNow(false,false));
}
static void refreshTests() {
  // With a computer: the press is not drawn; its frame is drawn (one refresh).
  RefreshRequest r;assert(!r.press(0,true) && !r.marker && r.pending());
  assert(r.tick(RefreshRequest::fallbackMs-1)==NoticeChange::None);
  assert(r.frame() && !r.marker && !r.pending());
  assert(!r.frame());  // an ordinary later frame is deferred to the minute draw
  // No frame within 10 s: the marker acknowledges the press once.
  RefreshRequest slow;slow.press(100,true);
  assert(slow.tick(100+RefreshRequest::fallbackMs)==NoticeChange::DrawNow && slow.marker);
  assert(slow.tick(100+RefreshRequest::fallbackMs+1)==NoticeChange::None);
  assert(slow.frame() && !slow.marker);  // a late answer is still drawn now, removing the marker
  RefreshRequest never;never.press(0,true);never.tick(RefreshRequest::fallbackMs);
  assert(never.tick(RefreshRequest::answerMs)==NoticeChange::DrawLater && !never.marker && !never.pending());
  // Without a computer the press is acknowledged at once.
  RefreshRequest alone;assert(alone.press(5,false) && alone.marker);
  assert(alone.tick(5+RefreshRequest::fallbackMs)==NoticeChange::None);
  assert(alone.frame() && !alone.marker);  // a computer that reconnects answers it
  // Steady state: frames and minutes alone never add draws beyond one per minute.
  RefreshRequest idle;for(uint32_t t=0;t<600000;t+=1000) assert(idle.tick(t)==NoticeChange::None && !idle.frame());
}
static void advertisingTests() {
  static_assert(advertisementSize<=advertisingLimit,"advertisement");
  uint8_t raw[advertisingLimit];
  // Normal: name AD (17) + marker AD (7) = 24 bytes; menu: "-PAIR" name (22) + marker = 29.
  size_t closed=buildScanResponse(raw,"Sweetmeter-ABCD",false);
  assert(closed==24 && raw[0]==16 && raw[1]==0x09 && !memcmp(raw+2,"Sweetmeter-ABCD",15));
  const uint8_t markerClosed[7]={6,0xFF,0xFF,0xFF,'S','M',markerAuth};
  assert(!memcmp(raw+17,markerClosed,7));
  size_t open=buildScanResponse(raw,"Sweetmeter-ABCD",true);
  assert(open==29 && open<=advertisingLimit && raw[0]==21 && !memcmp(raw+2,"Sweetmeter-ABCD-PAIR",20));
  const uint8_t markerOpen[7]={6,0xFF,0xFF,0xFF,'S','M',markerAuth|markerMenu};
  assert(!memcmp(raw+22,markerOpen,7));
  // Every name the firmware can generate ("Sweetmeter-%04X") fits; longer names are refused, never truncated.
  assert(buildScanResponse(raw,"Sweetmeter-ABCDEFGH",true)==0);
}
static void statusTests() {
  StatusFields f;char out[600];size_t offset=0;
  f.firmware="9999.12.4294967295";f.board="elecrow-crowpanel-2.13-v1.2-jd79661";f.serial="a1b2c3d4e5f6";
  f.health="pending";f.lastUpdate="rollback";f.target="9999.12.4294967295";
  f.selected=f.secured=f.critical=f.clockSynced=f.menu=f.ota=true;
  f.battery=100;f.millivolts=4350;f.rssi=-127;f.interval=300;f.computers=12;f.nonce=4294967295u;f.remaining=60000;
  int length=formatStatus(out,sizeof(out),f,offset);
  assert(length>0 && length<512 && out[length-1]=='}');
  assert(!strncmp(out+offset,challengePlaceholder,32) && out[offset+32]=='"');
  assert(!strstr(out,"selected_host") && strstr(out,"\"auth\":1") && strstr(out,"\"serial\":\"a1b2c3d4e5f6\""));
  assert(!strstr(out,"rssi"));  // optional diagnostic omitted rather than exceeding 512
  f.target="";length=formatStatus(out,sizeof(out),f,offset);
  assert(length>0 && length<512 && strstr(out,",\"rssi\":-127}"));
  f.rssi=127;assert(formatStatus(out,sizeof(out),f,offset)>0 && !strstr(out,"rssi"));
  char tiny[100];assert(formatStatus(tiny,sizeof(tiny),f,offset)==-1);
}
int main() {
  noticeTests();installNoticeTests();powerTests();bondTests();clockDisplayTests();refreshTests();advertisingTests();statusTests();
  puts("PASS: rocker/install notices, battery/clock/idle/retry policy, per-link bond removal, clock/refresh redraw policy, bounded status JSON");
}
