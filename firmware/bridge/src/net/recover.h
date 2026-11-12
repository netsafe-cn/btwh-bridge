// When to stop trusting the stored Wi-Fi and offer the setup page instead.
//
// Background (ADR-018). The setup AP is deliberately not left running: a page that can rewrite
// the credentials should not be reachable on a bridge that is working. That is the right default
// and the wrong only option, because the only other way back to that page is a USB cable and the
// `portal` console command - which is fine for whoever flashed the board and useless for the
// person who owns the heater. Rename the SSID, change the password, replace the router, and the
// board reconnects forever with no way to be told otherwise.
//
// So the AP comes back by itself, under two limits that keep it a fallback rather than a state:
//
//   * it appears only after fallbackAfterMs with no IP, so a router that is merely rebooting is
//     left alone - 30 s attempts keep running the whole time;
//   * an automatic portal closes itself after idleCloseAfterMs with nobody associated, and the
//     attempts resume. A transient outage therefore costs a bounded window, not the bridge.
//
// Why the trigger is elapsed time and not a failure count: a failed attempt and a slow one cost
// the same 15 s join budget, so "three failures" is "45 s" spelled a less useful way. Time also
// covers the cases a count cannot see - the board booting while the router is still off, or an
// association that succeeds onto an AP with nothing behind it.
//
// Nothing here includes Arduino. decide() is a pure function of the samples, which is what lets
// the host self test pin the boundaries - including the millis() wrap, where a naive subtraction
// reads 49 days of uptime as an instant timeout.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_RECOVER_H
#define BTWH_NET_RECOVER_H

#include <stdint.h>

#include "net/selftest.h"

namespace net {
namespace recover {

// What the caller should do this tick. Every action is the caller's to execute: this module
// never touches Wi-Fi, so the board boundary stays the only place that knows the radio exists.
enum class Action {
  None,
  Retry,        // one station join attempt is due
  OpenPortal,   // bring up the setup AP (automatic, so it may close itself again)
  ClosePortal,  // take that AP down and go back to retrying
};

struct Policy {
  uint32_t fallbackAfterMs;   // no IP this long -> offer the setup page
  uint32_t retryEveryMs;      // station join cadence while offline
  uint32_t idleCloseAfterMs;  // automatic portal with nobody on it this long -> close
};

// 5 min / 30 s / 10 min. The middle one is unchanged from the hand-written retry it replaces.
// 5 min is past the worst router reboot and short enough that an owner who just changed
// something sees the AP within one coffee; 10 min is a slow form fill on an old phone.
extern const Policy kDefaultPolicy;

// One tick's worth of observation. All of it is read from the board, none of it is remembered
// here - the caller owns the clocks, which is why the wrap rules below are worth reading.
struct Sample {
  bool hasCredentials;   // NVS holds an SSID
  bool stationUp;        // associated with an IP right now
  bool portalActive;     // the setup page is being served
  bool portalAuto;       // ...and this policy opened it (the console path never closes itself)
  bool clientPresent;    // at least one station associated with the setup AP
  uint32_t nowMs;        // millis()
  uint32_t offlineSinceMs;  // last tick the station was seen up; boot; or the last portal close
  uint32_t lastAttemptMs;   // last stationConnect() attempt; boot
  uint32_t lastClientMs;    // last tick a client was on the AP; the open time if never
};

// Decide what to do with the current sample. Pure: same input, same answer, no side effects.
//
// Two obligations the caller takes on by acting on the result:
//   * ClosePortal this tick means set offlineSinceMs = now, or the fallback is still satisfied
//     next tick and the AP reopens immediately - an open/close flap with no purpose.
//   * while stationUp, keep moving offlineSinceMs forward to now, so the first outage is
//     measured from the drop rather than from boot.
Action decide(const Sample& s, const Policy& p);

// Suites: the boundary of each timer, the wrap, and every combination where an action must
// NOT fire. The negatives outnumber the positives on purpose; a fallback that opens at the
// wrong time is the failure mode that is hard to notice and expensive to have.
SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace recover
}  // namespace net

#endif  // BTWH_NET_RECOVER_H
