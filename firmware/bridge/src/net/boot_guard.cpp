#include "net/boot_guard.h"

#include <Arduino.h>
#include <Preferences.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>

namespace net {
namespace bootguard {

namespace {

// Three attempts at two minutes each: enough for a slow DHCP lease or a broker restart, short
// enough that a genuinely broken image is off the device in about six minutes.
constexpr uint8_t  kMaxAttempts      = 3;
constexpr uint32_t kHealthDeadlineMs = 120000;
constexpr uint8_t  kImageMagic       = 0xE9;   // every ESP32 app image starts with this byte

Preferences gPrefs;
bool        gLoaded   = false;
bool        gArmed    = false;
bool        gHealthy  = false;
uint8_t     gAttempts = 0;
uint32_t    gTargetAddr = 0;      // address of the slot the flasher wrote
uint32_t    gBootMs     = 0;

void load() {
  if (gLoaded) return;
  gPrefs.begin("btwhguard", false);
  gArmed      = gPrefs.getBool("armed", false);
  gAttempts   = gPrefs.getUChar("fails", 0);
  gTargetAddr = gPrefs.getULong("target", 0);
  gLoaded     = true;
}

void store() {
  load();
  gPrefs.putBool("armed", gArmed);
  gPrefs.putUChar("fails", gAttempts);
  gPrefs.putULong("target", gTargetAddr);
}

// A fallback slot is only worth switching to if something actually bootable is in it. A board
// flashed over USB for the first time has an empty app1, and pointing the bootloader there would
// turn a recoverable image into a dead device.
bool holdsImage(const esp_partition_t* p) {
  if (!p || p->size < 4) return false;
  uint8_t magic = 0;
  if (esp_partition_read(p, 0, &magic, 1) != ESP_OK) return false;
  return magic == kImageMagic;
}

}  // namespace

void armBeforeReboot() {
  load();
  const esp_partition_t* boot = esp_ota_get_boot_partition();
  gArmed      = true;
  gAttempts   = 0;
  gTargetAddr = boot ? boot->address : 0;
  store();
  Serial.printf("[OTA] boot guard armed: first boot of %s counts as attempt 1/%u\n",
                boot ? boot->label : "?", (unsigned)kMaxAttempts);
}

void checkAtBoot() {
  load();
  gBootMs = millis();
  if (!gArmed) return;

  const esp_partition_t* running = esp_ota_get_running_partition();
  if (!running) { gArmed = false; store(); return; }

  if (running->address != gTargetAddr) {
    // The switch back already happened on an earlier boot; this is the old image running again.
    Serial.printf("[BOOT] ota guard: running %s again (fallback done), disarming\n",
                  running->label);
    gArmed    = false;
    gAttempts = 0;
    store();
    return;
  }

  if (gAttempts < 255) gAttempts++;

  if (gAttempts > kMaxAttempts) {
    const esp_partition_t* other = esp_ota_get_next_update_partition(nullptr);
    if (holdsImage(other) && esp_ota_set_boot_partition(other) == ESP_OK) {
      Serial.printf("[BOOT] x ota guard: %u attempts without a broker connection, "
                    "switching back to %s\n", (unsigned)gAttempts, other->label);
      gArmed    = false;
      gAttempts = 0;
      store();
      Serial.flush();
      delay(80);
      esp_restart();
    }
    // Nothing usable to go back to: staying on an image that at least boots beats pointing the
    // bootloader at an empty partition and having no console at all.
    Serial.printf("[BOOT] x ota guard: %u attempts, but %s holds no image - staying here\n",
                  (unsigned)gAttempts, other ? other->label : "(no other slot)");
    gArmed = false;
    store();
    return;
  }

  store();
  Serial.printf("[BOOT] ota guard: attempt %u/%u on %s, health due in %lu s\n",
                (unsigned)gAttempts, (unsigned)kMaxAttempts, running->label,
                (unsigned long)(kHealthDeadlineMs / 1000));
}

void poll(uint32_t nowMs) {
  if (!gArmed || gHealthy) return;
  if (nowMs - gBootMs < kHealthDeadlineMs) return;
  Serial.printf("[BOOT] x ota guard: attempt %u/%u never reached the broker, restarting\n",
                (unsigned)gAttempts, (unsigned)kMaxAttempts);
  Serial.flush();
  delay(80);
  esp_restart();
}

void markHealthy() {
  gHealthy = true;
  if (!gArmed) return;
  gArmed    = false;
  gAttempts = 0;
  store();
  Serial.println("[OTA] + health confirmed (broker reachable), boot guard disarmed");
}

bool armed() { load(); return gArmed; }

uint8_t attempts() { load(); return gAttempts; }

uint32_t secondsLeft(uint32_t nowMs) {
  if (!gArmed) return 0;
  const uint32_t elapsed = nowMs - gBootMs;
  return elapsed >= kHealthDeadlineMs ? 0 : (kHealthDeadlineMs - elapsed) / 1000;
}

const char* stateText() {
  static char buf[40];
  load();
  if (!gArmed) return "idle";
  snprintf(buf, sizeof(buf), "armed %u/%u, %lu s left",
           (unsigned)gAttempts, (unsigned)kMaxAttempts, (unsigned long)secondsLeft(millis()));
  return buf;
}

}  // namespace bootguard
}  // namespace net
