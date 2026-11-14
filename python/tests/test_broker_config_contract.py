# -*- coding: utf-8 -*-
"""test_broker_config_contract.py - pin the broker credential wiring, end to end

The production broker (Mosquitto on the HAOS host) refuses anonymous connections, so a
reflashed board is useless unless the account travels with the config. The chain is long and
every link is in a different layer, and each one is one rename away from silently dropping
the account - the MQTT packet would still be well-formed and the broker would just say no:

    portal_html.h <input> -> portal.cpp arg() -> ProvisionRequest -> onProvisioned ->
    Config (NVS) -> startMqtt -> mqttSetCredentials -> CONNECT username flag

This test reads C++ as text, like test_firmware_contract.py: a contract check, not a
compiler, failing loudly when the shape it relies on changes.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "bridge" / "src"

PORTAL_HTML = (SRC / "net" / "portal_html.h").read_text(encoding="utf-8")
PORTAL_CPP = (SRC / "net" / "portal.cpp").read_text(encoding="utf-8")
PROVISION_H = (SRC / "net" / "provision.h").read_text(encoding="utf-8")
CONFIG_CPP = (SRC / "app" / "config.cpp").read_text(encoding="utf-8")
MAIN_CPP = (SRC / "main.cpp").read_text(encoding="utf-8")
MQTT_CLIENT_CPP = (SRC / "net" / "mqtt_client.cpp").read_text(encoding="utf-8")


def _function_body(text: str, signature: str) -> str:
    """The source between the signature's opening brace and its matching closing brace."""
    start = text.index(signature)
    body_start = text.index("{", start)
    depth = 0
    for i in range(body_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[body_start:i]
    raise AssertionError("no matching brace for " + signature)


class TestPortalForm(unittest.TestCase):
    def test_form_has_the_three_broker_fields(self):
        for name in ("broker", "mqttuser", "mqttpass"):
            self.assertIn('name="%s"' % name, PORTAL_HTML,
                          "the portal form lost its %s input" % name)

    def test_handle_save_reads_all_three(self):
        for name in ("broker", "mqttuser", "mqttpass"):
            self.assertIn('gServer->arg("%s")' % name, PORTAL_CPP,
                          "handleSave no longer reads the %s field" % name)


class TestProvisionRequest(unittest.TestCase):
    def test_request_carries_the_broker_fields(self):
        for field in ("broker", "mqttUser", "mqttPass"):
            self.assertIn("char %s[" % field, PROVISION_H,
                          "ProvisionRequest lost its %s field" % field)


class TestConfigStorage(unittest.TestCase):
    def test_broker_account_survives_reboot(self):
        """`mqttuser`/`mqttpass` must be in both load() and saveNetwork(), or a reboot
        drops the account and the broker refuses the anonymous reconnect."""
        for key in ("mqttuser", "mqttpass"):
            self.assertIn('getString("%s"' % key, CONFIG_CPP,
                          "Config::load() no longer reads NVS key %s" % key)
            self.assertIn('putString("%s"' % key, CONFIG_CPP,
                          "Config::saveNetwork() no longer writes NVS key %s" % key)

    def test_wipe_clears_the_broker_account(self):
        body = _function_body(CONFIG_CPP, "void Config::wipe()")
        for field in ("mqttUser", "mqttPass"):
            self.assertIn("%s[0] = 0" % field, body,
                          "wipe() leaves %s behind - a wiped board still authenticates" % field)


class TestWiring(unittest.TestCase):
    def test_start_mqtt_passes_the_stored_account(self):
        """The one place the client is (re)started must hand it both the address and the
        account, credentials AFTER begin: begin() no longer resets them, but the call order
        is still the documented contract.

        The address is `target`, not `gCfg.broker` directly, since an empty config field is
        resolved either from mDNS or to nothing at all - the client still has to be started
        without one, because `begin("")` is what clears a previous broker."""
        body = _function_body(MAIN_CPP, "static void startMqtt() {")
        self.assertIn("net::mqttBegin(target", body)
        self.assertIn("gCfg.broker", body,
                      "the resolved address has to start from the stored one")
        begin_at = body.index("net::mqttBegin(target")
        creds_at = body.index("net::mqttSetCredentials(gCfg.mqttUser, gCfg.mqttPass)")
        self.assertLess(begin_at, creds_at,
                        "mqttSetCredentials must follow mqttBegin in startMqtt()")

    def test_on_provisioned_applies_the_broker_fields(self):
        """The portal path must copy all three fields (empty = keep, like the Wi-Fi
        password) and restart the client, or a portal save never takes effect."""
        body = _function_body(MAIN_CPP, "static const char* onProvisioned")
        for field in ("r.broker", "r.mqttUser", "r.mqttPass"):
            self.assertIn("if (%s[0])" % field, body,
                          "onProvisioned no longer copies %s" % field)
        self.assertIn("startMqtt();", body,
                      "onProvisioned must restart the MQTT client after saving")

    def test_mqtt_begin_keeps_credentials(self):
        """mqttBegin() used to blank gUser/gPass, which turned a broker address change into
        a silent anonymous reconnect. The reset must stay gone."""
        body = _function_body(MQTT_CLIENT_CPP, "void mqttBegin(")
        self.assertNotIn("gUser[0] = 0", body,
                         "mqttBegin resets credentials again - see the comment in that "
                         "function before re-adding it")
        self.assertNotIn("gPass[0] = 0", body,
                         "mqttBegin resets the password again")


if __name__ == "__main__":
    unittest.main()
