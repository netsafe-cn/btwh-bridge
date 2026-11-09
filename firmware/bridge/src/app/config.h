// Persistent configuration: which heater, which host MAC, which panel code.
//
// Layer rule (ADR-008): app/ may use NVS. Nothing here touches the radio or the frame bytes.
// The panel code is stored in NVS in the clear. That is a deliberate trade-off: the board sits
// next to the heater inside the home, and a code that is lost on reboot means the unit cannot
// come back after a power cut. It is not a bank credential and it is not committed anywhere.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_APP_CONFIG_H
#define BTWH_APP_CONFIG_H

#include <stdint.h>

namespace app {

struct Config {
  char heater[18];   // heater BLE address, e.g. 4C:EB:D6:xx:xx:xx
  char login[18];    // host MAC on the heater whitelist (any value the panel accepts)
  char pin[8];       // six digits, or empty

  // Provisioning fields. Written by the portal (or the wifi console command), read at boot:
  // no ssid means "never provisioned", which is what turns the hotspot on.
  char ssid[33];     // 32 chars is the 802.11 maximum
  char wifiPass[65]; // 64 chars, the WPA2 maximum
  char broker[129];  // host[:port]; empty = discovery only, no external broker
  char mqttUser[33]; // broker account, empty = anonymous
  char mqttPass[65]; // broker password, empty = no password (user may still be sent)

  // Reads NVS; fills in placeholders when a key is absent.
  void load();

  // Writes the pairing keys (heater / login / pin).
  void save() const;

  // Writes the network keys (ssid / wifiPass / broker). Separate because the portal saves
  // them together but the console edits them one at a time.
  void saveNetwork() const;

  // Erases both halves: BOOT held for 5 s does this. The heater keeps its own pairing record,
  // so the user must still clear pairing on the panel - documented in the portal.
  void wipe();

  bool pinValid() const;
  uint32_t pinValue() const;
};

}  // namespace app

#endif  // BTWH_APP_CONFIG_H
