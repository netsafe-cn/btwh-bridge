#include "net/discovery.h"

#include <stdio.h>
#include <string.h>

#include "net/ha_discovery_gen.h"

namespace net {
namespace discovery {
namespace {

const char* const kToken = "@DEV@";
constexpr size_t kTokenLen = 5;

}  // namespace

size_t expand(const char* tmpl, const char* deviceId, char* out, size_t cap) {
  if (!tmpl || !deviceId || !out || cap == 0) return 0;
  out[0] = 0;

  const size_t devLen = strlen(deviceId);
  size_t at = 0;
  const char* p = tmpl;

  while (*p) {
    const char* hit = strstr(p, kToken);
    const size_t run = hit ? (size_t)(hit - p) : strlen(p);

    if (at + run + 1 > cap) return 0;                  // +1 for the terminator
    memcpy(out + at, p, run);
    at += run;

    if (!hit) break;

    if (at + devLen + 1 > cap) return 0;
    memcpy(out + at, deviceId, devLen);
    at += devLen;
    p = hit + kTokenLen;
  }
  out[at] = 0;
  return at;
}

size_t entityCount() { return kEntityCount; }

const char* entityTopic(size_t index) {
  return index < kEntityCount ? kEntities[index].topic : nullptr;
}

const char* entityPayload(size_t index) {
  return index < kEntityCount ? kEntities[index].payload : nullptr;
}

// ---------------------------------------------------------------- self test
namespace {

struct Ctx {
  LogFn log;
  void* user;
  int   total;
  int   failed;
  char  line[200];
};

void check(Ctx& c, bool ok, const char* what, const char* detail) {
  c.total++;
  if (ok) return;
  c.failed++;
  if (!c.log) return;
  snprintf(c.line, sizeof(c.line), "FAIL %s%s%s", what, detail ? ": " : "",
           detail ? detail : "");
  c.log(c.user, c.line);
}

}  // namespace

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Ctx c{log, ctx, 0, 0, {0}};
  char buf[640];

  // 1. The substitution itself, including a token that appears more than once.
  size_t n = expand("btwh/@DEV@/state", "btwh_eb6f5f", buf, sizeof(buf));
  check(c, n == strlen("btwh/btwh_eb6f5f/state"), "expand replaces the token", nullptr);
  check(c, strcmp(buf, "btwh/btwh_eb6f5f/state") == 0, "expanded text is exact", buf);

  n = expand("@DEV@@DEV@", "ab", buf, sizeof(buf));
  check(c, strcmp(buf, "abab") == 0, "expand handles repeats", buf);

  n = expand("no token here", "ab", buf, sizeof(buf));
  check(c, strcmp(buf, "no token here") == 0, "expand passes plain text through", buf);

  // 2. A buffer one byte short must refuse, not truncate. "btwh_eb6f5f" is 11 characters -
  //    the first on-board selftest run caught this pair asserting 10, which had never executed
  //    anywhere: the C++ suites only run on the target.
  n = expand("@DEV@", "btwh_eb6f5f", buf, 12);       // 11 chars + terminator needs 12
  check(c, n == 11, "expand fits exactly", nullptr);
  n = expand("@DEV@", "btwh_eb6f5f", buf, 11);       // one byte short of the terminator
  check(c, n == 0 && buf[0] == 0, "expand refuses a short buffer", nullptr);

  // 3. The generated table is present and complete.
  check(c, entityCount() > 0, "entity table is not empty", nullptr);
  check(c, entityTopic(0) && entityPayload(0), "first entity exists", nullptr);
  check(c, entityTopic(entityCount()) == nullptr, "index past the end is null", nullptr);

  // 4. Golden pair: the first entity rendered for a known device id must match the generator
  //    byte for byte. That catches a template edited without regenerating, and any drift in the
  //    substitution.
  n = expand(entityTopic(0), "btwh_eb6f5f", buf, sizeof(buf));
  check(c, n > 0 && strcmp(buf, kGoldenTopic) == 0, "golden discovery topic", buf);

  n = expand(entityPayload(0), "btwh_eb6f5f", buf, sizeof(buf));
  check(c, n > 0 && strcmp(buf, kGoldenPayload) == 0, "golden discovery payload", buf);

  // 5. Every entity must name a state topic on this device: a discovery entry pointing
  //    elsewhere creates an entity that never updates.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    const char* stateTopic = payload ? strstr(payload, "\"state_topic\":\"btwh/@DEV@/state\"") : nullptr;
    check(c, stateTopic != nullptr, "entity names the state topic", entityTopic(i));
  }

  return SelfTestResult{c.total, c.failed};
}

}  // namespace discovery
}  // namespace net
