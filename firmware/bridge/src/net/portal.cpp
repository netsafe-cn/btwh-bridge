#include "net/portal.h"

#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>

#include <string.h>

#include "app/session.h"
#include "ble/link.h"
#include "net/portal_html.h"

namespace net {
namespace {

WebServer* gServer = nullptr;
DNSServer* gDns    = nullptr;
ProvisionedFn gOnProvisioned = nullptr;
void* gCtx = nullptr;

// True when the page is served on the LAN instead of over its own hotspot. Matters at stop
// time: tearing down an AP also means turning the radio off, which must never happen to a
// station link that the rest of the bridge depends on.
bool gLanMode = false;

// The last validation error, shown on the form after a rejected submit.
char gError[96] = {0};

// ---------------------------------------------------------------- page assembly

// Escapes what can break the <option> text: nothing in a BLE name or SSID can inject script
// into the attribute, but quotes and angle brackets still break the markup.
void appendEscaped(String& out, const char* text) {
  for (const char* p = text; *p; p++) {
    switch (*p) {
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '&': out += "&amp;"; break;
      default:  out += *p;
    }
  }
}

String heaterOptions() {
  // Two situations, and they need different answers:
  //
  //   session up    A BLE scan cannot see anything while the link is busy (measured
  //                 2026-11-08: LAN-mode page returned zero heaters with the link up and one
  //                 heater with it down). Offer the heater we are already working with -
  //                 that is what a re-pair targets anyway.
  //   session down  Scan. First boot and post-wipe are always in this state.
  String out;
  if (app::status().linkUp) {
    const char* addr = app::configuredHeater();
    out += "<option value=\"";
    appendEscaped(out, addr);
    out += "\">";
    appendEscaped(out, addr);
    out += " (currently paired)</option>";
    return out;
  }

  ble::ScanEntry found[8];
  int n = ble::scanHeaters(found, (int)(sizeof(found) / sizeof(found[0])), 5);

  if (n == 0) {
    out += "<option value=\"\">(no heater found - move closer, then reload)</option>";
    return out;
  }
  for (int i = 0; i < n; i++) {
    out += "<option value=\"";
    appendEscaped(out, found[i].addr);
    out += "\">";
    appendEscaped(out, found[i].name);
    out += " (";
    out += found[i].rssi;
    out += ")</option>";
  }
  return out;
}

String wifiOptions(const char* storedSsid) {
  // The station scan needs the radio in station mode for a moment. It is synchronous (~2 s).
  int n = WiFi.scanNetworks();
  String out;
  bool storedSeen = false;

  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) continue;
    // ESP32 radios are 2.4 GHz only. A 5 GHz-only neighbour in this list would be a choice
    // that cannot work, so it is not offered.
    if (WiFi.channel(i) > 14) continue;
    // The same name shows up once per BSSID (2.4 + 5 GHz, or a mesh node). One row is enough.
    if (out.indexOf("<option value=\"" + ssid + "\"") >= 0)
      continue;
    if (storedSsid && ssid == storedSsid) storedSeen = true;
    out += "<option value=\"";
    appendEscaped(out, ssid.c_str());
    out += "\">";
    appendEscaped(out, ssid.c_str());
    out += "</option>";
  }
  WiFi.scanDelete();

  // A hidden SSID never appears in a scan. Offer it explicitly when it is the stored one, and
  // let any other hidden network be entered through the manual field below the list.
  if (storedSsid && storedSsid[0] && !storedSeen) {
    String row = "<option value=\"";
    appendEscaped(row, storedSsid);
    row += "\">";
    appendEscaped(row, storedSsid);
    row += " (saved)</option>";
    out = row + out;
  }
  out += "<option value=\"__other__\">Other network...</option>";
  return out;
}

String renderPage(const char* heaterOptionsHtml, const char* wifiOptionsHtml) {
  // The page is a PROGMEM template with @TOKENS@; ~4 KB, well below what String can hold.
  String page = FPSTR(provision_html::PAGE);
  page.replace("@HEATER_OPTIONS@", heaterOptionsHtml);
  page.replace("@WIFI_OPTIONS@", wifiOptionsHtml);
  page.replace("@STATE@", provStateText(ProvState::kProvisioning));
  page.replace("@ERROR@", gError);
  page.replace("@ERROR_DISPLAY@", gError[0] ? "block" : "none");
  return page;
}

// ---------------------------------------------------------------- handlers

void handleRoot() {
  // Both scans run here, so the page is always fresh; the portal has no other job.
  String heaters = heaterOptions();
  String wifis = wifiOptions(nullptr);
  gServer->send(200, "text/html", renderPage(heaters.c_str(), wifis.c_str()));
}

void handleSave() {
  ProvisionRequest r;
  memset(&r, 0, sizeof(r));
  strncpy(r.heater,   gServer->arg("heater").c_str(),   sizeof(r.heater) - 1);
  strncpy(r.ssid,     gServer->arg("ssid").c_str(),     sizeof(r.ssid) - 1);
  strncpy(r.wifiPass, gServer->arg("wifipass").c_str(), sizeof(r.wifiPass) - 1);
  strncpy(r.pairCode, gServer->arg("code").c_str(),     sizeof(r.pairCode) - 1);
  strncpy(r.broker,   gServer->arg("broker").c_str(),   sizeof(r.broker) - 1);
  r.broker[sizeof(r.broker) - 1] = 0;
  strncpy(r.mqttUser, gServer->arg("mqttuser").c_str(), sizeof(r.mqttUser) - 1);
  r.mqttUser[sizeof(r.mqttUser) - 1] = 0;
  strncpy(r.mqttPass, gServer->arg("mqttpass").c_str(), sizeof(r.mqttPass) - 1);
  r.mqttPass[sizeof(r.mqttPass) - 1] = 0;

  // "Other network..." swaps the dropdown for whatever was typed alongside it.
  if (strcmp(r.ssid, "__other__") == 0) {
    strncpy(r.ssid, gServer->arg("ssidmanual").c_str(), sizeof(r.ssid) - 1);
    r.ssid[sizeof(r.ssid) - 1] = 0;
  }

  const char* problem = validateRequest(r);
  if (problem) {
    Serial.printf("[PORTAL] rejected: %s\n", problem);
    strncpy(gError, problem, sizeof(gError) - 1);
    gError[sizeof(gError) - 1] = 0;
    gServer->send(400, "text/html",
                  renderPage(heaterOptions().c_str(), wifiOptions(nullptr).c_str()));
    return;
  }
  gError[0] = 0;

  // Acknowledge before tearing the AP down: the phone will lose its link to the board the
  // moment we stop the soft AP, and an unanswered POST shows as an error page.
  gServer->send(200, "text/html",
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<body style='font-family:system-ui;margin:40px auto;max-width:420px'>"
                "<h2>Saved</h2><p>Connecting. This network will close in a few seconds; "
                "the bridge appears on your Wi-Fi when it is done.</p></body></html>");

  Serial.printf("[PORTAL] saved: heater=%s ssid=%s code=%s\n",
                r.heater, r.ssid, r.pairCode[0] ? "****" : "(keep)");

  stopPortal();
  if (gOnProvisioned) {
    const char* outcome = gOnProvisioned(gCtx, r);
    Serial.printf("[PORTAL] provision outcome: %s\n", outcome ? outcome : "?");
  }
}

void handleNotFound() {
  // Captive portal: anything the phone asks for - apple.com/connecttest.txt, msftconnecttest,
  // whatever - is a redirect. That is what makes the login popup appear.
  gServer->sendHeader("Location", "http://192.168.4.1/", true);
  gServer->send(302, "text/plain", "");
}

}  // namespace

bool portalNeeded(const char* storedSsid) {
  return storedSsid == nullptr || storedSsid[0] == 0;
}

void startPortal(ProvisionedFn onProvisioned, void* ctx) {
  if (gServer) return;                       // already up

  gOnProvisioned = onProvisioned;
  gCtx = ctx;

  // Two shapes, chosen by whether the board already has a network:
  //
  //   AP mode   no credentials yet (first boot, or after a wipe). Brings up its own
  //             hotspot; the phone switches to it deliberately.
  //   LAN mode  already joined. Serve the same page on the LAN address, so re-pairing or
  //             changing networks does not require anything to leave its current network -
  //             in particular not a PC with a single Wi-Fi radio, which cannot be on the
  //             bridge's hotspot and the internet at the same time (measured 2026-11-08).
  //
  // LAN mode is only reachable on demand (`portal` on the console) and never left running by
  // itself: that keeps a page that can re-provision the device from being permanently exposed.
  if (WiFi.status() == WL_CONNECTED) {
    gLanMode = true;
    gServer = new WebServer(80);
    gServer->on("/", HTTP_GET, handleRoot);
    gServer->on("/save", HTTP_POST, handleSave);
    gServer->onNotFound(handleNotFound);
    gServer->begin();
    Serial.printf("[PORTAL] LAN mode: http://%s/  (same network, nothing to switch)\n",
                  WiFi.localIP().toString().c_str());
    return;
  }

  // AP name ties the unit to the heater it will serve: BTWH-Bridge-XXXX with the last two
  // MAC bytes, the same convention the heater itself uses.
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char apName[24];
  snprintf(apName, sizeof(apName), "BTWH-Bridge-%02X%02X", mac[4], mac[5]);

  WiFi.mode(WIFI_AP);
  // Modem sleep drops beacons and defers broadcast handling exactly when a client is trying
  // to associate and DHCP itself - with BLE coexistence on top it made every association fall
  // apart (measured 2026-11-08: PC associated, three captive probes answered, zero DHCP
  // offers). The portal lives for minutes on a powered board; power is not the constraint,
  // reliability is.
  WiFi.setSleep(false);
  // PSRAM is off in this build, so the default max_clients=4 keeps the heap happy; a setup
  // session only ever has one phone on it.
  // WPA2 rather than an open AP: the captive-portal handshake on several older phone/PC
  // drivers (measured on a Marvell AVASTAR) associates to an open ESP32 AP but never
  // completes DHCP, while the same hardware joins a WPA2 one cleanly. The passcode is printed
  // below and spoken in the manual; it is a speed bump, not a lock - the provisioning form is
  // what carries the real credentials.
  bool ap = WiFi.softAP(apName, "bridge123");
  Serial.printf("[PORTAL] AP up: %s  ->  http://%s/  (softAP=%d ch=%d)\n",
                apName, WiFi.softAPIP().toString().c_str(), (int)ap,
                (int)WiFi.channel());

  gDns = new DNSServer();
  gDns->start(53, "*", WiFi.softAPIP());

  gServer = new WebServer(80);
  gServer->on("/", HTTP_GET, handleRoot);
  gServer->on("/save", HTTP_POST, handleSave);
  gServer->onNotFound(handleNotFound);
  gServer->begin();
}

void stopPortal() {
  if (gServer) { gServer->stop(); delete gServer; gServer = nullptr; }
  if (gDns)    { gDns->stop();    delete gDns;    gDns = nullptr; }
  if (gLanMode) {
    // Just close the page. The station link is what the bridge runs on - leave it alone.
    gLanMode = false;
    return;
  }
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
}

bool portalActive() { return gServer != nullptr; }

void portalPoll() {
  if (gDns)    gDns->processNextRequest();
  if (gServer) gServer->handleClient();
}

}  // namespace net
