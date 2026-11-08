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
}

void Config::save() const {
  prefs().putString("devmac", heater);
  prefs().putString("loginmac", login);
  prefs().putString("pin", pin);
}

bool Config::pinValid() const {
  if (strlen(pin) != 6) return false;
  for (int i = 0; i < 6; i++) if (pin[i] < '0' || pin[i] > '9') return false;
  return true;
}

uint32_t Config::pinValue() const { return (uint32_t)strtoul(pin, nullptr, 10); }

}  // namespace app
