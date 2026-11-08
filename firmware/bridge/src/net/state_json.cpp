#include "net/state_json.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace net {
namespace {

// Appends text. Returns false when it would not fit, so the caller can abort instead of
// publishing half a payload.
bool appendRaw(char* out, size_t cap, size_t& len, const char* text) {
  size_t n = strlen(text);
  if (len + n + 1 > cap) return false;
  memcpy(out + len, text, n);
  len += n;
  out[len] = 0;
  return true;
}

bool appendf(char* out, size_t cap, size_t& len, const char* fmt, ...) {
  char scratch[32];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(scratch, sizeof(scratch), fmt, ap);
  va_end(ap);
  if (n < 0 || (size_t)n >= sizeof(scratch)) return false;
  return appendRaw(out, cap, len, scratch);
}

// Writes the separator (except before the first field) and `"key":`.
bool openField(char* out, size_t cap, size_t& len, bool& first, const char* key) {
  if (!first && !appendRaw(out, cap, len, ",")) return false;
  first = false;
  return appendRaw(out, cap, len, "\"") && appendRaw(out, cap, len, key)
      && appendRaw(out, cap, len, "\":");
}

int64_t roundHalfAway(double x) {
  double  a = x < 0 ? -x : x;
  int64_t r = (int64_t)(a + 0.5);
  return x < 0 ? -r : r;
}

}  // namespace

size_t formatFixed(char* out, size_t cap, float value, uint8_t precision) {
  if (!out || cap == 0) return 0;
  out[0] = 0;

  int64_t scale = 1;
  for (uint8_t i = 0; i < precision; i++) scale *= 10;

  int64_t n = roundHalfAway((double)value * (double)scale);
  const char* sign = (n < 0) ? "-" : "";
  int64_t mag = n < 0 ? -n : n;

  int written;
  if (precision == 0) {
    written = snprintf(out, cap, "%s%ld", sign, (long)mag);
  } else {
    written = snprintf(out, cap, "%s%ld.%0*ld", sign, (long)(mag / scale),
                       (int)precision, (long)(mag % scale));
  }
  if (written < 0 || (size_t)written >= cap) { out[0] = 0; return 0; }
  return (size_t)written;
}

const char* modeText(int mode) {
  switch (mode) {
    case 0:  return "idle";
    case 1:  return "burning";
    case 2:  return "comfort";
    default: return "unknown";
  }
}

size_t buildStateJson(char* out, size_t cap, const Snapshot& s) {
  if (!out || cap == 0) return 0;
  out[0] = 0;

  size_t len   = 0;
  bool   first = true;
  bool   ok    = true;
  char   num[24];

  ok = appendRaw(out, cap, len, "{");

  for (size_t i = 0; ok && i < contract::kFieldCount; i++) {
    const contract::FieldDesc& f = contract::kFields[i];
    const char* key = f.key;

    // A field with no value yet is left out entirely, per consumer_rules[0]: a missing field
    // means "not reported yet", and a 0 would read as a real measurement.
    if (strcmp(key, "ts") == 0 && s.ts == 0) continue;

    ok = openField(out, cap, len, first, key);
    if (!ok) break;

    if (strcmp(key, "temp_out") == 0) {
      ok = formatFixed(num, sizeof(num), s.temp_out, f.precision) > 0
        && appendRaw(out, cap, len, num);
    } else if (strcmp(key, "temp_in") == 0) {
      ok = formatFixed(num, sizeof(num), s.temp_in, f.precision) > 0
        && appendRaw(out, cap, len, num);
    } else if (strcmp(key, "chamber") == 0) {
      ok = formatFixed(num, sizeof(num), s.chamber, f.precision) > 0
        && appendRaw(out, cap, len, num);
    } else if (strcmp(key, "temp_box") == 0) {
      ok = formatFixed(num, sizeof(num), s.temp_box, f.precision) > 0
        && appendRaw(out, cap, len, num);
    } else if (strcmp(key, "water_flow") == 0) {
      ok = formatFixed(num, sizeof(num), s.water_flow, f.precision) > 0
        && appendRaw(out, cap, len, num);
    } else if (strcmp(key, "setpoint") == 0) {
      ok = appendf(out, cap, len, "%ld", (long)s.setpoint);
    } else if (strcmp(key, "burner_power") == 0) {
      ok = appendf(out, cap, len, "%ld", (long)s.burner_power);
    } else if (strcmp(key, "fan_speed") == 0) {
      ok = appendf(out, cap, len, "%ld", (long)s.fan_speed);
    } else if (strcmp(key, "flame") == 0) {
      ok = appendRaw(out, cap, len, s.flame ? "true" : "false");
    } else if (strcmp(key, "mode") == 0) {
      ok = appendRaw(out, cap, len, "\"") && appendRaw(out, cap, len, modeText(s.mode))
        && appendRaw(out, cap, len, "\"");
    } else if (strcmp(key, "fault") == 0) {
      // Contract: two upper-case hex digits, or "none". Never an empty string.
      ok = (s.fault == 0)
             ? appendRaw(out, cap, len, "\"none\"")
             : appendf(out, cap, len, "\"%02lX\"", (long)(s.fault & 0xFF));
    } else if (strcmp(key, "ts") == 0) {
      ok = appendf(out, cap, len, "%lld", (long long)s.ts);
    } else {
      ok = appendRaw(out, cap, len, "null");   // a field the contract adds before we handle it
    }
  }

  ok = ok && appendRaw(out, cap, len, "}");
  if (!ok) { out[0] = 0; return 0; }
  return len;
}

// ---------------------------------------------------------------- self test
namespace {

struct Ctx {
  LogFn log;
  void* user;
  int   total;
  int   failed;
  char  line[220];
};

void check(Ctx& c, bool ok, const char* what, const char* got, const char* want) {
  c.total++;
  if (ok) return;
  c.failed++;
  if (!c.log) return;
  snprintf(c.line, sizeof(c.line), "FAIL %s: got [%s] want [%s]", what,
           got ? got : "", want ? want : "");
  c.log(c.user, c.line);
}

void checkStr(Ctx& c, const char* what, const char* got, const char* want) {
  check(c, got && want && strcmp(got, want) == 0, what, got, want);
}

}  // namespace

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Ctx  c{log, ctx, 0, 0, {0}};
  char buf[384];

  // 1. The whole payload, byte for byte, against the string the generator rendered from
  //    spec/mqtt.json. If this passes, the published bytes follow the contract.
  Snapshot g;
  g.temp_out     = contract::kGolden.temp_out;
  g.temp_in      = contract::kGolden.temp_in;
  g.chamber      = contract::kGolden.chamber;
  g.temp_box     = contract::kGolden.temp_box;
  g.setpoint     = contract::kGolden.setpoint;
  g.water_flow   = contract::kGolden.water_flow;
  g.burner_power = contract::kGolden.burner_power;
  g.fan_speed    = contract::kGolden.fan_speed;
  g.flame        = contract::kGolden.flame;
  g.mode         = contract::kGolden.mode;
  g.fault        = contract::kGolden.fault;
  g.ts           = contract::kGolden.ts;

  size_t n = buildStateJson(buf, sizeof(buf), g);
  check(c, n > 0, "golden payload is not empty", "", "");
  checkStr(c, "golden payload", buf, contract::kGoldenState);
  check(c, n == strlen(contract::kGoldenState), "golden payload length", "", "");

  // 2. No clock: the key must be absent, not zero.
  Snapshot noClock = g;
  noClock.ts = 0;
  buildStateJson(buf, sizeof(buf), noClock);
  check(c, strstr(buf, "\"ts\"") == NULL, "ts omitted when unknown", buf, "(no ts)");
  check(c, strstr(buf, "\"fault\":\"none\"") != NULL, "fault renders none", buf, "none");

  // 3. A fault code is hex, two digits, upper case.
  Snapshot faulted = noClock;
  faulted.fault = 0xE1;
  buildStateJson(buf, sizeof(buf), faulted);
  check(c, strstr(buf, "\"fault\":\"E1\"") != NULL, "fault renders E1", buf, "E1");

  // 4. Every mode value has a word, and an unknown one does not invent one.
  checkStr(c, "mode 0", modeText(0), "idle");
  checkStr(c, "mode 1", modeText(1), "burning");
  checkStr(c, "mode 2", modeText(2), "comfort");
  checkStr(c, "mode 9", modeText(9), "unknown");

  // 5. Rounding: half away from zero, the rule the contract states. Python's "%.1f" would
  //    answer 42.2 for the first case - exactly the drift this pins down.
  formatFixed(buf, sizeof(buf), 42.25f, 1);
  checkStr(c, "round 42.25 -> 42.3", buf, "42.3");
  formatFixed(buf, sizeof(buf), -1.25f, 1);
  checkStr(c, "round -1.25 -> -1.3", buf, "-1.3");
  formatFixed(buf, sizeof(buf), 0.0f, 1);
  checkStr(c, "round 0.0 -> 0.0", buf, "0.0");
  formatFixed(buf, sizeof(buf), 15.0f, 1);
  checkStr(c, "round 15.0 -> 15.0", buf, "15.0");

  // 6. A buffer that is too small must refuse, not truncate.
  char tiny[8];
  check(c, buildStateJson(tiny, sizeof(tiny), g) == 0, "small buffer refuses", tiny, "(0)");
  check(c, tiny[0] == 0, "small buffer left empty", tiny, "");

  return SelfTestResult{c.total, c.failed};
}

}  // namespace net
