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
// Adds one host to the built-in allowlist, or clears it when given null/empty. The list exists
// so a URL published to the MQTT broker cannot point the flasher anywhere; this entry is set
// from the serial console, which is physical access, so the trust argument still holds.
void setExtraHost(const char* host);
const char* extraHost();

bool install(const char* host, uint16_t port, const char* path);

// Rollback is compiled into this build (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y in the 3.3.12
// sdkconfig). A freshly written image boots once in PENDING_VERIFY and is thrown away at the
// next reset unless it answers. confirmBoot() is that answer: returns true only when this call
// is the one that moved the slot to VALID.
bool confirmBoot();

// Which slot this binary is running from, and the state the bootloader has recorded for it.
// "direct" means the slot was flashed over USB and no OTA has ever swapped it - there is no
// otadata entry to report, which is a normal state and not an error.
const char* runningSlot();
const char* runningState();

}  // namespace ota
}  // namespace net

#endif  // BTWH_NET_OTA_H
