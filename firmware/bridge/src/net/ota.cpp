#include "net/ota.h"

#include <Arduino.h>
#include <HTTPClient.h>
#include <Update.h>
#include <WiFi.h>

namespace net {
namespace ota {

// 2 KB at a time: big enough to keep the TCP window busy, small enough to yield between
// writes so WiFi/stack housekeeping keeps running during the transfer.
constexpr size_t   kChunk       = 2048;
// Whole-transfer budget. A 2 MB image over a weak 2.4 GHz link is tens of seconds; this
// ceiling exists so a server that stalls mid-stream cannot wedge the board forever - the
// MQTT loop is not running while install() blocks.
constexpr uint32_t kBudgetMs    = 120000;
// Gap between progress lines; one line per chunk would drown the log, and the download
// speed is a per-second number, not a per-chunk one.
constexpr uint32_t kReportMs    = 2000;

bool install(const char* host, uint16_t port, const char* path) {
  if (!host || !host[0] || !path || !path[0]) return false;

  char url[128];
  snprintf(url, sizeof(url), "http://%s:%u%s", host, (unsigned)port, path);

  HTTPClient http;
  // 15 s connect/headers: the URL came over MQTT, so a reachable server is the common
  // case and an unreachable one should fail fast enough to stay inside one backoff slot.
  http.setTimeout(15000);
  if (!http.begin(url)) {
    Serial.printf("[OTA] x refused URL locally: %s\n", url);
    return false;
  }

  const int code = http.GET();
  Serial.printf("[OTA] GET %s -> HTTP %d\n", url, code);
  if (code != 200) {
    // 204/206 are refused on purpose: anything but a plain full-body 200 is not an image
    // this flasher agreed to write.
    http.end();
    return false;
  }

  const int len = http.getSize();          // -1 = server did not send Content-Length
  Serial.printf("[OTA] image %s (%d bytes reported)\n", len < 0 ? "of unknown size" : "", len);
  if (len == 0) {
    http.end();
    return false;
  }
  // UPDATE_SIZE_UNKNOWN makes Update size the write against the free space of the slot;
  // with a known length, Update uses it as an exact expectation and refuses a short image.
  if (!Update.begin(len > 0 ? (size_t)len : UPDATE_SIZE_UNKNOWN)) {
    Serial.printf("[OTA] x Update.begin failed (%s)\n", Update.errorString());
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  if (!stream) {
    Serial.println("[OTA] x no stream to read");
    Update.abort();
    http.end();
    return false;
  }

  uint8_t  buf[kChunk];
  size_t   written      = 0;
  uint32_t t0           = millis();
  uint32_t lastReportMs = 0;

  while (http.connected() && (len < 0 || (int)written < len)) {
    if (millis() - t0 > kBudgetMs) {
      Serial.printf("[OTA] x exceeded %lu ms budget at %u bytes\n",
                    (unsigned long)kBudgetMs, (unsigned)written);
      Update.abort();
      http.end();
      return false;
    }

    // available() returning 0 with the connection still up means "wait for the next TCP
    // segment", not "done" - a small poll delay keeps this from spinning at loop speed.
    const int avail = stream->available();
    if (avail <= 0) {
      delay(10);
      continue;
    }
    const size_t want = (size_t)avail < sizeof(buf) ? (size_t)avail : sizeof(buf);
    const size_t n    = stream->readBytes(buf, want);
    if (n == 0) {
      // readBytes returning 0 on a connected stream with data announced is a transport
      // error as far as this flasher is concerned; Update stays the judge of whether the
      // bytes so far already make a complete image.
      break;
    }
    if (Update.write(buf, n) != n) {
      Serial.printf("[OTA] x flash write failed at %u bytes (%s)\n",
                    (unsigned)written, Update.errorString());
      Update.abort();
      http.end();
      return false;
    }
    written += n;

    if (millis() - lastReportMs > kReportMs) {
      lastReportMs = millis();
      const uint32_t rate = written / ((millis() - t0) / 1000 + 1);
      if (len < 0) {
        Serial.printf("[OTA]   %u bytes so far (%lu B/s)\n",
                      (unsigned)written, (unsigned long)rate);
      } else {
        Serial.printf("[OTA]   %u / %d bytes (%lu B/s)\n",
                      (unsigned)written, len, (unsigned long)rate);
      }
    }
  }

  if (len > 0 && (int)written != len) {
    Serial.printf("[OTA] x short image: %u of %d bytes\n", (unsigned)written, len);
    Update.abort();
    http.end();
    return false;
  }

  // end(true): validate (checksum) and mark the slot bootable. A failure here leaves the
  // running firmware fully in charge - the next boot goes to the old partition.
  if (!Update.end(true)) {
    Serial.printf("[OTA] x image failed validation (%s)\n", Update.errorString());
    Update.abort();
    http.end();
    return false;
  }

  http.end();
  Serial.printf("[OTA] + %u bytes written and validated in %lu ms, ready to reboot\n",
                (unsigned)written, (unsigned long)(millis() - t0));
  return true;
}

}  // namespace ota
}  // namespace net
