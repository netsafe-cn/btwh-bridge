// Last line of defence for an image that arrives by OTA and never works.
//
// The bootloader on this core does not implement rollback: measured 2026-11-28, otadata read
// ota_seq=2 state=VALID immediately after a swap, with no PENDING_VERIFY phase, despite
// CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y in the prebuilt libraries' sdkconfig. So a new image
// that writes cleanly, boots, and then cannot reach the broker stays there forever - the only
// exit is a USB cable. This module supplies the missing safety net in the application:
//
//   install() arms it -> first boot of the new image counts attempt 1
//                     -> no broker connection within kHealthDeadlineMs restarts the board
//                     -> attempt 2, then 3
//                     -> still nothing: point the boot partition back and restart
//
// Health is "a CONNACK arrived". That deliberately conflates two causes - a broken image and a
// broker that is down - because telling them apart from inside the device is guesswork, and the
// cost is bounded: three attempts, about six minutes, on a device that is not doing anything
// else. The old image is not necessarily better connected, but it is known to have worked.
#pragma once
#ifndef BTWH_NET_BOOT_GUARD_H
#define BTWH_NET_BOOT_GUARD_H

#include <stdint.h>

namespace net {
namespace bootguard {

// Called by the flasher once the new image is written and validated, before the caller reboots.
void armBeforeReboot();

// Called early in setup(), before the radio comes up. May switch the boot partition back and
// restart; when it returns, the board is meant to keep running.
void checkAtBoot();

// Called from the main loop: restarts the board when an armed image misses its deadline, which
// is what makes the next boot count as another failed attempt.
void poll(uint32_t nowMs);

// Called once the bridge has proved itself (MQTT CONNACK). Disarms the guard.
void markHealthy();

bool     armed();
uint8_t  attempts();
uint32_t secondsLeft(uint32_t nowMs);   // 0 when idle
const char* stateText();                // "idle" or "armed 2/3, 74 s left"

}  // namespace bootguard
}  // namespace net

#endif  // BTWH_NET_BOOT_GUARD_H
