#include "net/discover.h"

#include <ESPmDNS.h>
#include <WiFi.h>

#include <stdio.h>

namespace net {
namespace {

bool gStarted = false;

bool ensureStarted() {
  if (gStarted) return true;
  // The API needs a name before it will answer anything. It also makes the bridge itself
  // reachable as btwh-bridge.local, which is how a user can open the LAN-mode setup page without
  // being told an address either.
  gStarted = MDNS.begin("btwh-bridge");
  return gStarted;
}

void writeIp(IPAddress ip, char* out, size_t cap) {
  snprintf(out, cap, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
}

// Turns result `idx` into a dotted quad. Two queries, not one: MDNS.queryService() runs
// mdns_query_ptr, which returns PTR answers without following SRV/A, so result->addr - what
// MDNS.IP() reads - is empty and reports 0.0.0.0. Measured 2026-11-14 on the board: the service
// was found and the address came back as 0.0.0.0. The ESPmDNS examples resolve the SRV target
// with a second query for exactly this reason.
bool resolveResult(int idx, char* out, size_t cap) {
  IPAddress ip = MDNS.IP(idx);
  if (ip == IPAddress()) {
    String host = MDNS.hostname(idx);
    if (host.length()) ip = MDNS.queryHost(host.c_str(), 2000);
  }
  if (ip == IPAddress()) return false;
  writeIp(ip, out, cap);
  return true;
}

}  // namespace

bool discoverBroker(char* out, size_t cap, uint16_t* port, char* how, size_t howCap) {
  if (!out || cap < 8 || !port) return false;
  if (WiFi.status() != WL_CONNECTED) return false;

  if (!ensureStarted()) {
    if (how && howCap) snprintf(how, howCap, "mDNS would not start");
    return false;
  }

  // 1. A broker that says what it is. Blocks a few seconds - resolution is a second query - which
  //    is why this runs once at boot and from the console, never in loop().
  int count = MDNS.queryService("mqtt", "tcp");
  if (count > 0 && resolveResult(0, out, cap)) {
    *port = MDNS.port(0) ? MDNS.port(0) : 1883;
    if (how && howCap) snprintf(how, howCap, "advertised _mqtt._tcp");
    return true;
  }

  // 2. The add-on case: the broker lives on the Home Assistant host. The advertised port is the
  //    web UI (80), so it is deliberately ignored - 1883 is the broker's conventional port and
  //    the add-on's default.
  count = MDNS.queryService("home-assistant", "tcp");
  if (count > 0) {
    bool resolved = resolveResult(0, out, cap);
    if (!resolved) {
      // Last resort, and it is the name Home Assistant OS always answers to: the SRV target on
      // some installations is a UUID-based name that a second query does not resolve.
      IPAddress ip = MDNS.queryHost("homeassistant.local", 2000);
      if (ip != IPAddress()) {
        writeIp(ip, out, cap);
        resolved = true;
      }
    }
    if (resolved) {
      *port = 1883;
      if (how && howCap) {
        snprintf(how, howCap, "Home Assistant host, broker assumed at 1883 (the add-on default)");
      }
      return true;
    }
  }

  if (how && howCap) snprintf(how, howCap, "no MQTT or Home Assistant service found");
  return false;
}

}  // namespace net
