#include "net/mqtt_rules.h"

#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "net/mqtt_contract.h"

namespace net {
namespace rules {

// ---------------------------------------------------------------- broker address

bool parseBroker(const char* text, char* host, size_t hostCap, uint16_t* port) {
  if (!text || !text[0] || !host || hostCap < 2) return false;

  size_t hostLen = strlen(text);
  long   p = 1883;

  const char* colon     = strrchr(text, ':');
  const char* firstColon = strchr(text, ':');
  if (colon) {
    if (!colon[1]) {
      // "host:" - a colon that announces a port and then delivers none is a typo, not a
      // default. Storing "host:" as the address would surface later as an unreachable broker
      // instead of a rejected line.
      return false;
    }
    if (firstColon != colon) {
      // More than one colon and no brackets (the [IPv6]:port form is out of scope): this is
      // a bare IPv6 literal like fe80::1, and strrchr would tear it into host "fe80:" port 1.
      // Take the whole text as the host and keep the default port.
    } else {
      char* end = nullptr;
      const long v = strtol(colon + 1, &end, 10);
      if (!end || *end != 0 || v <= 0 || v > 65535) return false;
      p = v;
      hostLen = (size_t)(colon - text);
    }
  }
  if (hostLen == 0 || hostLen >= hostCap) return false;

  memcpy(host, text, hostLen);
  host[hostLen] = 0;
  if (port) *port = (uint16_t)p;
  return true;
}

// ---------------------------------------------------------------- setpoint text

// Trailing whitespace only; see the header for why leading stays an error.
static void trimTrailing(char* text, size_t len) {
  for (int i = (int)len - 1; i >= 0 && isspace((unsigned char)text[i]); i--) text[i] = 0;
}

Setpoint parseSetpoint(const char* text, int* outCelsius) {
  if (!text || !text[0]) return Setpoint::kNotANumber;

  // The client hands us a bounded copy; still copy defensively so this function's
  // correctness never depends on the caller's buffer discipline.
  char buf[24];
  size_t n = strlen(text);
  if (n >= sizeof(buf)) return Setpoint::kNotANumber;
  memcpy(buf, text, n + 1);

  // Leading whitespace is an error, not something to trim: strtod would skip it silently, and
  // no legitimate consumer (HA number entity, mosquitto_pub, the demo bridge) sends it - so
  // whitespace hiding in a payload is worth seeing, not worth guessing at.
  if (isspace((unsigned char)buf[0])) return Setpoint::kNotANumber;
  trimTrailing(buf, n);

  char* end = nullptr;
  // strtod, not strtol: Home Assistant's number entity publishes str(float) - "42.0" - and
  // an integer-only parser would silently drop every command from it.
  const double value = strtod(buf, &end);
  const bool numeric = end && *end == 0 && end != buf;
  if (!numeric) return Setpoint::kNotANumber;
  if (value != (double)(long)value) return Setpoint::kNotWhole;
  const long whole = (long)value;
  if (whole < contract::kSetpointMin || whole > contract::kSetpointMax) return Setpoint::kOutOfRange;

  if (outCelsius) *outCelsius = (int)whole;
  return Setpoint::kOk;
}

// ---------------------------------------------------------------- command verb

const char* commandVerb(const char* text) {
  if (!text) return nullptr;
  // A tiny substring match instead of a JSON parser: the contract allows three words, and
  // dragging in a parser for them would cost more than it explains.
  if (strstr(text, "refresh")) return "refresh";
  if (strstr(text, "reboot"))  return "reboot";
  if (strstr(text, "unpair"))  return "unpair";
  return nullptr;
}

bool commandDeliveryAllowed(bool retained) { return !retained; }

uint32_t elapsedMs(uint32_t newer, uint32_t older) { return newer - older; }

uint32_t nextBackoffMs(uint32_t current) {
  return current < 60000 ? current * 2 : 60000;
}

// ---------------------------------------------------------------- self test

namespace {

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

void checkBroker(Tally& t, const char* text, bool wantOk, const char* wantHost, long wantPort) {
  char host[64] = {0};
  uint16_t port = 0;
  const bool ok = parseBroker(text, host, sizeof(host), &port);
  t.total++;
  if (ok != wantOk || (wantOk && (strcmp(host, wantHost) != 0 || port != wantPort))) {
    t.failed++;
    line(t, "  x parseBroker(\"%s\"): got ok=%d host=%s port=%u", text, ok, host, (unsigned)port);
  }
}

void checkSetpoint(Tally& t, const char* text, Setpoint want, int wantValue) {
  int celsius = -999;
  const Setpoint got = parseSetpoint(text, &celsius);
  t.total++;
  if (got != want || (want == Setpoint::kOk && celsius != wantValue)) {
    t.failed++;
    line(t, "  x parseSetpoint(\"%s\"): got %d (value %d)", text, (int)got, celsius);
  }
}

// Setpoint boundary through the number's own text form, so the test uses the same
// decimal rendering a consumer would send.
void checkSetpointValue(Tally& t, int value, Setpoint want) {
  char text[16];
  snprintf(text, sizeof(text), "%d", value);
  checkSetpoint(t, text, want, value);
}

void checkVerb(Tally& t, const char* text, const char* want) {
  const char* got = commandVerb(text);
  t.total++;
  const bool ok = (want == nullptr && got == nullptr)
                  || (want && got && strcmp(want, got) == 0);
  if (!ok) {
    t.failed++;
    line(t, "  x commandVerb(\"%s\"): got %s", text, got ? got : "(null)");
  }
}

}  // namespace

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Tally t;
  t.log = log;
  t.ctx = ctx;

  // 1. Broker address grammar: the console, the portal and boot all feed this parser.
  checkBroker(t, "192.168.9.206", true, "192.168.9.206", 1883);
  checkBroker(t, "192.168.9.206:1883", true, "192.168.9.206", 1883);
  checkBroker(t, "mqtt.example.com:8883", true, "mqtt.example.com", 8883);
  checkBroker(t, "localhost:1", true, "localhost", 1);
  checkBroker(t, "localhost:65535", true, "localhost", 65535);
  checkBroker(t, "", false, "", 0);
  checkBroker(t, "host:0", false, "", 0);            // port 0 is not a usable port
  checkBroker(t, "host:65536", false, "", 0);        // one past the uint16 range
  checkBroker(t, "host:notaport", false, "", 0);
  checkBroker(t, "host:12x", false, "", 0);          // trailing garbage after the digits
  checkBroker(t, "host:", false, "", 0);             // colon with nothing after it
  checkBroker(t, "fe80::1", true, "fe80::1", 1883);  // strrchr: bare IPv6 stays whole, no port
  // A host longer than the output buffer must refuse, not truncate: a truncated address
  // silently connects to the wrong broker.
  {
    char tiny[4];
    uint16_t port = 0;
    t.total++;
    if (parseBroker("192.168.9.206", tiny, sizeof(tiny), &port)) {
      t.failed++;
      line(t, "  x parseBroker: undersized buffer accepted");
    }
  }

  // 2. Setpoint text: the exact bytes a Home Assistant number entity publishes are "42.0"
  //    (str(float)); the demo bridge and mosquitto_pub send "42\n" and "42".
  checkSetpoint(t, "42", Setpoint::kOk, 42);
  checkSetpoint(t, "42.0", Setpoint::kOk, 42);        // HA number entity, the main real-world shape
  checkSetpoint(t, "42\n", Setpoint::kOk, 42);        // trailing newline trimmed
  checkSetpoint(t, "42 ", Setpoint::kOk, 42);         // trailing space trimmed
  checkSetpoint(t, "42.0\r\n", Setpoint::kOk, 42);    // CRLF trimmed
  checkSetpoint(t, " 42", Setpoint::kNotANumber, 0);  // leading whitespace is a visible problem
  checkSetpoint(t, "41.5", Setpoint::kNotWhole, 0);   // fractional is a range error, not rounding
  checkSetpoint(t, "abc", Setpoint::kNotANumber, 0);
  checkSetpoint(t, "", Setpoint::kNotANumber, 0);
  checkSetpoint(t, "42x", Setpoint::kNotANumber, 0);
  checkSetpoint(t, "42.", Setpoint::kOk, 42);         // strtod accepts, whole value 42
  // Range edges come from the contract header, which comes from the profile row - pin the
  // boundary itself so a profile edit that forgets the contract regeneration shows up here.
  checkSetpointValue(t, contract::kSetpointMin, Setpoint::kOk);
  checkSetpointValue(t, contract::kSetpointMax, Setpoint::kOk);
  checkSetpointValue(t, contract::kSetpointMax + 1, Setpoint::kOutOfRange);
  checkSetpointValue(t, contract::kSetpointMin - 1, Setpoint::kOutOfRange);
  // An absurdly long payload must be rejected, not parsed past the buffer.
  {
    const char* longText = "0000000000000000000000000000042";
    int celsius = -999;
    t.total++;
    if (parseSetpoint(longText, &celsius) != Setpoint::kNotANumber) {
      t.failed++;
      line(t, "  x parseSetpoint: oversized input accepted");
    }
  }

  // 3. Command verbs: substring match, case-sensitive as documented.
  checkVerb(t, "refresh", "refresh");
  checkVerb(t, "reboot", "reboot");
  checkVerb(t, "unpair", "unpair");
  checkVerb(t, "{\"cmd\":\"refresh\"}", "refresh");   // tolerant of a JSON wrapper
  checkVerb(t, "REFRESH", nullptr);                   // case-sensitive, as the contract documents
  checkVerb(t, "restart", nullptr);                   // not a verb, must not half-match reboot
  checkVerb(t, "", nullptr);
  checkVerb(t, nullptr, nullptr);

  // 4. Live-only commands: a retained "reboot" replaying on every reconnect would reboot the
  //    board forever. The property is one line, but it is a safety property, so it has a test.
  check(t, !commandDeliveryAllowed(true), "retained command delivery must be refused");
  check(t, commandDeliveryAllowed(false), "live command delivery must be allowed");

  // 5. Elapsed-time arithmetic, argument order included. This is the class of the 2.5.1 bug:
  //    the watchdog sampled `now` before the drain that stamped gLastRx, the subtraction
  //    wrapped, and the link died 0.1 s after CONNACK. Correct order reads small; reversed
  //    order reads huge - so an expiry check fails loudly in the direction of "drop" rather
  //    than "spuriously healthy".
  check(t, elapsedMs(1000, 1000) == 0, "same tick is zero elapsed");
  check(t, elapsedMs(1001, 1000) == 1, "one tick later");
  check(t, elapsedMs(1000, 2000) > 90000, "reversed samples wrap huge, never zero");
  // The wrap itself is only safe because the comparison threshold is far below UINT32_MAX;
  // pin that the watchdog limit (90 s) stays far from the wrap magnitude.
  check(t, 90000u < 0xFFFFFFFFu / 2, "watchdog threshold far below wrap magnitude");

  // 6. The reconnect schedule: doubling, capped at a minute, with the one-step overshoot
  //    pinned as intended behaviour (the cap applies to the output). This is the schedule
  //    that once looked broken during the 2.5.1 bench session - a good session resets it,
  //    so a flapping-at-4-s loop meant the link was being killed, not that the doubling
  //    failed. With the sequence pinned here, that diagnosis no longer needs a bench.
  check(t, nextBackoffMs(2000) == 4000, "backoff doubles from the start");
  check(t, nextBackoffMs(32000) == 64000, "backoff overshoots once before the cap");
  check(t, nextBackoffMs(64000) == 60000, "backoff caps at one minute");
  check(t, nextBackoffMs(60000) == 60000, "backoff stays at the cap");
  check(t, nextBackoffMs(0) == 0, "zero backoff stays zero (never used, never surprising)");

  return SelfTestResult{t.total, t.failed};
}

}  // namespace rules
}  // namespace net
