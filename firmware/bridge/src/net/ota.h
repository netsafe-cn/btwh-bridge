// OTA flasher: download an image over HTTP and write it to the inactive slot.
//
// Layer rule (ADR-008): this is an I/O shell like station.cpp/portal.cpp, not a payload
// module - it may include Arduino/HTTPClient/Update, and it may not be included by any
// host-testable file. The URL grammar it trusts lives in net/mqtt_rules.cpp (parseOtaUrl),
// which IS host-testable; the split means "what the flasher will fetch" is pinned by tests
// while "how it fetches" is a single read-on-the-board file.
//
// Contract, deliberately narrow:
// - http only. https needs a TLS stack (flash + heap) that this device does not budget
//   for; parseOtaUrl already refuses it, so this function never sees one.
// - install() never reboots. It returns true only when the image is fully written and
//   validated; the caller owns the restart decision, so the serial log stays honest
//   ("written and validated" is printed before any reboot).
// - On any failure the running firmware is untouched: Update.end(true) validates before
//   the new slot is marked bootable, and a failure path calls Update.abort() so a later
//   attempt does not inherit half-written state.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_OTA_H
#define BTWH_NET_OTA_H

#include <stddef.h>
#include <stdint.h>

namespace net {
namespace ota {

// Fetches http://host:port/path and flashes it to the inactive OTA slot.
// host/port/path are the outputs of rules::parseOtaUrl (already validated: host
// non-empty, port 1..65535, path starts with '/'). Returns true when the image is
// fully written and Update's checksum validated; the caller reboots.
bool install(const char* host, uint16_t port, const char* path);

}  // namespace ota
}  // namespace net

#endif  // BTWH_NET_OTA_H
