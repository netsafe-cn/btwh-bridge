// The SoftAP + captive portal: what a fresh board becomes until it is provisioned.
//
// Flow (agreed 2026-11-08):
//   GET  /            the form. Heater list from a BLE scan, Wi-Fi list from a station scan.
//   POST /save        validateRequest() -> save to NVS -> stop the AP -> try the credentials.
//                     On failure the AP comes back with the reason shown on the form.
//   any other host    redirected to / (DNSServer wildcard), so the phone's "log in to network"
//                     popup opens the form directly.
//
// Layer rule (ADR-008): this is net/'s Arduino-facing edge, the mirror of ble/. It may include
// Wi-Fi and WebServer, and it talks to the app layer through the two callbacks below - it never
// touches frame bytes and never starts a pairing attempt itself. The pairing attempt is the
// app's to run, because only the app knows how to leave the heater in a clean state.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_PORTAL_H
#define BTWH_NET_PORTAL_H

#include "net/provision.h"

namespace net {

// Whether the board has anything to connect with. No SSID in NVS -> true.
bool portalNeeded(const char* storedSsid);

// Brings up the AP + DNS + HTTP server and serves until stop() or a successful /save.
// `onProvisioned` runs once, after credentials are validated and stored and the AP is down:
// the app layer starts Wi-Fi and (if a code was given) the pairing attempt. It receives the
// saved request and returns an outcome line for the log; the portal is finished either way.
//
// `automatic` marks a portal nobody asked for: the Wi-Fi fallback (net/recover.h) opened it
// because the stored credentials stopped working. The only thing the flag changes is that
// such a portal may be closed again by that same policy once it has been idle - a page a
// human asked for stays until a human closes it.
typedef const char* (*ProvisionedFn)(void* ctx, const ProvisionRequest& r);

void startPortal(ProvisionedFn onProvisioned, void* ctx, bool automatic = false);
void stopPortal();
bool portalActive();

// Whether this is an automatic portal. False once it stops, and false for the console path.
bool portalAutomatic();

// Stations associated with the setup AP right now. Always 0 when the page is served on the LAN
// (there is no AP then), which is what the fallback policy needs to know: a LAN-mode page is
// never idle-closed anyway.
int portalClients();

// Serves pending HTTP requests and DNS queries. Call from loop(); cheap when inactive.
void portalPoll();

}  // namespace net

#endif  // BTWH_NET_PORTAL_H
