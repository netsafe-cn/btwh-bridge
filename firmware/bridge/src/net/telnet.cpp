#include "net/telnet.h"

#include <Arduino.h>
#include <WiFi.h>

#include "net/logring.h"
#include "net/station.h"

namespace net {
namespace telnet {
namespace {

constexpr uint16_t kPort        = 23;
constexpr uint32_t kCoalesceMs  = 1500;   // one push per 1.5 s at most; the ring is 4 KB
constexpr size_t   kSendBufSize = 4224;   // ring + NUL + slack

WiFiServer gServer(kPort);
WiFiClient gClient;
bool       gStarted   = false;
uint32_t   gLastSeq   = 0;
uint32_t   gLastPush  = 0;
char       gPeer[24]  = {0};
char       gSendBuf[kSendBufSize];

void attach(WiFiClient c) {
  gClient = c;
  const String ip = c.remoteIP().toString();
  snprintf(gPeer, sizeof(gPeer), "%s", ip.c_str());
  gLastSeq  = 0;          // force the first dump on the next poll
  gLastPush = 0;
  logring::write(logring::Level::Info, "TELNET", "client %s attached (read-only)", gPeer);
}

void detach() {
  if (gClient) gClient.stop();
  gClient = WiFiClient();
  if (gPeer[0]) logring::write(logring::Level::Info, "TELNET", "client %s gone", gPeer);
  gPeer[0] = 0;
}

}  // namespace

void poll() {
  if (!stationUp()) {
    if (gStarted) { detach(); gServer.end(); gStarted = false; }
    return;
  }
  if (!gStarted) {
    gServer.begin();
    gServer.setNoDelay(true);
    gStarted = true;
    logring::write(logring::Level::Info, "TELNET", "listening on :%u", (unsigned)kPort);
  }

  if (!gClient || !gClient.connected()) {
    WiFiClient c = gServer.available();
    if (c) attach(c);
    return;
  }

  // Read and discard: the client is a viewer. Draining matters anyway, because an unread
  // receive buffer is what makes a stuck client block the accept path.
  while (gClient.available()) gClient.read();

  const uint32_t seq = logring::sequence();
  if (seq == gLastSeq) return;
  const uint32_t now = millis();
  if (gLastPush != 0 && (now - gLastPush) < kCoalesceMs) return;

  const size_t n = logring::dump(gSendBuf, sizeof(gSendBuf));
  if (n == 0) return;

  // availableForWrite() is the only honest answer about whether a write will block. On a slow
  // or stalled client it returns 0, and the right move is to skip this tick rather than stall
  // the loop - the ring keeps the history, so the next poll catches up.
  const int can = gClient.availableForWrite();
  if (can <= 0) return;

  const size_t send = (n < (size_t)can) ? n : (size_t)can;
  gClient.write((const uint8_t*)gSendBuf, send);
  gLastPush = now;
  if (send == n) gLastSeq = seq;   // partial send: keep the old seq, resend the whole ring later
}

bool active() { return gClient && gClient.connected(); }

const char* peer() { return gPeer; }

}  // namespace telnet
}  // namespace net
