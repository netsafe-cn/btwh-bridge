#include "net/station.h"

#include <WiFi.h>
#include <esp_sntp.h>
#include <stdio.h>
#include <time.h>

namespace net {
namespace {

constexpr uint32_t kConnectBudgetMs = 15000;

}  // namespace

bool stationConnect(const char* ssid, const char* pass) {
  if (!ssid || !ssid[0]) return false;

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass ? pass : "");

  Serial.printf("[WIFI] joining %s ...\n", ssid);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > kConnectBudgetMs) {
      Serial.printf("[WIFI] x not associated after %lu ms (wrong password / 5 GHz only / "
                    "out of range)\n", (unsigned long)kConnectBudgetMs);
      WiFi.disconnect();
      return false;
    }
    delay(100);
  }

  Serial.printf("[WIFI] + associated, ip=%s\n", WiFi.localIP().toString().c_str());

  // Fire-and-forget SNTP: pool.ntp.org answers anywhere; the regional servers are faster and
  // keep working when the global pool is filtered. CN pool first, then the global one as the
  // fallback target, UTC everywhere - the bridge never renders local time, it publishes epoch.
  configTzTime("", "ntp.aliyun.com", "cn.ntp.org.cn", "pool.ntp.org");
  Serial.println("[WIFI] SNTP started (ts joins the payload once it answers)");
  return true;
}

bool stationUp() { return WiFi.status() == WL_CONNECTED; }

int64_t nowEpoch() {
  // time() reports 1970 until SNTP has answered. 0 means "no clock", which the payload
  // renderer already maps to an omitted ts field - the same contract rule, no special case.
  time_t t = time(nullptr);
  return (t < 1000000000) ? 0 : (int64_t)t;   // reject pre-2001 as "not synced yet"
}

void stationIp(char* out, size_t cap) {
  if (!out || cap == 0) return;
  snprintf(out, cap, "%s", WiFi.localIP().toString().c_str());
}

}  // namespace net
