#include "net/provision.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace net {
namespace {

bool sixDigits(const char* s) {
  if (!s || strlen(s) != 6) return false;
  for (int i = 0; i < 6; i++) if (s[i] < '0' || s[i] > '9') return false;
  return true;
}

bool macText(const char* s) {
  if (!s) return false;
  int pairs = 0;
  const char* p = s;
  while (*p && pairs < 6) {
    if (*p == ':' || *p == '-') { p++; continue; }
    if (!((p[0] >= '0' && p[0] <= '9') || (p[0] >= 'a' && p[0] <= 'f') || (p[0] >= 'A' && p[0] <= 'F'))
        || !((p[1] >= '0' && p[1] <= '9') || (p[1] >= 'a' && p[1] <= 'f')
             || (p[1] >= 'A' && p[1] <= 'F'))) return false;
    pairs++;
    p += 2;
  }
  return pairs == 6 && *p == 0;
}

}  // namespace

const char* provStateText(ProvState s) {
  switch (s) {
    case ProvState::kUnprovisioned: return "unpaired";
    case ProvState::kProvisioning:  return "provisioning";
    case ProvState::kConnecting:    return "reconnecting";
    case ProvState::kPaired:        return "paired";
    case ProvState::kNeedsRepair:   return "needs_code";
  }
  return "unknown";
}

const char* validateRequest(const ProvisionRequest& r) {
  // The heater must come from the scan list; an address typed by hand has a typo rate that is
  // not worth tolerating when the dropdown exists.
  if (!macText(r.heater)) return "pick a heater from the list";
  if (r.ssid[0] == 0) return "the Wi-Fi network is required";
  if (strlen(r.ssid) > 32) return "the Wi-Fi name is too long";

  // A code, when present, is exactly six digits - checked before anything is transmitted,
  // because a malformed attempt still counts toward the heater's three-failure lockout.
  if (r.pairCode[0] != 0 && !sixDigits(r.pairCode))
    return "the pairing code is six digits from the heater panel";
  return nullptr;
}

// ---------------------------------------------------------------- self test

namespace {

struct Tally {
  int total = 0;
  int failed = 0;
  LogFn log = nullptr;
  void* ctx = nullptr;
};

void check(Tally& t, bool ok, const char* what) {
  t.total++;
  if (!ok) {
    t.failed++;
    if (t.log) {
      char buf[128];
      snprintf(buf, sizeof(buf), "  x %s", what);
      t.log(t.ctx, buf);
    }
  }
}

// A well-formed request, mutated per case: every check starts from a valid baseline so a
// failure names the one broken field, not an unrelated one.
ProvisionRequest good() {
  ProvisionRequest r{};
  strcpy(r.heater,   "5c:f8:21:eb:6f:5f");
  strcpy(r.ssid,     "home-net");
  strcpy(r.wifiPass, "wpa2secret");
  strcpy(r.pairCode, "123456");
  return r;
}

void checkValidate(Tally& t, const ProvisionRequest& r, bool wantOk, const char* label) {
  const char* why = validateRequest(r);
  t.total++;
  const bool ok = (wantOk && why == nullptr) || (!wantOk && why != nullptr);
  if (!ok) {
    t.failed++;
    if (t.log) {
      char buf[160];
      snprintf(buf, sizeof(buf), "  x validateRequest(%s): got %s", label, why ? why : "ok");
      t.log(t.ctx, buf);
    }
  }
}

}  // namespace

namespace prov {

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Tally t;
  t.log = log;
  t.ctx = ctx;

  // 1. provStateText mirrors the pair_state attribute Home Assistant publishes. If these two
  //    drift, HA shows a different picture of the board than the board has of itself - which
  //    is exactly the kind of lie a remote user cannot debug. Pin all five words.
  check(t, strcmp(provStateText(ProvState::kUnprovisioned), "unpaired") == 0,
        "state text: unprovisioned");
  check(t, strcmp(provStateText(ProvState::kProvisioning), "provisioning") == 0,
        "state text: provisioning");
  check(t, strcmp(provStateText(ProvState::kConnecting), "reconnecting") == 0,
        "state text: connecting");
  check(t, strcmp(provStateText(ProvState::kPaired), "paired") == 0, "state text: paired");
  check(t, strcmp(provStateText(ProvState::kNeedsRepair), "needs_code") == 0,
        "state text: needs repair");

  // 2. The baseline is valid - every later failure must be attributable to its own mutation.
  checkValidate(t, good(), true, "baseline");

  // 3. Heater address: the form's dropdown sends lowercase colon-separated; the parser also
  //    accepts dash-separated and uppercase, and must refuse anything a typo produces.
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "5c-f8-21-eb-6f-5f");
    checkValidate(t, r, true, "dash-separated address");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "5C:F8:21:EB:6F:5F");
    checkValidate(t, r, true, "uppercase hex address");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "5c:f8:21:eb:6f");          // five pairs
    checkValidate(t, r, false, "short address");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "5c:f8:21:eb:6f:5g");       // not hex
    checkValidate(t, r, false, "non-hex digit");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "5c:f8:21:eb:6f:5ff");      // trailing junk after six pairs
    checkValidate(t, r, false, "trailing junk");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.heater, "");                        // the classic "form not filled"
    checkValidate(t, r, false, "empty address");
  }

  // 4. Wi-Fi SSID: required, 32 chars is the 802.11 maximum - and the field is char[33], so
  //    32 + NUL fills it exactly. A longer SSID cannot even be constructed in the struct,
  //    which is the field size doing part of the validation's job.
  {
    ProvisionRequest r = good();
    strcpy(r.ssid, "");
    checkValidate(t, r, false, "empty ssid");
  }
  {
    ProvisionRequest r = good();
    memset(r.ssid, 'a', 32);
    r.ssid[32] = 0;
    checkValidate(t, r, true, "32-char ssid fills the field exactly");
  }

  // 5. Pairing code: six digits or empty (keep existing pairing). This is the lockout guard -
  //    a malformed attempt still burns one of the heater's three failures, so the boundary
  //    cases matter more than they look.
  {
    ProvisionRequest r = good();
    strcpy(r.pairCode, "");
    checkValidate(t, r, true, "empty code keeps the existing pairing");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.pairCode, "12345");
    checkValidate(t, r, false, "five-digit code");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.pairCode, "1234567");
    checkValidate(t, r, false, "seven-digit code");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.pairCode, "12345a");
    checkValidate(t, r, false, "code with a letter");
  }
  {
    ProvisionRequest r = good();
    strcpy(r.pairCode, " 12345");
    checkValidate(t, r, false, "code with a space");
  }

  return SelfTestResult{t.total, t.failed};
}

}  // namespace prov

}  // namespace net
