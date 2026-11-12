#include "net/recover.h"

#include <stdarg.h>
#include <stdio.h>

namespace net {
namespace recover {

const Policy kDefaultPolicy = {
    5u * 60u * 1000u,    // fallbackAfterMs: past the worst router reboot, before a coffee ends
    30u * 1000u,         // retryEveryMs: the cadence the hand-written loop used
    10u * 60u * 1000u,   // idleCloseAfterMs: a slow form fill on an old phone still fits
};

namespace {

// "Has `limitMs` passed since `sinceMs`", answered so that a stale reference cannot fire.
//
// The plain subtraction is what the retry loop does today and is correct for intervals shorter
// than 24.8 days, which every interval here is. What it cannot tell apart is a genuine long
// wait from a reference taken before millis() wrapped: both produce a huge unsigned number, and
// firing on the wrapped one opens the setup AP at 49 days of uptime for no reason. Diffing
// against the half range separates them - a reversed pair always lands above 0x7FFFFFFF.
//
// Refusing to fire on a stale reference is the safe answer for both timers: one would open the
// AP at random, the other would close it while somebody is filling in the form.
bool reached(uint32_t nowMs, uint32_t sinceMs, uint32_t limitMs) {
  const uint32_t diff = nowMs - sinceMs;
  return diff >= limitMs && diff <= 0x7FFFFFFFu;
}

// ---------------------------------------------------------------- self test plumbing

struct Tally {
  int total = 0;
  int failed = 0;
  LogFn log = nullptr;
  void* ctx = nullptr;
};

void line(Tally& t, const char* fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (t.log) t.log(t.ctx, buf);
}

void check(Tally& t, bool ok, const char* what) {
  t.total++;
  if (!ok) {
    t.failed++;
    line(t, "  x %s", what);
  }
}

// A provisioned, online, idle board at t=0: every action must decline on this.
Sample onlineAtZero() {
  Sample s;
  s.hasCredentials = true;
  s.stationUp = true;
  s.portalActive = false;
  s.portalAuto = false;
  s.clientPresent = false;
  s.nowMs = 0;
  s.offlineSinceMs = 0;
  s.lastAttemptMs = 0;
  s.lastClientMs = 0;
  return s;
}

// The same board with the station down since t=0.
Sample offlineAtZero() {
  Sample s = onlineAtZero();
  s.stationUp = false;
  return s;
}

const Policy& def() { return kDefaultPolicy; }

}  // namespace

Action decide(const Sample& s, const Policy& p) {
  // No credentials: setup() already opens the portal on this boot and there is nothing to
  // retry. Returning Retry here would call stationConnect("") once per tick forever.
  if (!s.hasCredentials) return Action::None;

  if (s.portalActive) {
    // Only a portal this policy opened may be closed by it. The console path, and the LAN-mode
    // page a developer opens to re-pair a working board, stay up until someone closes them.
    if (!s.portalAuto) return Action::None;
    // Somebody is on the page: never yank it, whatever the clock says. The caller refreshes
    // lastClientMs while a client stays associated, so this is belt and braces against a
    // caller that forgets.
    if (s.clientPresent) return Action::None;
    if (reached(s.nowMs, s.lastClientMs, p.idleCloseAfterMs)) return Action::ClosePortal;
    return Action::None;
  }

  if (s.stationUp) return Action::None;   // nothing to fix, nothing to retry

  // The fallback outranks a retry: past the limit, another 15 s scan-and-fail is not what the
  // situation calls for.
  if (reached(s.nowMs, s.offlineSinceMs, p.fallbackAfterMs)) return Action::OpenPortal;
  if (reached(s.nowMs, s.lastAttemptMs, p.retryEveryMs)) return Action::Retry;
  return Action::None;
}

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Tally t;
  t.log = log;
  t.ctx = ctx;

  // 1. The published numbers, because the guide and the ADR quote them.
  check(t, def().fallbackAfterMs == 300000u, "fallback is five minutes");
  check(t, def().retryEveryMs == 30000u, "retry cadence is thirty seconds");
  check(t, def().idleCloseAfterMs == 600000u, "an auto portal closes after ten idle minutes");

  // 2. Nothing due, nothing done.
  check(t, decide(onlineAtZero(), def()) == Action::None, "online at t=0: nothing to do");
  check(t, decide(offlineAtZero(), def()) == Action::None, "just went offline: wait, do not act");

  // 3. No credentials: the boot path owns this case, not the policy.
  {
    Sample s = offlineAtZero();
    s.hasCredentials = false;
    s.nowMs = 3600000;
    check(t, decide(s, def()) == Action::None, "no credentials: never retry a blank SSID");
  }

  // 4. Online stays online, however long the (irrelevant) offline clock says.
  {
    Sample s = onlineAtZero();
    s.nowMs = 3600000;
    check(t, decide(s, def()) == Action::None, "online for an hour: no fallback");
  }

  // 5. The fallback boundary, both sides. Inclusive at the limit, and it must not fire one
  //    millisecond early - this is the pair of checks that a `<` vs `<=` slip would break.
  {
    Sample s = offlineAtZero();
    s.nowMs = 299999;
    s.lastAttemptMs = 299999;               // retry not due either, so the answer is None
    check(t, decide(s, def()) == Action::None, "one ms short of the fallback: no portal");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 300000;
    check(t, decide(s, def()) == Action::OpenPortal, "exactly five minutes offline: open");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 300001;
    check(t, decide(s, def()) == Action::OpenPortal, "past five minutes offline: open");
  }

  // 6. Retrying below the limit, and the cadence itself.
  check(t, decide(offlineAtZero(), def()) == Action::None, "offline for no time: no attempt");
  {
    Sample s = offlineAtZero();
    s.nowMs = 29999;
    check(t, decide(s, def()) == Action::None, "before the retry cadence: wait");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 30000;
    check(t, decide(s, def()) == Action::Retry, "at the retry cadence: try again");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 45000;
    s.lastAttemptMs = 20000;
    check(t, decide(s, def()) == Action::None, "attempt 25 s ago: not yet");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 45000;
    s.lastAttemptMs = 15000;
    check(t, decide(s, def()) == Action::Retry, "attempt 30 s ago: due");
  }

  // 7. An automatic portal closes itself, and only when nobody is on it.
  {
    Sample s = offlineAtZero();
    s.portalActive = true;
    s.portalAuto = true;
    s.nowMs = 300000;
    s.lastClientMs = 300000;                // opened now, nobody has joined
    s.lastAttemptMs = 300000;
    check(t, decide(s, def()) == Action::None, "auto portal, just opened: keep it");
  }
  {
    Sample s = offlineAtZero();
    s.portalActive = true;
    s.portalAuto = true;
    s.nowMs = 300000 + 599999;
    s.lastClientMs = 300000;
    s.lastAttemptMs = 300000;
    check(t, decide(s, def()) == Action::None, "auto portal, idle 9m59s: keep it");
  }
  {
    Sample s = offlineAtZero();
    s.portalActive = true;
    s.portalAuto = true;
    s.nowMs = 300000 + 600000;
    s.lastClientMs = 300000;
    s.lastAttemptMs = 300000;
    check(t, decide(s, def()) == Action::ClosePortal, "auto portal, idle 10m: close it");
  }
  {
    Sample s = offlineAtZero();
    s.portalActive = true;
    s.portalAuto = true;
    s.clientPresent = true;
    s.nowMs = 300000 + 3600000;
    s.lastClientMs = 300000;
    check(t, decide(s, def()) == Action::None, "auto portal with a client: never close");
  }
  {
    // The console's portal and the LAN-mode page: somebody asked for these.
    Sample s = onlineAtZero();
    s.portalActive = true;
    s.portalAuto = false;
    s.nowMs = 3600000;
    check(t, decide(s, def()) == Action::None, "manual portal: the policy leaves it alone");
  }

  // 8. While a portal is up it owns the radio: no join attempts alongside it.
  {
    Sample s = offlineAtZero();
    s.portalActive = true;
    s.portalAuto = true;
    s.nowMs = 300000 + 100000;
    s.lastClientMs = 300000 + 100000;
    s.lastAttemptMs = 0;                    // long past the cadence
    check(t, decide(s, def()) == Action::None, "portal up: no retry beside it");
  }

  // 9. millis() wrap. A reference taken before the wrap reads as 49 days elapsed under plain
  //    subtraction, which would open the AP at random or close it under a user.
  {
    Sample s = offlineAtZero();
    s.nowMs = 1000;
    s.offlineSinceMs = 0xFFFFFF00u;         // taken just before the wrap
    s.lastAttemptMs = 0xFFFFFF00u;
    check(t, decide(s, def()) == Action::None, "stale reference after the wrap: do not fire");
  }
  {
    Sample s = offlineAtZero();
    s.nowMs = 0;
    s.offlineSinceMs = 0x80000000u;         // exactly the half range: reversed
    s.lastAttemptMs = 0;
    check(t, decide(s, def()) == Action::None, "half-range difference: treated as stale");
  }
  {
    // The same wrap, but a genuine five minutes across it: 0xFFFFFF00 + 300000 mod 2^32.
    Sample s = offlineAtZero();
    s.offlineSinceMs = 0xFFFFFF00u;
    s.nowMs = (uint32_t)(0xFFFFFF00u + 300000u);
    s.lastAttemptMs = 0xFFFFFF00u;
    check(t, decide(s, def()) == Action::OpenPortal, "five real minutes across the wrap: open");
  }

  // 10. The numbers are a parameter, not baked in: a caller that passes its own policy gets it.
  {
    Policy p = def();
    p.fallbackAfterMs = 1000;
    p.retryEveryMs = 200;
    p.idleCloseAfterMs = 2000;
    Sample s = offlineAtZero();
    s.nowMs = 1000;
    check(t, decide(s, p) == Action::OpenPortal, "custom policy: fallback at 1 s");
  }

  return SelfTestResult{t.total, t.failed};
}

}  // namespace recover
}  // namespace net
