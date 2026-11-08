#include "net/provision.h"

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

}  // namespace net
