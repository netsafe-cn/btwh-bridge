#include "net/discovery.h"

#include <stdio.h>
#include <string.h>

#include "net/ha_discovery_gen.h"
#include "net/mqtt_wire.h"

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
  // Sized to the largest generated payload plus the device-id substitution. Check 7 ties this
  // number to the publish path: an entity that outgrows the buffers fails here, on the board,
  // instead of publishing a truncated packet a consumer would silently choke on.
  char buf[1280];

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
  //    elsewhere creates an entity that never updates. Measurements read the state topic;
  //    diagnostics read the attributes topic; the water_heater entity binds
  //    current/target/mode to the state topic under its own key names.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    const bool ok = payload && (strstr(payload, "\"state_topic\":\"btwh/@DEV@/state\"")
                                || strstr(payload, "\"state_topic\":\"btwh/@DEV@/attributes\"")
                                || strstr(payload, "\"current_temperature_topic\":\"btwh/@DEV@/state\"")
                                || strstr(payload, "\"state_topic\":\"btwh/@DEV@/update/state\""));
    check(c, ok, "entity names a state or attributes topic", entityTopic(i));
  }

  // 5b. The water_heater entity must bind the setpoint to the existing command topic - a
  //     typo here sends temperature commands into the void while the card looks perfect.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    if (!payload || !strstr(payload, "\"water_heater\"")) continue;
    check(c, strstr(payload, "\"temperature_command_topic\":\"btwh/@DEV@/set/temperature\"") != nullptr,
          "water_heater binds the contract command topic", entityTopic(i));
    check(c, strstr(payload, "\"optimistic\":false") != nullptr,
          "water_heater is not optimistic", entityTopic(i));
  }

  // 5c. The update entity must install through the existing ota topic and say the word the
  //     firmware dispatches ("install") - the UI button and the bench tool must drive the
  //     same flasher, and payload_install is the one string that couples them.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    if (!payload || !strstr(payload, "\"payload_install\"")) continue;
    check(c, strstr(payload, "\"command_topic\":\"btwh/@DEV@/ota\"") != nullptr,
          "update installs through the ota topic", entityTopic(i));
    check(c, strstr(payload, "\"payload_install\":\"install\"") != nullptr,
          "update says the install word the firmware dispatches", entityTopic(i));
    check(c, strstr(payload, "\"state_topic\":\"btwh/@DEV@/update/state\"") != nullptr,
          "update reads the bridge-mediated state", entityTopic(i));
  }

  // 5d. Freshness and liveness are different signals, and the generated wiring has to say so.
  //     Anything fed by state frames - measurements and the water_heater view - is available
  //     only while frames arrive; anything fed by attributes or by the firmware itself stays
  //     readable precisely when the heater is unreachable, which is when a remote user needs
  //     the pairing state and the Install button. Wiring these the wrong way round is silent
  //     in Home Assistant: the entity simply lies, which is the failure the link topic exists
  //     to prevent.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    if (!payload) continue;
    const bool fromState = strstr(payload, "\"state_topic\":\"btwh/@DEV@/state\"")
                           || strstr(payload, "\"water_heater\"");
    const bool hasLink   = strstr(payload, "\"topic\":\"btwh/@DEV@/link\"") != nullptr;
    if (fromState) {
      check(c, hasLink && strstr(payload, "\"availability_mode\":\"all\"") != nullptr,
            "state fed entity binds link with mode all", entityTopic(i));
    } else {
      check(c, !hasLink, "bridge fed entity ignores the link topic", entityTopic(i));
    }
    check(c, strstr(payload, "\"origin\":{\"name\":\"btwh-bridge\"") != nullptr,
          "payload names its origin", entityTopic(i));
  }

  // 6. Exactly the entities on the attributes topic must be entity_category diagnostic -
  //    and nothing else may be. A measurement reading attributes would look alive while
  //    the state stream is dead, and a diagnostic reading state would refresh every 4 s.
  for (size_t i = 0; i < entityCount(); i++) {
    const char* payload = entityPayload(i);
    const bool attrs = payload && strstr(payload, "\"state_topic\":\"btwh/@DEV@/attributes\"");
    const bool diag  = payload && strstr(payload, "\"entity_category\":\"diagnostic\"");
    check(c, attrs == diag, "diagnostics are exactly the attributes readers", entityTopic(i));
  }

  // 7. Every payload must survive the real publish path, not just the payload buffer: expand
  //    it, then build the complete PUBLISH - topic, fixed header and payload - into a buffer of
  //    the size the client actually transmits from. Checking the expansion alone is what let
  //    the table grow from 1032 to 1082 bytes and leave the water_heater packet nine bytes
  //    short of the wire buffer; the only symptom was "20 of 21 entities" in the connect log.
  char topic[96];
  uint8_t packet[1280];
  for (size_t i = 0; i < entityCount(); i++) {
    n = expand(entityTopic(i), "btwh_eb6f5f", topic, sizeof(topic));
    check(c, n > 0, "discovery topic fits", entityTopic(i));
    n = expand(entityPayload(i), "btwh_eb6f5f", buf, sizeof(buf));
    check(c, n > 0, "payload fits the publish buffer", entityTopic(i));
    if (n > 0) {
      check(c, mqtt::buildPublish(packet, sizeof(packet), topic, buf, n, true) > 0,
            "publish fits the transaction buffer", entityTopic(i));
    }
  }

  return SelfTestResult{c.total, c.failed};
}

}  // namespace discovery
}  // namespace net
