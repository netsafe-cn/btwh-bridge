#include "app/config.h"

#include <Preferences.h>
#include <stdlib.h>
#include <string.h>

namespace app {
namespace {

const char* const kHeaterDefault = "AA:BB:CC:DD:EE:FF";   // placeholder, set with `addr`
const char* const kLoginDefault  = "11:22:33:44:55:66";   // placeholder, set with `mac`
const char* const kNamespace     = "bosch";

Preferences& prefs() {
  static Preferences p;
  static bool opened = false;
  if (!opened) { p.begin(kNamespace, false); opened = true; }
  return p;
}

void copyTo(char* dst, size_t cap, const char* src) {
  if (!src) src = "";
  strncpy(dst, src, cap - 1);
  dst[cap - 1] = 0;
}

}  // namespace

void Config::load() {
  String h = prefs().getString("devmac", kHeaterDefault);
  String l = prefs().getString("loginmac", kLoginDefault);
  String p = prefs().getString("pin", "");
  copyTo(heater, sizeof(heater), h.c_str());
  copyTo(login, sizeof(login), l.c_str());
  copyTo(pin, sizeof(pin), p.c_str());
  copyTo(profile, sizeof(profile), prefs().getString("profile", "").c_str());

  String s = prefs().getString("ssid", "");
  String w = prefs().getString("wifipass", "");
  String b = prefs().getString("broker", "");
  String u = prefs().getString("mqttuser", "");
  String pw = prefs().getString("mqttpass", "");
  copyTo(ssid, sizeof(ssid), s.c_str());
  copyTo(wifiPass, sizeof(wifiPass), w.c_str());
  copyTo(broker, sizeof(broker), b.c_str());
  copyTo(mqttUser, sizeof(mqttUser), u.c_str());
  copyTo(mqttPass, sizeof(mqttPass), pw.c_str());
  telnet = prefs().getBool("telnet", false);
  copyTo(otaHost, sizeof(otaHost), prefs().getString("otahost", "").c_str());
}

void Config::save() const {
  prefs().putString("devmac", heater);
  prefs().putString("loginmac", login);
  prefs().putString("pin", pin);
  prefs().putString("profile", profile);
  // The two diagnostic switches are written here because this is what their own console
  // commands call. They used to be written only by saveNetwork(), so `telnet on` reported
  // success and then came back off after the next reboot - a setting that lies about itself is
  // worse than one that cannot be set.
  prefs().putBool("telnet", telnet);
  prefs().putString("otahost", otaHost);
}

void Config::saveNetwork() const {
  prefs().putString("ssid", ssid);
  prefs().putString("wifipass", wifiPass);
  prefs().putString("broker", broker);
  prefs().putString("mqttuser", mqttUser);
  prefs().putString("mqttpass", mqttPass);
}

void Config::wipe() {
  prefs().clear();
  // Restore the placeholders load() would have used, so a wipe while running behaves like a
  // fresh board instead of leaving empty strings in the live struct.
  copyTo(heater, sizeof(heater), kHeaterDefault);
  copyTo(login, sizeof(login), kLoginDefault);
  pin[0] = 0;
  ssid[0] = 0;
  wifiPass[0] = 0;
  broker[0] = 0;
  mqttUser[0] = 0;
  mqttPass[0] = 0;
  otaHost[0] = 0;
  telnet = false;
}

bool Config::pinValid() const {
  if (strlen(pin) != 6) return false;
  for (int i = 0; i < 6; i++) if (pin[i] < '0' || pin[i] > '9') return false;
  return true;
}

uint32_t Config::pinValue() const { return (uint32_t)strtoul(pin, nullptr, 10); }

}  // namespace app
