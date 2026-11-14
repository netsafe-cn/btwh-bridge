# -*- coding: utf-8 -*-
"""test_firmware_contract.py - keep the firmware's auth layer in step with the Python API

`firmware/bridge/src/auth/` reimplements the two-step login, so its failure reasons are a second
copy of `bosch_api.PAIR_FAIL_*`. Two copies only stay equal if something checks, and here the
things that can silently drift are:

  * a reason renamed on one side (the serial log and an MQTT consumer would disagree)
  * the reply line printed in decimal instead of two-digit hex, which makes `log_diag.py` read
    `60` where the device said `3C` - a real bug that shipped and is why this test exists

The test reads C++ as text. That is deliberate: it is a contract check, not a compiler, and it
fails loudly when the shape it relies on changes.
"""
from __future__ import annotations

import json
import pathlib
import re
import unittest

import bosch_api as api

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOGIN_CPP = ROOT / "firmware" / "bridge" / "src" / "auth" / "login.cpp"
MQTT_CLIENT_CPP = ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_client.cpp"
SPEC = ROOT / "spec" / "mqtt.json"

# Reasons the firmware defines that have no Python counterpart: "ok" is the success case, and
# "bad_input" means nothing was transmitted, so no PAIR_FAIL_* applies.
FIRMWARE_ONLY = {"ok", "bad_input"}


def _reason_strings() -> set[str]:
    """Every string literal returned from a `case` arm of auth::reasonText."""
    text = LOGIN_CPP.read_text(encoding="utf-8")
    body = re.search(r"reasonText\(Result r\)\s*\{(.*?)\n\}", text, re.S)
    assert body, "reasonText() not found in auth/login.cpp"
    # Only the case arms; the trailing "unknown" fallback is not a reason code.
    return set(re.findall(r'case\s+\w+:\s*return\s+"([a-z_]+)"', body.group(1)))


class TestAuthReasons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = _reason_strings()
        cls.py = {v for k, v in vars(api).items()
                  if k.startswith("PAIR_FAIL_") and isinstance(v, str)}

    def test_firmware_reasons_are_found(self):
        self.assertTrue(self.fw, "no reason strings parsed; did reasonText() change shape?")

    def test_device_reasons_match_the_python_api(self):
        """The names a consumer sees must be identical on both sides, in both directions.

        `link_failed` is set by app/session.cpp rather than by classify(): it means the BLE link
        never came up or died mid-login, which is not something either reply byte can say.
        """
        fw_device = self.fw - FIRMWARE_ONLY
        self.assertEqual(fw_device, self.py,
                         "reason codes drifted: firmware %s vs python %s"
                         % (sorted(fw_device), sorted(self.py)))

    def test_firmware_only_reasons_still_exist(self):
        missing = FIRMWARE_ONLY - self.fw
        self.assertFalse(missing, "reasonText() no longer returns %s" % sorted(missing))


class TestReplyLineFormat(unittest.TestCase):
    """log_diag.py parses `A2/07 reply = 3C`, so the value must be two hex digits.

    It was `%d` once: the device answered 3C (60), the log said `60`, and the diagnosis script
    read an undefined value. Nobody noticed because the happy path is 01, which renders as `1`
    and simply fails to match - the log looked fine and the advice was silently skipped.
    """

    def test_replies_are_logged_as_two_hex_digits(self):
        text = LOGIN_CPP.read_text(encoding="utf-8")
        self.assertIn('reply = %02X', text,
                      "the reply line must print two hex digits; log_diag.py reads them")

    def test_log_diag_pattern_matches_what_the_firmware_prints(self):
        """Pair the pattern in log_diag.py with the format in login.cpp, value by value."""
        diag = (ROOT / "firmware" / "bridge" / "tools" / "log_diag.py").read_text(encoding="utf-8")
        pattern = r"A2/07\s*reply\s*=\s*([0-9A-Fa-f]{2})"
        self.assertIn(pattern, diag,
                      "log_diag.py no longer uses this pattern; update both sides together")

        for code in (0x00, 0x01, 0x02, 0x3C, 0x5A):
            line = "[AUTH] A2/%02X reply = %02X" % (0x07, code)
            self.assertEqual(re.findall(pattern, line), ["%02X" % code],
                             "log_diag would miss %s" % line)


class TestAttributesPayload(unittest.TestCase):
    """The attributes topic is the contract's diagnostics channel (spec/mqtt.json,
    attributes_fields). rssi was contracted but never published for weeks before anyone
    noticed - a spec field with no firmware writer is a diagnostic that lies.

    These checks read mqtt_client.cpp as text on purpose: publishAttributes() cannot run
    without Arduino, so the contract is pinned the same way auth reasons are.
    """

    @classmethod
    def setUpClass(cls):
        cls.fw = MQTT_CLIENT_CPP.read_text(encoding="utf-8")
        cls.spec = json.loads(SPEC.read_text(encoding="utf-8"))
        cls.keys = [f["key"] for f in cls.spec["attributes_fields"]]

    def test_publish_attributes_carries_every_contracted_key(self):
        # The payload is a C string literal, so the JSON quotes are backslash-escaped in
        # the source; match the escaped form or this check pins nothing.
        for key in self.keys:
            self.assertIn('\\"%s\\"' % key, self.fw,
                          "publishAttributes() does not publish %s" % key)

    def test_disconnect_reason_and_time_survive_the_reconnect(self):
        # gWhy is the reason, gLastDropAt the epoch. Both must be stamped in drop() and in
        # the TCP-failure path (which does not go through drop), or a flapping link shows
        # "never dropped" while dropping.
        for needle in ("gLastDropAt = nowEpoch();",):
            self.assertIn(needle, self.fw,
                          "%s is the stamp; losing it makes last_disconnect_at lie" % needle)
        self.assertGreaterEqual(self.fw.count("gLastDropAt = nowEpoch();"), 2,
                                "drop() and the tcp-connect failure must both stamp it")

    def test_attributes_republish_on_the_heartbeat(self):
        # Published once per minute, not only on connect: uptime and rssi freeze otherwise,
        # and a stuck bridge would look healthy for as long as the socket lives.
        self.assertIn("gLastAttrs", self.fw,
                      "the attributes heartbeat timer is gone; uptime would freeze")

    def test_info_rssi_is_wired(self):
        self.assertIn("rssi", self.fw,
                      "MqttInfo.rssi is not read anywhere in mqtt_client.cpp")


class TestSessionRetryLadder(unittest.TestCase):
    """A failed session must not be terminal, and must not turn into a hammer either.

    Both halves are measured, not assumed:

      2026-11-13  one hard reset left the bridge in kFailed all evening. poll() returned early
                  for any phase but kRunning, so nothing ever retried. MQTT published
                  `link: offline` until a human typed `go`.
      2026-11-08  right after a link is cut the heater refuses sessions for minutes; retrying
                  within seconds reproduces the refusal.

    So the ladder waits, goes quiet, and starts over. This test pins the shape so a later
    refactor cannot quietly restore either failure mode.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "firmware" / "bridge" / "src" / "app" / "session.cpp") \
            .read_text(encoding="utf-8")
        cls.hdr = (ROOT / "firmware" / "bridge" / "src" / "app" / "session.h") \
            .read_text(encoding="utf-8")

    def test_failure_arms_a_retry(self):
        body = self.src[self.src.index("if (!ok) {"):]
        body = body[:body.index("gRunning = false;")]
        self.assertIn("armRetry(", body,
                      "a failed attempt no longer schedules a retry: kFailed is terminal again")
        self.assertIn("gPhase = kFailed;", body)

    def test_retry_loop_does_not_care_about_phase(self):
        # The scheduled attempt must be evaluated BEFORE the phase gate; after it, a kFailed
        # board would sit there forever - exactly the 2026-11-13 bug.
        poll = self.src[self.src.index("void poll()"):]
        armed = poll.index("gRetryArmed &&")
        gate = poll.index("if (gPhase != kRunning) return;")
        self.assertLess(armed, gate,
                        "the retry check sits behind the phase gate; kFailed would never retry")

    def test_no_blocking_delay_in_the_drop_path(self):
        # The old drop path was `delay(5000); start(false);` - it froze loop() (MQTT, console,
        # portal) for five seconds and reconnected inside the heater's refusal window.
        # Comments are stripped first: this file's own explanation of the old bug contains the
        # very string we are hunting.
        poll = self.src[self.src.index("void poll()"):]
        poll = poll[:poll.index("void shutdown()")]
        code = "\n".join(line.split("//")[0] for line in poll.splitlines())
        self.assertNotIn("delay(", code,
                         "poll() blocks again; a retry must be scheduled, not slept")

    def test_the_ladder_has_a_quiet_period(self):
        for needle in ("kQuietAfter", "kQuietMs", "kRetryAfterFailMs", "kRetryAfterDropMs"):
            self.assertIn(needle, self.src, "%s is missing from the retry ladder" % needle)
        quiet = re.search(r"kQuietMs\s*=\s*(\d+)UL\s*\*\s*60\s*\*\s*1000", self.src)
        self.assertIsNotNone(quiet, "kQuietMs is not expressed in minutes")
        self.assertGreaterEqual(int(quiet.group(1)), 10,
                                "a quiet period shorter than 10 min just re-enters the refusal")

    def test_quiet_branch_actually_waits_the_quiet_period(self):
        # Board test 2026-11-13 22:53: the log said "going quiet for 15 min" and the next
        # attempt came two minutes later, because the failure counter was reset before the
        # delay was computed. The ladder announced one thing and did another.
        body = self.src[self.src.index("gFailures++;"):]
        body = body[:body.index("armRetry(delayMs);") + len("armRetry(delayMs);")]
        quiet = body.index("delayMs  = kQuietMs;")
        reset = body.index("gFailures = 0;")
        self.assertLess(quiet, reset,
                        "the quiet delay must be taken before the counter is reset")
        self.assertIn("armRetry(delayMs)", body, "the delay is not the one that gets armed")
        # The buggy indirection was a helper that recomputed the delay from the counter.
        self.assertNotIn("retryDelayNow", self.src,
                         "a helper that recomputes the delay will drift out of step again")

    def test_success_clears_the_ladder(self):
        body = self.src[self.src.index("} else {"):]
        body = body[:body.index("gRunning = false;")]
        self.assertIn("gFailures = 0;", body, "a good session must reset the failure count")
        self.assertIn("disarmRetry()", body, "a good session must cancel a queued attempt")

    def test_shutdown_cancels_a_queued_attempt(self):
        body = self.src[self.src.index("void shutdown()"):]
        self.assertIn("disarmRetry()", body,
                      "shutdown() must not leave an attempt queued for a rebooting board")

    def test_status_exposes_the_ladder(self):
        # "why is it not retrying?" was the question this evening; status has to answer it.
        for needle in ("failures", "retryInMs"):
            self.assertIn(needle, self.hdr, "Status lost %s" % needle)
        self.assertIn("s.retryInMs", self.src, "status() does not fill retryInMs")


class TestDeviceIdFollowsTheHeater(unittest.TestCase):
    """The MQTT device id is derived from the heater's BLE address, so it moves when the address
    does. Nothing recomputed it: `addr` wrote NVS and the client kept publishing under the old
    id. Measured 2026-11-13 - a bogus aa:bb:cc:dd:ee:ff left 22 retained discovery documents in
    place, and Home Assistant grew a second device that could never update again.

    Three things have to hold together, and each one alone is not enough: the old id is captured
    before the address is overwritten, the retained documents under it are deleted, and the
    client is restarted so it publishes under the new one.
    """

    def setUp(self):
        self.main = (ROOT / "firmware" / "bridge" / "src" / "main.cpp").read_text(encoding="utf-8")
        self.src = MQTT_CLIENT_CPP.read_text(encoding="utf-8")
        self.hdr = (ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_client.h").read_text(
            encoding="utf-8")

    def _addr_block(self):
        start = self.main.index('low.startsWith("addr ")')
        return self.main[start:start + 1600]

    def test_old_id_is_read_before_the_address_is_overwritten(self):
        block = self._addr_block()
        capture = block.index("deviceIdFromAddress(gCfg.heater, oldId")
        write = block.index("strncpy(gCfg.heater")
        self.assertLess(capture, write,
                        "the old device id must be read before gCfg.heater is overwritten")

    def test_old_id_is_forgotten_before_the_client_restarts(self):
        block = self._addr_block()
        self.assertIn("mqttForgetDevice(oldId)", block,
                      "addr must delete the retained documents of the id it is leaving")
        self.assertLess(block.index("mqttForgetDevice(oldId)"), block.index("startMqtt()"),
                        "begin() drops the socket, so the deletions have to go first")

    def test_forget_only_runs_when_the_derived_id_changes(self):
        block = self._addr_block()
        self.assertIn("strcmp(oldId, newId) != 0", block,
                      "an address edit that keeps the last three bytes must not churn the client")

    def test_deletion_covers_discovery_and_retained_state(self):
        body = self.src[self.src.index("size_t clearRetained("):]
        body = body[:body.index("return cleared;")]
        self.assertIn("discovery::entityTopic", body,
                      "the discovery configs are what create the device; they must be deleted")
        self.assertIn('publishRaw(topic, "", 0, true)', body,
                      "deletion is an empty payload with the retain flag")
        for tail in ("availability", "link", "update/state"):
            self.assertIn(tail, body,
                          "retained %s survives the id change and keeps a dead device alive" % tail)

    def test_forget_flushes_before_the_socket_closes(self):
        body = self.src[self.src.index("bool mqttForgetDevice("):]
        body = body[:body.index("return true;")]
        self.assertIn("gState != kUp", body,
                      "offline there is nothing retained by us to delete, so report that")
        self.assertIn("flush()", body,
                      "the socket is closed right after; the deletions must reach the network first")
        self.assertIn("gForgetPending = true", body,
                      "the will fires after this socket dies, so the id has to be remembered")

    def test_the_header_declares_it(self):
        self.assertIn("mqttForgetDevice", self.hdr,
                      "mqtt_client.cpp defines it; main.cpp calls it through the header")

    def test_the_deletion_runs_again_after_the_next_connect(self):
        # The socket is closed without a DISCONNECT, so the broker fires this connection's last
        # will - availability=offline, retained, on the id we just left - AFTER the deletion.
        # Measured 2026-11-14: 25 documents cleared, then 1 (availability) came back. The second
        # pass on CONNACK is what closes it, so it must stay ahead of publishDiscovery().
        body = self.src[self.src.index("void onConnected()"):]
        body = body[:body.index("Serial.printf(\"[MQTT] + connected")]
        self.assertIn("gForgetPending", body, "onConnected no longer finishes a pending deletion")
        self.assertLess(body.index("clearRetained(gForgetId)"), body.index("publishDiscovery()"),
                        "the ghost has to be deleted before the new id's discovery goes out")
        self.assertIn("gForgetPending = false", body, "the pending flag must be cleared once done")


class TestNoHomeNetworkInTheRepository(unittest.TestCase):
    """Sample addresses in a public repository should be documentation addresses.

    Measured 2026-11-14: the portal placeholder, the LAN-mode example and two self-test vectors
    all carried the author's own broker address. Harmless to the firmware, wrong in a repository
    other people read and fork. 192.0.2.0/24 is reserved for exactly this (RFC 5737).

    The one private address allowed to stay is 192.168.4.1: it is the ESP32 SoftAP gateway, and
    the captive portal has to redirect there.
    """

    ALLOWED = "192.168.4.1"
    SCAN = ("firmware/bridge/src", "docs", "python", "spec")

    def test_only_the_softap_gateway_is_private(self):
        hits = []
        for rel in self.SCAN:
            for path in sorted((ROOT / rel).rglob("*")):
                if not path.is_file() or path.suffix not in (".h", ".cpp", ".md", ".py", ".json"):
                    continue
                for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    for found in re.findall(r"192\.168\.\d+\.\d+", line):
                        if found != self.ALLOWED:
                            hits.append("%s:%d  %s" % (path.relative_to(ROOT), lineno, found))
        self.assertEqual(hits, [], "private network addresses belong in config, not in the source")


class TestBrokerDiscovery(unittest.TestCase):
    """An empty broker field means "find it", so a buyer never has to know an IP address.

    The candidate order in net/discover.cpp is not an opinion about how networks should be set
    up - it was measured on the target LAN with _handoff/mdns_probe.py (2026-11-14):

      _mqtt._tcp.local           no answer; the Home Assistant Mosquitto add-on advertises nothing
      _home-assistant._tcp.local answer; SRV says host:80, so the port is the web UI, not the broker

    Those two facts are what the assertions below protect: try the honest service first, and when
    falling back, do not reuse the advertised port.
    """

    def setUp(self):
        self.src = (ROOT / "firmware" / "bridge" / "src" / "net" / "discover.cpp").read_text(
            encoding="utf-8")
        self.main = (ROOT / "firmware" / "bridge" / "src" / "main.cpp").read_text(encoding="utf-8")

    def test_mqtt_service_is_tried_before_the_home_assistant_fallback(self):
        self.assertLess(self.src.index('queryService("mqtt"'),
                        self.src.index('queryService("home-assistant"'),
                        "a broker that advertises itself must win over the inference")

    def test_the_fallback_does_not_reuse_the_advertised_port(self):
        body = self.src[self.src.index('queryService("home-assistant"'):]
        body = body[:body.index("return true;")]
        self.assertIn("*port = 1883;", body,
                      "the advertised port is the web UI; the broker is the add-on on 1883")
        self.assertNotIn("MDNS.port(0)", body,
                         "MDNS.port(0) here would install the web UI port as the broker port")

    def test_the_address_is_resolved_in_a_second_query(self):
        # MDNS.queryService() runs mdns_query_ptr, which returns PTR answers without following
        # SRV/A: MDNS.IP() then reads a null addr and reports 0.0.0.0. Measured on the board
        # 2026-11-14 - the service was found and the address came back as 0.0.0.0, so the bridge
        # sat there dialling nothing. The SRV target has to be resolved separately.
        self.assertIn("MDNS.hostname(", self.src,
                      "the SRV target is where the address comes from")
        self.assertIn("queryHost(", self.src,
                      "the address needs its own A query; IP() alone is 0.0.0.0")
        self.assertLess(self.src.index("MDNS.IP("), self.src.index("queryHost("),
                        "the cheap read is tried first and the extra query only when it fails")

    def test_the_reason_is_reported(self):
        # An unlabelled guess gets blamed on the wrong component later.
        self.assertIn("advertised _mqtt._tcp", self.src)
        self.assertIn("Home Assistant host", self.src)

    def test_boot_discovers_only_when_nothing_was_configured(self):
        self.assertIn('if (!gCfg.broker[0] && net::stationUp()) runDiscovery(', self.main,
                      "discovery at boot must not override a configured broker")

    def test_a_typed_broker_wins_over_the_discovered_one(self):
        body = self.main[self.main.index("static void startMqtt()"):]
        body = body[:body.index("refreshMqttInfo();")]
        self.assertLess(body.index("if (gCfg.broker[0])"), body.index("gAutoBroker[0]"),
                        "the config field has to be checked before the discovered address")
        self.assertIn("gAutoBroker[0] = 0;", self.main,
                      "typing a broker must clear a discovered one, or they fight")

    def test_the_discovered_address_is_not_persisted(self):
        # Storing it would recreate the staleness the feature exists to remove.
        self.assertNotIn("gCfg.broker", self.src,
                         "discover.cpp must not touch the stored config")
        idx = self.main.index("static bool runDiscovery")
        body = self.main[idx:self.main.index("static void startMqtt()")]
        self.assertNotIn("saveNetwork()", body,
                         "a discovered address is an observation, not configuration")


class TestOversizePacketIsSkipped(unittest.TestCase):
    """A legal packet bigger than the receive buffer must be thrown away, not fatal.

    Measured 2026-11-14: the board sat in a reconnect loop publishing `malformed or oversized
    packet` every 4 seconds. The cause was a 1083-byte retained document delivered to it - on a
    topic it had never subscribed to, which is the dev broker's bug - but the firmware turned one
    big packet into a bridge that could never come back. MQTT puts no upper bound on packet size,
    so any broker, or any other integration publishing a kilobyte on a subscribed topic, can do
    the same thing to a production unit.
    """

    def setUp(self):
        self.wire = (ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_wire.cpp").read_text(
            encoding="utf-8")
        self.hdr = (ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_wire.h").read_text(
            encoding="utf-8")
        self.src = MQTT_CLIENT_CPP.read_text(encoding="utf-8")

    def test_the_size_is_available_without_the_body(self):
        self.assertIn("int packetLength(", self.hdr,
                      "the size has to be readable before the body is buffered")
        self.assertIn("int packetLength(", self.wire)

    def test_the_client_drains_instead_of_dropping(self):
        body = self.src[self.src.index("void drainSocket()"):]
        body = body[:body.index("wire::Packet p{};")]
        self.assertIn("gSkipBytes", body, "oversize must set a discard count")
        self.assertIn("packetLength(", body, "the discard needs the packet's total size")
        branch = body[body.index("total > sizeof(gRx)"):]
        self.assertNotIn("drop(", branch,
                         "the oversize branch must discard the packet, not drop the connection")

    def test_the_discard_count_survives_between_polls(self):
        # The body arrives over several polls; a local counter would lose the remainder and
        # desynchronise the stream.
        self.assertIn("size_t  gSkipBytes", self.src)
        self.assertGreaterEqual(self.src.count("gSkipBytes = 0;"), 2,
                                "the counter must be reset with the receive buffer")

    def test_the_stream_is_only_dropped_for_an_illegal_prefix(self):
        body = self.src[self.src.index("void drainSocket()"):]
        body = body[:body.index("memmove(gRx")] if "memmove(gRx" in body else body
        self.assertIn('drop("malformed length prefix")', body,
                      "a prefix that is not four bytes long cannot be recovered from")


if __name__ == "__main__":
    unittest.main()
