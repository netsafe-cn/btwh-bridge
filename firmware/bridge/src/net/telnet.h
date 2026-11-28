// Read-only telnet access to the log ring, so a board on a charger can be interrogated without
// unplugging it. This exists because the first long soak could not answer "why did the last
// reboot happen" without a laptop: the history had already scrolled past the UART.
//
// Deliberately read-only. A writable console on port 23 in a home LAN is an unauthenticated
// remote control for a device wired to a gas appliance; there is no version of that worth the
// convenience. The serial console keeps its commands, and it requires physical access.
//
// Only one client at a time, and only while the station is up. In AP mode the setup portal
// owns the only page a user should be looking at.
#pragma once

namespace net {
namespace telnet {

// Starts listening once the station has an address. Safe to call every loop iteration.
// Ship-closed. poll() returns immediately until this is set, so no socket is ever bound on a unit
// that has not opted in.
void setEnabled(bool on);
bool enabled();

void poll();

// True while a client is attached - lets the caller skip expensive work when nobody is reading.
bool active();

// Client count/peer for the `status` command. Empty when nobody is attached.
const char* peer();

}  // namespace telnet
}  // namespace net
