// Why the Wi-Fi join failed, in words that point at a fix.
//
// Background (2026-11-12). The stored SSID turned out to be a laptop hotspot that was simply
// switched off, and half a day went into finding that out. The console knew only
// "x not associated after 15000 ms (wrong password / 5 GHz only / out of range)" - three guesses
// in one line, printed in every case including the ones that were none of the three. The radio
// knew better the whole time: WiFi.status() says "no SSID in range" versus "association refused",
// and WiFi.disconnectReason() carries the driver's own code (201 NO_AP_FOUND, 15 4-way handshake
// timeout, 210 no AP with a security mode this board supports). That information was being
// thrown away at the exact moment somebody needed it.
//
// Pure on purpose: no Arduino, no <WiFi.h>. The caller passes the two integers the radio
// returned; this module turns them into a sentence. That is what lets the host self test pin
// every code without an access point, and what keeps the wording reviewable in one file.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_WIFISTAT_H
#define BTWH_NET_WIFISTAT_H

#include "net/selftest.h"

namespace net {
namespace wifistat {

// Arduino wl_status_t, as returned by WiFi.status(). Plain int so the host build needs no
// Arduino headers: the numbers are the ABI, the names are the documentation.
//   0 idle, 1 no SSID available, 2 scan completed, 3 connected, 4 connect failed,
//   5 connection lost, 6 wrong password, 7 disconnected, 255 no shield
const char* statusText(int wl);

// The upstream spelling of an esp_wifi reason code ("NO_AP_FOUND"), or "" when this file has
// no name for it. The console prints it next to the number so a log can be searched for the
// constant rather than for a decimal.
const char* reasonName(int reason);

// The same code as a sentence. Unknown codes still render, with the number kept, because a
// code this file has never seen is exactly the one worth reading in a bug report.
const char* reasonText(int reason);

// The sentence to show a person: the reason code when the driver gave one we understand,
// otherwise the status. "The password was rejected" (15) is strictly more useful than
// "disconnected" (7), and both arrive together.
const char* failureText(int wl, int reason);

// Suites: every mapped code renders, no two codes collide, and the unknown fallbacks keep the
// number instead of swallowing it. Wording is not asserted - only that it exists and is
// distinct, so the sentences stay free to improve.
SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace wifistat
}  // namespace net

#endif  // BTWH_NET_WIFISTAT_H
