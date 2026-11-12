#include "net/wifistat.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace net {
namespace wifistat {
namespace {

// One buffer per function. An unknown status and an unknown reason can be rendered in the same
// printf ("status=9 ... reason=9999"), and a shared buffer would print the second number twice.
char gStatusBuf[48];
char gReasonBuf[64];

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

}  // namespace

const char* statusText(int wl) {
  switch (wl) {
    case 0: return "the join has not started";
    case 1: return "no network with that name is broadcasting here (switched off, renamed, out "
                   "of range, or 5 GHz only)";
    case 2: return "a scan finished, the join has not run yet";
    case 3: return "connected";
    case 4: return "the access point refused the association";
    case 5: return "the connection was lost";
    case 6: return "wrong password";
    case 7: return "disconnected";
    case 255: return "no Wi-Fi hardware at all (wrong board or a broken core)";
    default: break;
  }
  snprintf(gStatusBuf, sizeof(gStatusBuf), "status %d (no wording for it in this file)", wl);
  return gStatusBuf;
}

const char* reasonName(int reason) {
  switch (reason) {
    case 2: return "AUTH_EXPIRE";
    case 3: return "AUTH_LEAVE";
    case 4: return "ASSOC_EXPIRE";
    case 5: return "ASSOC_TOOMANY";
    case 6: return "NOT_AUTHED";
    case 7: return "NOT_ASSOCED";
    case 8: return "ASSOC_LEAVE";
    case 9: return "ASSOC_NOT_AUTHED";
    case 15: return "4WAY_HANDSHAKE_TIMEOUT";
    case 16: return "GROUP_KEY_UPDATE_TIMEOUT";
    case 17: return "IE_IN_DIFFERENT_CHANNEL";
    case 200: return "BEACON_TIMEOUT";
    case 201: return "NO_AP_FOUND";
    case 202: return "AUTH_FAIL";
    case 203: return "ASSOC_FAIL";
    case 204: return "HANDSHAKE_TIMEOUT";
    case 205: return "CONNECTION_FAIL";
    // 210-212 are the modern "the AP is there but not for us" family: WPA3-only, an auth mode
    // the core cannot do, or a signal too weak for the configured threshold. They are the ones
    // that look like a wrong password from the outside and are not.
    case 210: return "NO_AP_FOUND_W_COMPATIBLE_SECURITY";
    case 211: return "NO_AP_FOUND_IN_AUTHMODE_THRESHOLD";
    case 212: return "NO_AP_FOUND_IN_RSSI_THRESHOLD";
    default: return "";
  }
}

const char* reasonText(int reason) {
  switch (reason) {
    case 0: return "the driver did not say why";
    case 2: return "the access point gave up waiting for the handshake (slow, or bad credentials)";
    case 3: return "the access point ended the authentication";
    case 4: return "association timed out";
    case 5: return "the access point has no room for another client";
    case 6: return "a frame arrived before authentication finished";
    case 7: return "a frame arrived before association finished";
    case 8: return "the access point dropped the association";
    case 9: return "the access point does not accept this client";
    case 15: return "the password was rejected (4-way handshake timed out); a WPA3-only access "
                    "point lands here too";
    case 16: return "the group key exchange timed out";
    case 17: return "the access point changed channel during the join";
    case 200: return "the beacons stopped arriving (out of range, or the AP went down)";
    case 201: return "the access point was never heard: wrong name, switched off, out of range, "
                     "or 5 GHz only";
    case 202: return "authentication was refused (wrong password, or a security mode this board "
                     "cannot do)";
    case 203: return "association was refused (the AP's client limit, or a MAC filter)";
    case 204: return "the handshake timed out";
    case 205: return "the access point answered, then went quiet";
    case 210: return "no access point with that name offered a security mode this board supports "
                     "(WPA3-only is the usual cause)";
    case 211: return "no access point with that name matched the authentication mode this board "
                     "can use";
    case 212: return "the signal was below the join threshold";
    default: break;
  }
  snprintf(gReasonBuf, sizeof(gReasonBuf), "reason code %d (see esp_wifi_types.h)", reason);
  return gReasonBuf;
}

const char* failureText(int wl, int reason) {
  // The reason code wins whenever we have a name for it: 201 and 15 say something the status
  // cannot, and both are reported as status 4 or 7.
  if (reasonName(reason)[0]) return reasonText(reason);
  return statusText(wl);
}

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Tally t;
  t.log = log;
  t.ctx = ctx;

  // 1. Every code this file claims to map renders something.
  const int statuses[] = {0, 1, 2, 3, 4, 5, 6, 7, 255};
  for (size_t i = 0; i < sizeof(statuses) / sizeof(statuses[0]); i++) {
    check(t, statusText(statuses[i])[0] != 0, "a known status renders text");
  }
  const int reasons[] = {0, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 17, 200, 201, 202, 203, 204, 205,
                         210, 211, 212};
  for (size_t i = 0; i < sizeof(reasons) / sizeof(reasons[0]); i++) {
    check(t, reasonText(reasons[i])[0] != 0, "a known reason renders text");
  }

  // 2. The two cases of the day: an AP that is not there, and a password that is not right.
  //    They must not read the same, or the log is back to "three guesses in one line".
  check(t, reasonName(201)[0] != 0 && strcmp(reasonName(201), "NO_AP_FOUND") == 0,
        "201 is NO_AP_FOUND by name");
  check(t, strcmp(reasonName(15), "4WAY_HANDSHAKE_TIMEOUT") == 0, "15 is the handshake timeout");
  check(t, strcmp(failureText(7, 201), reasonText(201)) == 0,
        "no AP found: the reason beats the status");
  check(t, strcmp(failureText(7, 15), reasonText(15)) == 0,
        "rejected password: the reason beats the status");
  check(t, strcmp(reasonText(201), reasonText(15)) != 0,
        "an AP that is off and a wrong password do not read the same");

  // 3. Distinctness across the mapped set: a copy-paste that leaves two codes sharing one
  //    sentence would silently collapse two causes into one diagnosis.
  bool distinct = true;
  for (size_t i = 0; i < sizeof(reasons) / sizeof(reasons[0]); i++) {
    for (size_t j = i + 1; j < sizeof(reasons) / sizeof(reasons[0]); j++) {
      if (strcmp(reasonText(reasons[i]), reasonText(reasons[j])) == 0) distinct = false;
    }
  }
  check(t, distinct, "no two reason codes share a sentence");

  // 4. Unknown numbers keep the number - that is the only case a bug report has to work with.
  check(t, strstr(statusText(9), "9") != nullptr, "an unknown status keeps its number");
  check(t, strstr(reasonText(9999), "9999") != nullptr, "an unknown reason keeps its number");
  check(t, reasonName(9999)[0] == 0, "an unknown reason has no name");

  // 5. The status fallback when the driver reported no reason at all (0 is the common one).
  check(t, strcmp(failureText(1, 0), statusText(1)) == 0,
        "no reason code: the status text is used");
  check(t, strcmp(failureText(6, 0), statusText(6)) == 0, "status 6 alone still says wrong password");

  // 6. Two unknowns rendered together must not share the buffer (this is why there are two).
  {
    const char* a = statusText(9);
    const char* b = reasonText(9999);
    check(t, strstr(a, "9") != nullptr && strstr(b, "9999") != nullptr,
          "an unknown status and an unknown reason survive the same printf");
  }

  return SelfTestResult{t.total, t.failed};
}

}  // namespace wifistat
}  // namespace net
