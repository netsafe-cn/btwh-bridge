#include "net/portal.h"

#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>

#include <string.h>

#include "ble/link.h"
#include "net/portal_html.h"

namespace net {
namespace {

WebServer* gServer = nullptr;
DNSServer* gDns    = nullptr;
ProvisionedFn gOnProvisioned = nullptr;
void* gCtx = nullptr;

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
  // The scan costs 5 s of radio time and cannot run while a BLE session is up; the portal is
  // only up when there is no session, so this is safe by construction. If it ever returns
  // nothing the form still renders - with one placeholder row and the "move closer" hint.
  ble::ScanEntry found[8];
  int n = ble::scanHeaters(found, (int)(sizeof(found) / sizeof(found[0])), 5);

  String out;
  if (n == 0) {
    out += "<option value=\"\">(no heater found - reload)</option>";
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

  // AP name ties the unit to the heater it will serve: BTWH-Bridge-XXXX with the last two
  // MAC bytes, the same convention the heater itself uses.
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char apName[24];
  snprintf(apName, sizeof(apName), "BTWH-Bridge-%02X%02X", mac[4], mac[5]);

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName);
  Serial.printf("[PORTAL] AP up: %s  ->  http://192.168.4.1/\n", apName);

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
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
}

bool portalActive() { return gServer != nullptr; }

void portalPoll() {
  if (gDns)    gDns->processNextRequest();
  if (gServer) gServer->handleClient();
}

}  // namespace net
