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
}

void Config::save() const {
  prefs().putString("devmac", heater);
  prefs().putString("loginmac", login);
  prefs().putString("pin", pin);
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
}

bool Config::pinValid() const {
  if (strlen(pin) != 6) return false;
  for (int i = 0; i < 6; i++) if (pin[i] < '0' || pin[i] > '9') return false;
  return true;
}

uint32_t Config::pinValue() const { return (uint32_t)strtoul(pin, nullptr, 10); }

}  // namespace app
