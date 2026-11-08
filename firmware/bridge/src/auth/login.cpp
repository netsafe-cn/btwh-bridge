#include "auth/login.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace auth {
namespace {

constexpr uint32_t kReplyTimeoutMs = 6000;   // bosch_api.py: 6 s per attempt
constexpr int      kAttempts       = 5;      // bosch_api.py: five resends, fresh msgId
constexpr uint8_t  kSubMac         = 0x07;
constexpr uint8_t  kSubCode        = 0x08;

void logf(const Transport& t, const char* fmt, ...) {
  if (!t.log) return;
  char buf[128];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  t.log(t.ctx, buf);
}

// One command, up to five attempts. Returns false only when nothing ever came back; a reply
// that arrived is written to `reply` whatever its value, because the value carries the reason.
bool exchange(const Transport& t, uint8_t sub, const uint8_t* data, size_t len, uint8_t* reply) {
  *reply = 0xFF;
  for (int i = 0; i < kAttempts; i++) {
    if (!t.send(t.ctx, sub, data, len)) return false;
    int v = t.wait(t.ctx, sub, kReplyTimeoutMs);
    if (v >= 0) {
      *reply = (uint8_t)v;
      logf(t, "A2/%02X reply = %02X", sub, *reply);
      return true;
    }
    logf(t, "A2/%02X no reply, resend %d/%d (fresh msgId)", sub, i + 1, kAttempts);
  }
  return false;
}

}  // namespace

bool pinValid(const char* pin6) {
  if (!pin6 || strlen(pin6) != 6) return false;
  for (int i = 0; i < 6; i++) if (pin6[i] < '0' || pin6[i] > '9') return false;
  return true;
}

bool submitMac(const Transport& t, const uint8_t mac[6], uint8_t* reply) {
  uint8_t data[7] = {kSubMac, 0, 0, 0, 0, 0, 0};
  memcpy(data + 1, mac, 6);
  return exchange(t, kSubMac, data, sizeof(data), reply);
}

bool submitCode(const Transport& t, const char* pin6, uint8_t* reply) {
  if (!pinValid(pin6)) { *reply = 0xFF; return false; }
  uint8_t data[7];
  data[0] = kSubCode;
  for (int i = 0; i < 6; i++) data[i + 1] = (uint8_t)(pin6[i] - '0');   // one byte per digit
  return exchange(t, kSubCode, data, sizeof(data), reply);
}

Result classify(uint8_t reply07, uint8_t reply08) {
  if (reply07 == 0x3C || reply07 == 0x5A) return kCooldown;
  if (reply07 == 0x00) return kWindowClosed;
  if (reply08 == 0x01) return kOk;
  if (reply08 == 0x02) return kCodeMismatch;
  if (reply08 == 0x00) return kCodeRejected;
  if (reply07 == 0xFF && reply08 == 0xFF) return kNoResponse;
  return kNoResponse;
}

const char* windowText(uint8_t r) {
  switch (r) {
    case 0x01: return "MAC registered, code still required";
    case 0x02: return "window open, code required";
    case 0x00: return "window closed or expired";
    case 0x3C: return "cooling down (30 s)";
    case 0x5A: return "asks for 90 s";
    case 0xFF: return "no reply";
    default:   return "unknown";
  }
}

const char* codeText(uint8_t r) {
  switch (r) {
    case 0x01: return "authorised";
    case 0x02: return "wrong code";
    case 0x00: return "refused (expired / no window)";
    case 0xFF: return "not sent";
    default:   return "unknown";
  }
}

const char* reasonText(Result r) {
  switch (r) {
    case kOk:           return "ok";
    case kWindowClosed: return "window_closed";
    case kCooldown:     return "cooldown";
    case kCodeRejected: return "code_rejected";
    case kCodeMismatch: return "code_mismatch";
    case kNoResponse:   return "no_response";
    case kBadInput:     return "bad_input";
    case kLinkLost:     return "link_failed";
  }
  return "unknown";
}

}  // namespace auth
