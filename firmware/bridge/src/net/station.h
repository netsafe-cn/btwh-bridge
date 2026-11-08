// Station Wi-Fi plus the clock it brings.
//
// Layer rule (ADR-008): net/'s Arduino-facing edge, like portal.cpp. It knows Wi-Fi and SNTP
// and nothing else: no MQTT (that client comes later and sits beside this file), no frame
// bytes, no pairing. The app layer asks "are we up?" and "what time is it?" and that is the
// whole interface.
//
// Behaviour worth stating because it was measured, not assumed:
//   * connect() is synchronous with a 15 s budget. The portal calls it after the AP is down;
//     a quick clear failure beats a background retry the user cannot see.
//   * SNTP is fire-and-forget (configTime). The first answer takes seconds; until it arrives
//     timeValid() is false and the MQTT payload omits `ts` (contract consumer_rules[0]).
//   * The heater link is NOT touched here. Wi-Fi and BLE share one radio; NimBLE keeps its
//     timeslices, and a pairing attempt runs only when the app layer says so.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_STATION_H
#define BTWH_NET_STATION_H

#include <stdint.h>

namespace net {

// Joins `ssid` with `pass` and starts SNTP. Returns false only when the association itself
// failed within the budget (wrong password, no such network, timeout). An open network takes
// an empty pass. Blocks up to ~15 s.
bool stationConnect(const char* ssid, const char* pass);

// Whether the station interface has an IP right now.
bool stationUp();

// Epoch seconds, or 0 when the clock has not synced yet. 0 is exactly the value the state
// payload treats as "omit the ts field".
int64_t nowEpoch();

}  // namespace net

#endif  // BTWH_NET_STATION_H
