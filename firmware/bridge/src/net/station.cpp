#include "net/station.h"

#include <WiFi.h>
#include <esp_sntp.h>
#include <esp_wifi.h>
#include <stdio.h>
#include <time.h>

#include "net/wifistat.h"

namespace net {
namespace {

constexpr uint32_t kConnectBudgetMs = 15000;

// Both are fixed-size copies, not pointers: the console prints them later and the setup page
// renders them after the attempt has already returned. A pointer into a caller's stack buffer
// would be the use-after-free this project already paid for once (portal /save, ADR-018).
char gLastFailure[192] = {0};
char gLastSsid[33] = {0};

// The driver's reason for the last disconnection, captured from the event rather than asked for.
//
// Arduino-esp32 2.0.17 (IDF 4.4) has no WiFi.disconnectReason(): the core maps the reason onto a
// coarse wl_status_t (NO_AP_FOUND -> WL_NO_SSID_AVAIL, AUTH_FAIL -> WL_CONNECT_FAILED) and throws
// the number away. NO_AP_FOUND and WRONG_PASSWORD then look like two different statuses, which
// helps, but "5 GHz only" and "the access point is switched off" both collapse into the first -
// and that collapse is exactly what cost the 2026-11-12 afternoon. The event carries the raw
// code, so one handler keeps it.
int  gLastReason   = 0;
bool gReasonHookOn = false;

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    gLastReason = (int)info.wifi_sta_disconnected.reason;
  }
}

}  // namespace

const char* stationLastFailure() { return gLastFailure; }
const char* stationLastAttemptSsid() { return gLastSsid; }

bool stationConnect(const char* ssid, const char* pass) {
  if (!ssid || !ssid[0]) return false;

  snprintf(gLastSsid, sizeof(gLastSsid), "%s", ssid);
  gLastFailure[0] = 0;
  gLastReason       = 0;   // a code from the previous attempt would be a false diagnosis

  if (!gReasonHookOn) {
    WiFi.onEvent(onWifiEvent, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
    gReasonHookOn = true;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass ? pass : "");

  Serial.printf("[WIFI] joining %s ...\n", ssid);
  const uint32_t t0 = millis();

  // Keep the order of the states, print none of them yet.
  //
  // Measured 2026-11-12, the first build of this log: the Windows hotspot produced status 6
  // ("wrong password") for one sample and associated successfully 100 ms later, so the console
  // read "wrong password" immediately above "+ associated". A driver can report a failed first
  // handshake and win on its own retry; the status is a sample, not a verdict. Printing samples
  // as they arrive turns a working join into an alarming log, which is worse than the vague
  // line this replaced. So the chain is recorded and only shown when the attempt actually fails.
  int  seen[8] = {0};
  int  seenN   = 0;
  while (WiFi.status() != WL_CONNECTED) {
    const int st = (int)WiFi.status();
    if (seenN == 0 || seen[seenN - 1] != st) {
      if (seenN < (int)(sizeof(seen) / sizeof(seen[0]))) {
        seen[seenN++] = st;
      } else {
        seen[seenN - 1] = st;   // full: keep the latest, that is the one that stuck
      }
    }

    if (millis() - t0 > kConnectBudgetMs) {
      const int   reason = gLastReason;
      const char* rname  = wifistat::reasonName(reason);
      snprintf(gLastFailure, sizeof(gLastFailure), "%s: %s", ssid, wifistat::failureText(st, reason));

      Serial.printf("[WIFI] x not associated after %lu ms\n", (unsigned long)kConnectBudgetMs);
      {
        // statusText keeps a shared buffer for numbers it has no name for, so each element is
        // rendered into `chain` before the next call can overwrite it.
        char chain[192];
        int  pos = 0;
        for (int i = 0; i < seenN; i++) {
          const int n = snprintf(chain + pos, sizeof(chain) - (size_t)pos, "%s%d %s",
                                 i ? " -> " : "", seen[i], wifistat::statusText(seen[i]));
          if (n < 0) break;
          pos += n;
          if ((size_t)pos >= sizeof(chain)) { pos = (int)sizeof(chain) - 1; break; }
        }
        Serial.printf("[WIFI]   chain: %s\n", pos > 0 ? chain : "(no sample)");
      }
      Serial.printf("[WIFI]   reason %d%s%s: %s\n", reason, rname[0] ? " " : "", rname,
                    wifistat::reasonText(reason));
      WiFi.disconnect();
      return false;
    }
    delay(100);
  }

  Serial.printf("[WIFI] + associated in %lu ms\n", (unsigned long)(millis() - t0));
  {
    char diag[128];
    stationDiag(diag, sizeof(diag));
    Serial.printf("[WIFI]   %s\n", diag);
  }

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

void stationDiag(char* out, size_t cap) {
  if (!out || cap == 0) return;
  if (WiFi.status() != WL_CONNECTED) {
    snprintf(out, cap, "not joined (ip=%s)", WiFi.localIP().toString().c_str());
    return;
  }
  // Gateway and DNS earn their place: a board with an address and no route is the failure the
  // Windows mobile hotspot produces after a few hours, and "ip=192.168.137.2 gw=0.0.0.0" says
  // that in one glance.
  // The channel is asked of the driver: this core has no WiFi.channel().
  uint8_t            primary = 0;
  wifi_second_chan_t second  = WIFI_SECOND_CHAN_NONE;
  if (esp_wifi_get_channel(&primary, &second) != ESP_OK) primary = 0;

  snprintf(out, cap, "ip=%s gw=%s mask=%s dns=%s rssi=%d dBm ch=%u bssid=%s",
           WiFi.localIP().toString().c_str(),
           WiFi.gatewayIP().toString().c_str(),
           WiFi.subnetMask().toString().c_str(),
           WiFi.dnsIP().toString().c_str(),
           (int)WiFi.RSSI(), (unsigned)primary,
           WiFi.BSSIDstr().c_str());
}

}  // namespace net
