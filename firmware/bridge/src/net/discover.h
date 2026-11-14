// mDNS broker discovery: find the broker without asking the user for an IP address.
//
// Why this exists: an IP address in a setup form is a promise the network will not change, and
// networks change. Measured 2026-11-14 - the same Home Assistant was carried into another
// building, came up on a different subnet, and every stored address went stale at once. A
// consumer product cannot ask its buyer to look that up.
//
// The candidate order is not a guess about what "should" be advertised; it was measured on the
// target network with _handoff/mdns_probe.py before this file was written:
//
//   _mqtt._tcp.local           NO ANSWER. The Home Assistant Mosquitto *add-on* does not
//                              advertise an MQTT service, so scanning for one finds nothing on
//                              the most likely deployment of all.
//   _home-assistant._tcp.local  ANSWER. Instance -> SRV -> host:80, A -> the host's address.
//
// So: try the honest service first (a broker on a NAS or a Pi often does advertise it), then fall
// back to "the broker is the add-on on the Home Assistant host", which is where it is for
// everybody running the add-on. `how` names the rule that answered, because an unlabelled guess
// gets blamed on the wrong component later.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace net {

// Fills `out` with a dotted-quad host and `port` with the port to use. `how` receives a short
// human-readable reason. Returns false when the station is down, mDNS will not start, or nothing
// was found - in which case the caller falls back to whatever the user configured.
bool discoverBroker(char* out, size_t cap, uint16_t* port, char* how, size_t howCap);

}  // namespace net
