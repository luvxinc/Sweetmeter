#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../firmware/src/update_notice.h"
#include "../firmware/src/power_policy.h"
#include "../firmware/src/bond_policy.h"
#include "../firmware/src/status_json.h"
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
  assert(!n.result(UpdateCurrent,130) && n.result(UpdateFailed,140) && n.notice==UpdateFailed);
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
static void powerTests() {
  assert(!batteryCritical(-1,-1,true));
  assert(batteryCritical(5,3900,false) && batteryCritical(50,3350,false) && !batteryCritical(6,3400,false));
  assert(batteryCritical(7,3500,true) && batteryCritical(20,3450,true) && !batteryCritical(8,3451,true));
  assert(!clockStillValid(false,false,1000,0));
  assert(clockStillValid(true,false,1000,0));  // no sleep: the XTAL-timed clock is kept
  assert(clockStillValid(true,true,1300,1000) && !clockStillValid(true,true,1301,1000));
  assert(!clockStillValid(true,true,900,1000) && !clockStillValid(true,true,1000,0));
  IdlePowerOff idle;idle.seen(0);
  assert(!idle.expired(IdlePowerOff::limitMs-1,false,false,false) && idle.expired(IdlePowerOff::limitMs,false,false,false));
  assert(!idle.expired(IdlePowerOff::limitMs,true,false,false));  // a linked computer restarts the period
  assert(!idle.expired(2*IdlePowerOff::limitMs-1,false,false,false));
  assert(!idle.expired(3*IdlePowerOff::limitMs,false,true,false) && !idle.expired(4*IdlePowerOff::limitMs-1,false,false,false));
  assert(!idle.expired(5*IdlePowerOff::limitMs,false,false,true));
  RetryBackoff retry;assert(retry.due(0));retry.failed(0);assert(!retry.due(999) && retry.due(1000));
  for(int i=0;i<10;++i) retry.failed(0);
  assert(retry.delay()==60000 && !retry.due(59999));retry.succeeded();assert(retry.due(1));
  assert(drawFrameNow(true,false) && drawFrameNow(false,true) && !drawFrameNow(false,false));
}
static void bondTests() {
  RecentPeers recent;const uint8_t a[6]={1},b[6]={2},c[6]={3},d[6]={4},e[6]={5};
  assert(recent.touch(a) && !recent.touch(a) && recent.touch(b) && recent.touch(a));
  assert(!memcmp(recent.addresses[0],a,6) && !memcmp(recent.addresses[1],b,6) && recent.count==2);
  recent.touch(c);recent.touch(d);recent.touch(e);assert(recent.count==5 && recent.contains(b));
  for(uint8_t i=0;i<recentPeerCount;++i){const uint8_t other[6]={0,i,1};recent.touch(other);}
  assert(recent.count==recentPeerCount && !recent.contains(a) && !recent.contains(e));
  uint8_t bonds[14][6]{};for(int i=0;i<14;++i) bonds[i][0]=uint8_t(10+i);
  bool evict[14];
  const uint8_t selected[6]={12},current[6]={13};
  assert(chooseBondEvictions(bonds,bondEvictThreshold-1,selected,current,RecentPeers{},evict)==0);
  RecentPeers keep;const uint8_t recentPeer[6]={20};keep.touch(recentPeer);
  size_t n=chooseBondEvictions(bonds,14,selected,current,keep,evict);
  assert(n==11 && !evict[2] && !evict[3] && !evict[10] && evict[0] && evict[13]);
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
  noticeTests();powerTests();bondTests();statusTests();
  puts("PASS: rocker notices, battery/clock/idle/retry policy, bond eviction, bounded status JSON");
}
