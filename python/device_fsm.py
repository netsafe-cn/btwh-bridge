# -*- coding: utf-8 -*-
"""device_fsm.py - provisioning / pairing state machine for the ESP32 bridge

Framework-agnostic: it uses no Arduino, ESP-IDF, BLE or Wi-Fi API, so every branch runs in
CI on a PC. The C++ firmware implements the same states and transitions, and the Python
side stays structurally identical.

Core design decision
--------------------
"Provisioning" and "pairing" are **two orthogonal flows** and must never collapse into one
"pairing mode" switch:

  provisioning : the ESP32 obtains a Wi-Fi SSID / password  -> network side, one-off,
                 credentials persisted
  pairing      : the heater grants the ESP32 a BLE link      -> device side, repeatable,
                 short-lived 6-digit code

The cost of coupling is concrete: changing router would mean walking to the heater panel to
clear pairing again, and a heater drop would mean re-entering the Wi-Fi password. The two
must re-enter and recover independently.

State axes
----------
  WifiState : UNPROVISIONED -> PROVISIONING -> CONNECTING -> CONNECTED
              CONNECTING -> FAILED -> (backoff) -> CONNECTING
  PairState : IDLE -> WINDOW_WAIT -> PIN_READY -> BONDING -> LOGIN -> ONLINE
              any state -> FAILED -> IDLE (retry with a fresh code)

Composite mode exposed to users (DeviceMode):
  SETUP     no Wi-Fi credentials, or the user asked for provisioning
  NORMAL    provisioned, bridging BLE normally
  RECOVERY  Wi-Fi keeps failing; open an AP so a human can reconfigure, **without clearing
            the heater bond**

Physical constraints (implementations must honour these; they are not preferences)
  1. The pairing window opens only when a human picks "clear pairing" on the heater panel.
     Software cannot trigger it remotely.
  2. The 6-digit code is short-lived and different every time: enter it at runtime, never
     compile it into firmware, never treat it as a long-term credential.
  3. The panel code is simultaneously the BLE SMP passkey and the application-layer A2/08
     password (measured 2026-11-01).
  4. Wi-Fi credentials, heater parameters and the NimBLE bond live in separate NVS
     namespaces; clearing Wi-Fi must never delete the bond, or the user has to walk back to
     the panel.

Security
  snapshot() feeds MQTT / Home Assistant and **never emits the pin or the Wi-Fi password**.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class WifiState(str, Enum):
    UNPROVISIONED = "unprovisioned"
    PROVISIONING = "provisioning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class PairState(str, Enum):
    IDLE = "idle"
    WINDOW_WAIT = "window_wait"
    PIN_READY = "pin_ready"
    BONDING = "bonding"
    LOGIN = "login"
    ONLINE = "online"
    FAILED = "failed"


class DeviceMode(str, Enum):
    SETUP = "setup"
    NORMAL = "normal"
    RECOVERY = "recovery"


# NVS key-space contract: the C++ firmware and the Python tools must use the same names
NVS_LAYOUT = {
    "wifi": {"namespace": "bosch_wifi", "keys": ["ssid", "pass"]},
    "heater": {"namespace": "bosch_heater",
               "keys": ["addr", "login_mac", "pin"]},
    # managed by NimBLE itself; the provisioning flow must never delete it
    "bond": {"namespace": "nimble_bond", "keys": ["*"], "managed_by": "NimBLE"},
}


@dataclass
class FsmConfig:
    wifi_fail_limit: int = 3          # consecutive failures before entering RECOVERY
    pair_window_sec: float = 60.0     # panel pairing window; a new code is needed after it
    pin_len: int = 6


class BridgeFsm:
    """Pure state machine; time is injected through tick(now) so tests stay deterministic."""

    def __init__(self, cfg: Optional[FsmConfig] = None):
        self.cfg = cfg or FsmConfig()
        self.wifi = WifiState.UNPROVISIONED
        self.pair = PairState.IDLE
        self.mode = DeviceMode.SETUP
        self.ssid: Optional[str] = None
        self.has_bond = False
        self.has_pin = False
        self._pin: Optional[str] = None
        self.wifi_fail = 0
        self.pin_deadline: Optional[float] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ boot
    def on_boot(self, has_wifi: bool, has_bond: bool,
                has_pin: bool) -> DeviceMode:
        """Restore after power-up. Complete credentials go straight to NORMAL."""
        self.has_bond = has_bond
        self.has_pin = has_pin
        if has_wifi:
            self.wifi = WifiState.CONNECTING
            self.mode = DeviceMode.NORMAL
            # Already bonded with a stored code: skip SMP, go straight to application login
            self.pair = PairState.PIN_READY if (has_bond and has_pin) else PairState.IDLE
        else:
            self.wifi = WifiState.UNPROVISIONED
            self.mode = DeviceMode.SETUP
            self.pair = PairState.IDLE
        return self.mode

    # ------------------------------------------------------------ provisioning axis
    def request_setup(self) -> DeviceMode:
        """Long-press BOOT / serial command / web button: enter provisioning, pairing untouched."""
        self.mode = DeviceMode.SETUP
        self.wifi = WifiState.PROVISIONING
        self.wifi_fail = 0
        self.last_error = None
        return self.mode

    def provisioning_done(self, ssid: str) -> WifiState:
        if not ssid:
            raise ValueError("SSID must not be empty")
        self.ssid = ssid
        self.wifi = WifiState.CONNECTING
        self.wifi_fail = 0
        if self.mode == DeviceMode.SETUP:
            self.mode = DeviceMode.NORMAL
        return self.wifi

    def wifi_result(self, ok: bool) -> DeviceMode:
        if ok:
            self.wifi = WifiState.CONNECTED
            self.wifi_fail = 0
            self.last_error = None
            if self.mode == DeviceMode.RECOVERY:
                self.mode = DeviceMode.NORMAL
            return self.mode
        self.wifi = WifiState.FAILED
        self.wifi_fail += 1
        self.last_error = "Wi-Fi connection failed"
        if self.wifi_fail >= self.cfg.wifi_fail_limit:
            self.mode = DeviceMode.RECOVERY
            self.wifi = WifiState.PROVISIONING   # open an AP and wait for new credentials
        return self.mode

    def wifi_retry(self) -> WifiState:
        """Retry after backoff; RECOVERY does not exit here, it waits for new credentials."""
        if self.mode == DeviceMode.RECOVERY:
            return self.wifi
        self.wifi = WifiState.CONNECTING
        return self.wifi

    def reset_wifi_only(self) -> DeviceMode:
        """New router: clear Wi-Fi, keep the bond and pin (the point of the orthogonality)."""
        self.ssid = None
        self.wifi_fail = 0
        self.wifi = WifiState.PROVISIONING
        self.mode = DeviceMode.SETUP
        # deliberately untouched: self.pair / self.has_bond / self._pin
        return self.mode

    # ------------------------------------------------------------ pairing axis
    def request_pairing(self, now: float) -> PairState:
        """The user pressed "start pairing"; we wait for them to clear pairing and read a code."""
        self.pair = PairState.WINDOW_WAIT
        self.pin_deadline = now + self.cfg.pair_window_sec
        self.last_error = None
        return self.pair

    def submit_pin(self, code: str, now: float) -> PairState:
        """Enter the six digits: connect over BLE and start SMP (the code is the passkey)."""
        if not isinstance(code, str):
            raise ValueError("pairing code must be %d digits" % self.cfg.pin_len)
        code = code.strip()
        if not code.isdigit() or len(code) != self.cfg.pin_len:
            raise ValueError("pairing code must be %d digits" % self.cfg.pin_len)
        if self.pin_deadline is not None and now > self.pin_deadline:
            self.pair = PairState.FAILED
            self.last_error = "pairing window expired; read a new code from the panel"
            return self.pair
        self._pin = code
        self.has_pin = True
        self.pair = PairState.BONDING
        return self.pair

    def smp_result(self, encrypted: bool) -> PairState:
        """Callback for the completion of SMP authentication."""
        if encrypted:
            self.has_bond = True
            self.pair = PairState.LOGIN
            self.last_error = None
        else:
            self.pair = PairState.FAILED
            self.last_error = ("SMP authentication failed: wrong code, window closed, "
                               "or the phone app holds the device")
        return self.pair

    def login_result(self, ok: bool) -> PairState:
        """Outcome of A2/07 + A2/08; on success the A0 broadcast starts arriving."""
        if ok:
            self.pair = PairState.ONLINE
            self.pin_deadline = None
            self.last_error = None
        else:
            self.pair = PairState.FAILED
            self.last_error = "application login failed: A2/08 did not answer 01"
        return self.pair

    def pair_retry(self) -> PairState:
        """Back to IDLE so a new code can be tried; Wi-Fi and the bond are left alone."""
        self.pair = PairState.IDLE
        self._pin = None
        self.has_pin = False
        self.pin_deadline = None
        return self.pair

    def tick(self, now: float) -> PairState:
        """Check the pairing window. Once ONLINE the window no longer applies."""
        if (self.pair in (PairState.WINDOW_WAIT, PairState.PIN_READY,
                          PairState.BONDING, PairState.LOGIN)
                and self.pin_deadline is not None
                and now > self.pin_deadline):
            self.pair = PairState.FAILED
            self.last_error = "pairing window expired; read a new code from the panel"
        return self.pair

    # ------------------------------------------------------------ full reset
    def factory_reset(self) -> DeviceMode:
        """Factory reset: drop Wi-Fi, bond and pin, back to SETUP."""
        self.ssid = None
        self._pin = None
        self.has_pin = False
        self.has_bond = False
        self.wifi_fail = 0
        self.pin_deadline = None
        self.wifi = WifiState.UNPROVISIONED
        self.pair = PairState.IDLE
        self.mode = DeviceMode.SETUP
        self.last_error = None
        return self.mode

    # ------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        """Structured state for MQTT / HA. Deliberately free of the pin and Wi-Fi password."""
        return {
            "mode": self.mode.value,
            "wifi": self.wifi.value,
            "pair": self.pair.value,
            "ssid": self.ssid,
            "has_bond": self.has_bond,
            "has_pin": self.has_pin,      # boolean only, never the value
            "wifi_fail": self.wifi_fail,
            "error": self.last_error,
        }

    def config_snapshot(self) -> dict:
        return asdict(self.cfg)


if __name__ == "__main__":
    f = BridgeFsm()
    f.on_boot(has_wifi=False, has_bond=False, has_pin=False)
    print(f.snapshot())
