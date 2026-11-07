# -*- coding: utf-8 -*-
"""bosch_api.py - Bosch gas water heater BLE client library

No UI dependency: reusable from a GUI, a command-line tool or a cloud bridge.

Layers
    BoschProtocol   pure codec (no I/O, unit-testable)
    BoschDevice     one asynchronous session (connect / login / send / receive / close)
    BoschService    resident service (worker thread + auto-reconnect + state cache + queue)

Typical use
    from bosch_api import Config, BoschService

    cfg = Config.load()
    svc = BoschService(cfg)
    svc.start()

    svc.state                     # DeviceState | None
    svc.status                    # connection status text
    svc.login_state               # login status text
    svc.set_temperature(42)       # queued, executed asynchronously
    svc.take_logs()               # drain the log queue (list)

    svc.stop()                    # graceful shutdown, disconnects Bluetooth first

Threading model
    BoschService runs asyncio on one daemon thread; everything it exposes is thread-safe
    and synchronous. state / status are replaced wholesale, so readers need no lock.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional

from bleak import BleakClient

from boschlog import logger as LOG

# ------------------------------------------------------------------ protocol constants
SERVICE_BASE = "ff4396a6-50a8-4fc0-bcf6-228e82dfed"
CHAR_MODE = SERVICE_BASE + "b1"        # read/write, factory value ff - leave it alone
CHAR_FIFO = SERVICE_BASE + "b2"        # the main send/receive channel
CHAR_CREDITS = SERVICE_BASE + "b3"     # subscribe only, no need to write
CHAR_COUNTER = SERVICE_BASE + "d1"     # uptime in seconds

XOR_KEY = b"1ag3.-fs7"                 # 9 bytes
FRAME_START = b"\x7b\x7b"
FRAME_END = b"\x7d\x7d"

SENDER = 0x21                          # always the central
RECV_BIZ = 0x00                        # business frame address
RECV_LOGIN = 0xAB                      # login / configuration frame address

CMD_STATE = 0xA0                       # device-initiated state broadcast
CMD_BCAST_CFG = 0xA1                   # set broadcast interval [type, sec]
CMD_LOGIN = 0xA2                       # 07 = submit MAC, 08 = submit pairing code
CMD_READ_PARAM = 0xD2
CMD_WRITE_PARAM = 0xD3
CMD_HISTORY = 0xB0                     # history: data=[type] 2=run stats 3=bathing
CMD_READ_ERROR = 0xD7                  # fault list: data=[count]
PARAM_TYPE_SETPOINT = 0x02
PARAM_ID_SETPOINT = 0x55

CHUNK = 20                             # max payload per packet (MTU 23)
TEMP_MIN, TEMP_MAX = 35, 60
IDLE_TIMEOUT = 25.0                    # no data for this long means the link is dead
RECONNECT_BASE = 8.0                   # first wait after a failed session (then doubles)
RECONNECT_MAX = 60.0                   # reconnect wait ceiling
SESSION_STABLE_SEC = 15.0              # a session must last this long to count as stable
SESSION_MIN_FRAMES = 2                 # A0 frames needed before a session counts as useful

# ------------------------------------------------------ pairing/login failure reasons
# Confirmed on ESP32 hardware, 2026-11-07: the A2/07 reply byte reports the PAIRING WINDOW,
# not the MAC state - 02 = open, code required; 01 = passwordless; 00 = closed or expired.
# Treating 00 as "MAC not authorised" sends users off to edit login_mac, when what they
# actually need is a fresh code from the panel. Prefer the reason code over display text.
PAIR_FAIL_WINDOW_CLOSED = "window_closed"    # A2/07 = 00
PAIR_FAIL_CODE_REJECTED = "code_rejected"    # A2/08 = 00 (window open, code invalid)
PAIR_FAIL_CODE_MISMATCH = "code_mismatch"    # A2/08 = 02
PAIR_FAIL_NO_RESPONSE = "no_response"        # 6 s x 5 resends exhausted
PAIR_FAIL_COOLDOWN = "cooldown"              # A2/07 = 3C / 5A, device rate limit
PAIR_FAIL_LINK = "link_failed"               # link never came up

PAIR_HINTS = {
    PAIR_FAIL_WINDOW_CLOSED:
        "Window closed or expired: panel 'clear pairing' -> 'connect/pair' -> enter code",
    PAIR_FAIL_CODE_REJECTED:
        "Code expired: read a new one and enter it within 30 s, staying on that screen",
    PAIR_FAIL_CODE_MISMATCH:
        "Code mismatch: check it is the six digits on the panel, not the nameplate S/N",
    PAIR_FAIL_NO_RESPONSE:
        "No response: stay on the pairing screen; power-cycle the heater if necessary",
    PAIR_FAIL_COOLDOWN:
        "Device rate limit: wait 30-90 s, do not retry back to back",
    PAIR_FAIL_LINK:
        "Link not established: move closer, tap the panel to wake it, close the phone app",
}


# ------------------------------------------------------------------ OS-level pairing
# Background (learned the hard way, 2026-11-01): after removing the pairing in Windows, the
# device still holds the old link key for this address. Connecting in plaintext then makes it
# silently drop every business frame - link up, GATT reads work, login gets nothing at all.
# Keep OS-level pairing. The device uses Just Works (CONFIRM_ONLY, no PIN), so we automate it.

try:                                   # WinRT exists on Windows only
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import (
        DevicePairingKinds, DevicePairingResultStatus,
        DeviceUnpairingResultStatus,
    )
    _WINRT_OK = True
except Exception:
    _WINRT_OK = False


def parse_mac(address: str) -> int:
    """'AA:BB:CC:DD:EE:FF' -> int。"""
    return int(address.replace(":", "").replace("-", ""), 16)


async def unpair_device(address: str) -> bool:
    """Remove the OS-level pairing between this host and the device (self-heal, step one).

    Situation: "clear pairing" on the device, or its pairing menu timing out, invalidates the
    link key while Windows still reports paired=True, so the stale encrypted link is rejected
    at the GATT layer. Delete the host pairing first, then let ensure_paired rebuild it.
    """
    if not _WINRT_OK:
        return False
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:
            return False
        p = dev.device_information.pairing
        if not p.is_paired:
            return True
        res = await p.unpair_async()
        ok = int(res.status) == int(DeviceUnpairingResultStatus.UNPAIRED)
        LOG.info("[PAIR] unpair %s", "ok" if ok else "failed(status=%s)" % res.status)
        return ok
    except Exception as e:
        LOG.warning("[PAIR] unpair raised: %s: %s", type(e).__name__, e)
        return False


async def paired_state(address: str) -> str:
    """Return 'paired' / 'unpaired' / 'unknown' (unreachable device, or not Windows)."""
    if not _WINRT_OK:
        return "unknown"
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:
            return "unknown"
        return "paired" if dev.device_information.pairing.is_paired else "unpaired"
    except Exception:
        return "unknown"


async def ensure_paired(address: str, log: Optional[Callable[[str], None]] = None
                        ) -> bool:
    """Ensure OS-level pairing exists; bond via Just Works when it does not.

    Returns True when already paired or nothing was needed; False when pairing failed.
    Non-Windows platforms return True immediately (BlueZ handles it).
    """
    def say(msg):
        LOG.info("[PAIR] %s" % msg)
        if log:
            log(msg)

    if not _WINRT_OK:
        return True
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:                # not in the system cache (asleep); connect will cope
            return True
        p = dev.device_information.pairing
        if p.is_paired:
            return True
        if not p.can_pair:
            say("Device cannot pair right now (asleep or occupied)")
            return False

        custom = p.custom
        if custom is None:             # older interface, use the system default
            say("Not paired, starting the default system pairing...")
            res = await p.pair_async()
        else:
            say("Not paired, bonding via Just Works...")

            def on_req(_sender, args):
                # Measured: the device only requests CONFIRM_ONLY; reject anything else so
                # we never block waiting for a UI that nobody can show
                from winrt.windows.devices.enumeration import DevicePairingKinds
                if args.pairing_kind == DevicePairingKinds.CONFIRM_ONLY:
                    args.accept()

            custom.add_pairing_requested(on_req)
            res = await custom.pair_async(DevicePairingKinds.CONFIRM_ONLY)

        ok = int(res.status) == int(DevicePairingResultStatus.PAIRED)
        if ok:
            say("Automatic pairing succeeded")
        else:
            # Measured: outside its pairing window the device can fail Just Works, so the
            # user has to run "clear pairing" on the panel to open the window
            say("Automatic pairing failed(status=%s): run 'clear pairing' on the heater "
                "panel, then reconnect" % res.status)
        return ok
    except Exception as e:
        say("Pairing check raised: %s: %s" % (type(e).__name__, e))
        return False

CONFIG_FILE = pathlib.Path(__file__).with_name("config.json")


# ---------------------------------------------------------------------- config
@dataclass
class Config:
    """Runtime configuration. **No sensitive value lives in the code**; it is read from

    config.json or from environment variables, which win: BOSCH_ + FIELD_NAME_UPPER.
        BOSCH_DEVICE_ADDRESS / BOSCH_LOGIN_MAC / BOSCH_PAIR_CODE / BOSCH_BEMFA_UID
    Handy for containers, CI, or whenever the pairing code must not land on disk.
    """

    device_address: str = ""           # heater Bluetooth address, required
    login_mac: str = ""                # this host's Bluetooth address, required
    pair_code: str = ""                # panel pairing code (temporary), required
    bemfa_uid: str = ""                # Bemfa key, optional (empty = no Mi Home sync)
    topic_set: str = "boschset004"
    topic_up: str = "boschup006"
    topic_down: str = "boschdown006"

    ENV_PREFIX = "BOSCH_"

    @classmethod
    def load(cls, path: Optional[pathlib.Path] = None) -> "Config":
        path = path or CONFIG_FILE
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        for name in cfg.__dataclass_fields__:
            env = os.environ.get(cls.ENV_PREFIX + name.upper())
            if env:
                setattr(cfg, name, env)
        return cfg

    def save(self, path: Optional[pathlib.Path] = None):
        path = path or CONFIG_FILE
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                        "utf-8")

    def validate(self) -> List[str]:
        """Return a list of configuration problems; an empty list means usable."""
        errs = []
        if not (self.device_address or "").strip():
            errs.append("heater Bluetooth address is not configured")
        if not (self.pair_code or "").strip():
            errs.append("pairing code is not configured")
        try:
            self.login_mac_bytes
        except ValueError:
            errs.append("host Bluetooth address is invalid (expected 12 hex digits)")
        return errs

    @property
    def login_mac_bytes(self) -> bytes:
        s = (self.login_mac or "").replace(":", "").replace("-", "").strip()
        if len(s) != 12:
            raise ValueError("host Bluetooth address must be 12 hex digits")
        return bytes.fromhex(s)


# ------------------------------------------------------------------ device state
@dataclass
class DeviceState:
    """Device state (field semantics measured 2026-11; see BLE_PROTOCOL.md §6).

    The 0xA0 broadcast is Type0 or Type1, distinguished by data[0]; some offsets differ.
    """

    frame_type: int = 0                 # 0=Type0, 1=Type1
    temp_in: Optional[float] = None     # inlet temperature data[1]+data[2]/100
    chamber: Optional[float] = None     # combustion chamber data[3]+data[4]/100
    setpoint: Optional[int] = None      # current setpoint data[5]
    temp_box: Optional[float] = None    # chassis temperature data[6]+data[7]/100
    water_flow: Optional[float] = None  # flow, L/min data[8]/10
    burner_power: int = 0               # burner power % data[9]
    ionized: bool = False               # ionisation (flame) detect data[10] & 0x10
    failure: int = 0                    # active fault code data[11], 0 = none
    fan_power: Optional[int] = None     # fan power (T0:[13] T1:[12])
    fan_speed: Optional[int] = None     # fan speed (T0:[14] T1:[13])
    mode: Optional[int] = None          # 0 idle, 1 burning, 2 comfort (T0:[16] T1:[32])
    temp_out: Optional[float] = None    # outlet temp (T0:[21]+[22] T1:[28]+[29])
    bathroom: bool = False              # bathroom controller present (T0/T1 [33]&0x80)
    max_setpoint: Optional[int] = None  # highest settable temperature
    air_flow: Optional[float] = None    # air flow (Type1 only)
    air_setpoint: Optional[float] = None
    gas_setpoint: Optional[float] = None
    pressure: Optional[int] = None      # gas pressure (Type1 only, data[27]*10)

    raw: bytes = b""
    ts: float = 0.0

    # ---- legacy field names (the UI and external callers still use them)
    @property
    def outlet(self) -> Optional[float]:
        return self.temp_out

    @property
    def inlet(self) -> Optional[float]:
        return self.temp_in

    @property
    def exchanger(self) -> Optional[float]:
        return self.chamber

    @property
    def power(self) -> int:
        return self.burner_power

    @property
    def burning(self) -> bool:
        return self.burner_power > 0

    @property
    def mode_text(self) -> str:
        return {0: "idle", 1: "burning", 2: "comfort"}.get(self.mode, "unknown")

    @property
    def failure_text(self) -> str:
        return "none" if not self.failure else "%02X" % self.failure


# ---------------------------------------------------------------------- protocol
class BoschProtocol:
    """Pure codec, no I/O. Every method is static or class level, so it tests alone."""

    # ---- encryption
    @staticmethod
    def xor(data: bytes) -> bytes:
        return bytes(v ^ XOR_KEY[i % 9] for i, v in enumerate(data))

    @staticmethod
    def encode_chunks(plain: bytes, chunk: int = CHUNK) -> List[bytes]:
        """Split into packets and encrypt fragment by fragment.

        Note: the device requires **each notify fragment to restart the XOR at KEY[0]**,
        not one continuous XOR over the frame, so splitting is our job, not the stack's.
        """
        out = []
        for i in range(0, len(plain), chunk):
            out.append(BoschProtocol.xor(plain[i:i + chunk]))
        return out

    # ---- framing
    @staticmethod
    def build(sender: int, receiver: int, mid: int, cmd: int,
              data: bytes = b"") -> bytes:
        """Build a **logical frame** (checksum included, stuffing not yet applied).

        Before transmission it still goes through stuff() and then encode_chunks().
        Keeping wire bytes separate from the logical frame is what makes both ends match.
        """
        body = bytes([sender, receiver, mid & 0xFF, cmd]) + data
        return FRAME_START + body + bytes([sum(body) & 0xFF]) + FRAME_END

    @staticmethod
    def stuff(frame: bytes) -> bytes:
        """Apply stuffing before transmission: 00 after 7B/7D in the data area only."""
        if len(frame) <= 4:
            return frame
        head, body, tail = frame[:2], frame[2:-2], frame[-2:]
        out = bytearray(head)
        for b in body:
            out.append(b)
            if b in (0x7b, 0x7d):
                out.append(0x00)
        return bytes(out) + tail

    # ---- deframing
    @staticmethod
    def decrypt_chunk(data: bytes) -> bytes:
        """Decrypt one notify fragment. Each fragment restarts at KEY[0]."""
        return BoschProtocol.xor(bytes(data))

    @staticmethod
    def unstuff(frame: bytes) -> bytes:
        """Strip the 00 padding inserted after 7B/7D (frame delimiters excluded)."""
        if len(frame) <= 2:
            return frame
        head, body, tail = frame[:2], frame[2:-2], frame[-2:]
        out = bytearray(head)
        skip = False
        for b in body:
            if skip and b == 0x00:
                skip = False
                continue
            out.append(b)
            skip = b in (0x7b, 0x7d)
        return bytes(out) + tail

    @staticmethod
    def decode_stream(plain: bytes):
        """Cut complete frames out of the **already decrypted** plaintext stream.

        Returns (complete frames, bytes buffered from a partial frame).
        Call it as: decrypt_chunk each fragment -> concatenate -> feed it here.
        """
        frames, buf = [], plain
        while True:
            if not buf.startswith(FRAME_START):
                i = buf.find(FRAME_START)
                if i < 0:
                    return frames, b""
                buf = buf[i:]
            i = buf.find(FRAME_END, 2)
            if i < 0:
                return frames, buf
            frames.append(BoschProtocol.unstuff(buf[:i + 2]))
            buf = buf[i + 2:]

    # ---- field parsing
    @staticmethod
    def parse_state(payload: bytes) -> DeviceState:
        """Parse the 0xA0 broadcast (offsets measured 2026-11-01; Type0 and Type1)."""
        n = len(payload)

        def b(i):
            return payload[i] if n > i else None

        def temp(i):
            if n > i + 1:
                return payload[i] + payload[i + 1] / 100.0
            return None

        ftype = b(0) or 0
        t1 = (ftype == 1)

        # Temperatures: identical offsets in both types (verified on hardware)
        # Fan power / mode / outlet / bathroom: offsets differ between the two types
        mode_i = 32 if t1 else 16
        bath_i = 33 if t1 else 17
        out_i = 28 if t1 else 21

        bath_raw = b(bath_i) or 0
        maxsp = (bath_raw & 0x7F) if n > bath_i else None
        ion_raw = b(10) or 0

        # Flow / power / fan: early reference values did not match this firmware. The
        # numbers below come from the 2026-11-01 load comparison (13:41 running, 13:42 off):
        #   [21] 75-111 -> 2-3 when off    = water flow x 0.1 L/min
        #   [13] 36-47  -> 15-16 when off  = burner power % (low flame held after the tap)
        #   [15][16] 15/11 -> 7/5          = fan speed ([15]*100 + [16] rpm)
        # Verified on Type1 only; Type0 keeps its own set of offsets.
        if t1:
            water_flow = (b(21) / 10.0) if n > 21 else None
            burner = b(13) or 0
            fan_speed = ((b(15) or 0) * 100 + (b(16) or 0)) if n > 16 else None
            fan_power = b(12)
        else:
            water_flow = (b(8) / 10.0) if n > 8 else None
            burner = b(9) or 0
            fan_speed = b(14)
            fan_power = b(13)

        return DeviceState(
            frame_type=ftype,
            temp_in=temp(1),
            chamber=temp(3),
            setpoint=b(5),
            temp_box=temp(6),
            water_flow=water_flow,
            burner_power=burner,
            ionized=bool(ion_raw & 0x10),
            failure=b(11) or 0,
            fan_power=fan_power,
            fan_speed=fan_speed,
            mode=b(mode_i),
            temp_out=temp(out_i),
            bathroom=bool(bath_raw & 0x80),
            max_setpoint=maxsp,
            air_flow=temp(16) if t1 else None,
            air_setpoint=temp(18) if t1 else None,
            gas_setpoint=temp(22) if t1 else None,
            pressure=(b(27) * 10) if (t1 and n > 27) else None,
            raw=bytes(payload),
            ts=time.time(),
        )

    # ---- pairing-code encoding (one byte per digit)
    @staticmethod
    def encode_pair_code(code: str) -> bytes:
        return bytes(int(c) for c in code if c.isdigit())

    # ---- reply parsing (history / faults; format measured 2026-11-01)
    @staticmethod
    def parse_history(payload: bytes) -> dict:
        """B0 reply: [type][4-byte big-endian A][4-byte big-endian B].

        Measured with type=2: 02 | 00 00 12 67 | 00 00 05 b6 -> 4711 / 1462.
        Counts are big-endian; /60 gives hours. A = run minutes, B = ignition count.
        """
        if len(payload) < 9:
            return {"type": payload[0] if payload else None,
                    "a": None, "b": None, "raw": payload.hex(" ")}
        return {
            "type": payload[0],
            "a": int.from_bytes(payload[1:5], "big"),
            "b": int.from_bytes(payload[5:9], "big"),
            "raw": payload.hex(" "),
        }

    @staticmethod
    def parse_errors(payload: bytes) -> List[int]:
        """D7 reply: [status byte][fault codes ...].

        Measured (2026-11-01): asking for 1, 5 or 10 entries returns exactly that many
        bytes, and the first is always 0xEA - a status marker, **not a fault code** (the
        app showed no history). The rest are fault codes, 0x00 meaning an empty slot.
        """
        return [b for b in payload[1:] if b]


# ------------------------------------------------------------------ single session
class BoschDevice:
    """Full lifecycle of one connection. Driven by BoschService, but usable alone.

    Callbacks run on the asyncio thread; never block inside them.
    """

    def __init__(self, cfg: Config,
                 on_state: Optional[Callable[[DeviceState], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_login: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_state = on_state
        self.on_log = on_log
        self.on_login = on_login

        self.client: Optional[BleakClient] = None
        self.login_state = "not logged in"
        self.state: Optional[DeviceState] = None
        self.mid = 0
        self._buf = b""
        self._login_resp: Optional[bytes] = None
        self._login_ev = asyncio.Event()
        self.cooldown_sec: float = 0.0   # shortest reconnect interval the device demands
        self._pair_fail_reason: Optional[str] = None
        self.pair_code_ts: Optional[float] = None   # when the code was last submitted
        # ---- structured authentication result (do not parse login_state text)
        self.mac_registered = False        # device knows this MAC (A2/07 = 01/02)
        self.authorized = False            # application auth passed (A2/08 = 01)
        self.pair_kind = ""                # 'first_pair'(07=02) | 'rebind'(07=01) | ''
        self.pair_code_verified_at: Optional[float] = None
        self._last_rx = time.time()
        # single-flight reply routing (spec §3.5): mid -> (Event, payload)
        self._resp_ev: "dict[int, asyncio.Event]" = {}
        self._resp_map: "dict[int, Optional[bytes]]" = {}

    # ---- logging
    def log(self, msg: str):
        LOG.info(msg)
        if self.on_log:
            self.on_log(msg)

    def tx(self, mid, cmd, data: bytes, tag: str = ""):
        LOG.debug("[TX] mid=%3d cmd=%02X len=%2d %s %s",
                  mid, cmd, len(data), data.hex(" "), tag)

    def rx(self, mid, cmd, payload: bytes):
        LOG.debug("[RX] mid=%3d cmd=%02X len=%2d %s",
                  mid, cmd, len(payload), payload.hex(" "))

    def _set_login(self, s: str):
        self.login_state = s
        if self.on_login:
            self.on_login(s)

    @property
    def pair_fail_reason(self) -> Optional[str]:
        """Structured reason for the last pairing/login failure; None on success.

        Values are the module-level PAIR_FAIL_* constants. The UI, CLI and diagnostics
        should use this rather than matching login_state text - codes are the contract.
        """
        return self._pair_fail_reason

    def pair_fail_hint(self) -> str:
        """Turn the failure reason into one user-facing action; empty when there is none."""
        return PAIR_HINTS.get(self._pair_fail_reason or "", "")

    def pair_summary(self) -> dict:
        """Structured auth-stage summary for the UI / MQTT. Never includes the code itself."""
        return {
            "mac_registered": self.mac_registered,
            "authorized": self.authorized,
            "pair_kind": self.pair_kind,
            "fail_reason": self._pair_fail_reason,
            "fail_hint": self.pair_fail_hint(),
            "code_verified_at": self.pair_code_verified_at,
        }

    # ---- connection
    async def connect(self):
        """Match the connection sequence observed on hardware:
        no reads, no MTU request; subscribe FIFO CCCD -> 200 ms -> subscribe CREDITS CCCD.
        """
        self.client = BleakClient(self.cfg.device_address, timeout=25.0)
        await self.client.connect()
        LOG.info("[CONN] link established address=%s", self.cfg.device_address)
        await asyncio.sleep(0.6)
        # Do not pre-check services: Windows misreports "GATT not ready" before discovery
        # has finished. Subscribe directly and let bleak wait. Two retries on failure.
        for attempt in range(3):
            try:
                await self.client.start_notify(CHAR_FIFO, self._on_notify,
                                               force_indicate=False)
                break
            except Exception as e:
                if attempt == 2:
                    LOG.error("[CONN] FIFO subscribe failed (service enumeration pending): %s",
                              type(e).__name__)
                    raise RuntimeError("GATT not ready")
                LOG.info("[CONN] services not ready, retrying subscribe in 1 s (%d/3)", attempt + 1)
                await asyncio.sleep(1.0)
        await asyncio.sleep(0.2)          # app: postDelayed(200 ms) before CREDITS
        await self.client.start_notify(CHAR_CREDITS, self._on_notify,
                                       force_indicate=False)
        self._last_rx = time.time()

    async def close(self):
        if self.client is not None:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
                    LOG.info("[CONN] disconnected")
            except Exception:
                pass
            self.client = None

    # ---- login
    async def _send_login_wait(self, cmd_data: bytes, tag: str) -> Optional[bytes]:
        """Send a login frame: 6 s timeout, 5 resends, a fresh msgId each time (§3.5/§6.6).

        send() increments self.mid on every call, which provides that msgId for free.
        """
        for attempt in range(5):
            self._login_resp = None
            self._login_ev.clear()
            await self.send(CMD_LOGIN, cmd_data, RECV_LOGIN, tag)
            if await self._wait_login(6):
                LOG.info("[AUTH] %s reply: %s", tag,
                         (self._login_resp or b"").hex(" "))
                return self._login_resp
            LOG.warning("[AUTH] %s no reply, resend %d/5 (fresh msgId)",
                        tag, attempt + 1)
        return None

    async def login(self) -> bool:
        """Log in, then submit the pairing code. Both steps matter: skip the second and the

        Timing follows the hardware: sleep(1000) before submitting the MAC, and
        sendLoginPass sleeps(1000) at its start. A 00 rejection enters the password flow
        exactly like 02, because the device handles both the same way.
        """
        try:
            mac = self.cfg.login_mac_bytes
        except ValueError:
            self._set_login("invalid host Bluetooth address")
            return False
        code = (self.cfg.pair_code or "").strip()

        self._set_login("authenticating...")
        await asyncio.sleep(1.0)          # app: sleep(1000) before submitting the MAC
        self._pair_fail_reason = None      # clear the previous attempt before each login
        self.mac_registered = False
        self.authorized = False
        self.pair_kind = ""
        r = await self._send_login_wait(bytes([0x07]) + mac, "login")
        if r is None:
            self._pair_fail_reason = PAIR_FAIL_NO_RESPONSE
            self._set_login("no reply")
            return False

        kind = r[1] if len(r) > 1 else -1
        # Cooldown / wait: the device drops the link on both, so we disconnect and back off.
        if kind == 0x3C:
            self._pair_fail_reason = PAIR_FAIL_COOLDOWN
            self._set_login("device cooling down")
            self.cooldown_sec = 30.0
            return False
        if kind == 0x5A:
            self._pair_fail_reason = PAIR_FAIL_COOLDOWN
            self._set_login("device asks for a 90 s wait")
            self.cooldown_sec = 90.0
            return False
        if kind == 0x01:
            # Measured 2026-11-07: 01 means the device already has this MAC registered but
            # still wants A2/08. A 01 -> A2/08=00 failure was seen, so 01 is not a pass.
            self.mac_registered = True
            self.pair_kind = "rebind"
            self._set_login("MAC registered, pairing code still required")
        elif kind == 0x02:
            self.mac_registered = True
            self.pair_kind = "first_pair"
            self._set_login("device requires the pairing code (window open)")
        elif kind == 0x00:
            # Measured on ESP32-S3 hardware, 2026-11-07: 00 means the device is not in its
            # pairing window (clear pairing never ran, or it timed out) - NOT "MAC unauthed".
            # 仍补发 A2/08（设备侧同样处理），但原因码如实记 window_closed。
            self._pair_fail_reason = PAIR_FAIL_WINDOW_CLOSED
            self._set_login("配对窗口未打开或已超时（仍尝试提交码）")
        else:
            self._set_login("未知应答 %s" % r.hex(" "))
            return False

        if not code:
            self._set_login("需要配对码（未填写）")
            self.log("请在配置里填写配对码，否则设备不会推送数据")
            return False

        # 对齐 App 时序：提交配对码前固定 sleep(1000ms)
        await asyncio.sleep(1.0)
        self.pair_code_ts = time.time()
        r2 = await self._send_login_wait(
            bytes([0x08]) + BoschProtocol.encode_pair_code(code), "配对码")
        if r2 is None:
            if self._pair_fail_reason is None:
                self._pair_fail_reason = PAIR_FAIL_NO_RESPONSE
            self._set_login("配对码无应答")
            return False

        k2 = r2[1] if len(r2) > 1 else -1
        if k2 == 0x01:
            self._pair_fail_reason = None
            self.authorized = True
            self.pair_code_verified_at = time.time()
            self._set_login("已授权（配对码）")
            return True
        if k2 == 0x02:
            self._pair_fail_reason = PAIR_FAIL_CODE_MISMATCH
            self._set_login("配对码错误（与当前面板码不一致）")
        elif k2 == 0x00:
            # 实测：窗口已关或码已过期都会回 00。
            # 若 A2/07 已经是 00，说明窗口从头就没开，此处保持 window_closed——
            # 否则会把「没开窗口」误报成「码错」，用户去核对码是白折腾。
            if self._pair_fail_reason != PAIR_FAIL_WINDOW_CLOSED:
                self._pair_fail_reason = PAIR_FAIL_CODE_REJECTED
            self._set_login("配对码被拒绝（窗口已关或码已过期）")
        else:
            self._set_login("配对码应答异常 %s" % r2.hex(" "))
        return False

    async def _wait_login(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._login_ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- 收发
    async def send(self, cmd: int, data: bytes = b"",
                   receiver: int = RECV_BIZ, tag: str = ""):
        if self.client is None or not self.client.is_connected:
            raise RuntimeError("not connected")
        frame = BoschProtocol.build(SENDER, receiver, self.mid, cmd, data)
        self.mid = (self.mid + 1) & 0xFF
        wire = BoschProtocol.stuff(frame)
        self.tx(self.mid - 1, cmd, data, tag)
        for chunk in BoschProtocol.encode_chunks(wire):
            await self.client.write_gatt_char(CHAR_FIFO, chunk, response=True)
            await asyncio.sleep(0.02)
        if tag:
            self.log("%s → %s" % (tag, frame.hex(" ")))
        await asyncio.sleep(0.1)          # App 无此延迟；留小间隔防淹没

    def _on_notify(self, _sender, data: bytearray):
        self._last_rx = time.time()
        self._buf += BoschProtocol.decrypt_chunk(bytes(data))
        frames, self._buf = BoschProtocol.decode_stream(self._buf)
        for f in frames:
            if len(f) < 9:
                continue
            if sum(f[2:-3]) & 0xFF != f[-3]:      # CheckSum.check 等价
                LOG.warning("[RX] 丢弃坏帧 chk: %s", f.hex(" "))
                continue
            mid, cmd, payload = f[4], f[5], f[6:-3]
            self.rx(mid, cmd, payload)
            if cmd == CMD_STATE:
                st = BoschProtocol.parse_state(payload)
                self.state = st
                LOG.debug("[STATE] T%d 出%.1f 进%.1f 炉腔%.1f 机身%.1f 设定%s "
                          "功率%d%% 风机%s/%srpm 水流%.1f 模式%s 故障%s%s",
                          st.frame_type,
                          st.temp_out or 0, st.temp_in or 0, st.chamber or 0,
                          st.temp_box or 0, st.setpoint,
                          st.burner_power, st.fan_power, st.fan_speed,
                          st.water_flow or 0, st.mode_text, st.failure_text,
                          " 电离" if st.ionized else "")
                LOG.debug("[STATE] raw(%d)=%s", len(payload), payload.hex(" "))
                if self.on_state:
                    self.on_state(st)
            elif cmd == CMD_LOGIN:
                self._login_resp = payload
                self._login_ev.set()
                ev = self._resp_ev.get(mid)
                if ev is not None:
                    self._resp_map[mid] = payload
                    ev.set()
            else:
                # 应答路由：有等待者则投递（单飞机制，规格 §3.5）
                ev = self._resp_ev.get(mid)
                if ev is not None:
                    self._resp_map[mid] = payload
                    ev.set()
                else:
                    # D2/D8 等业务应答统一记下来，方便探测未知命令
                    self.log("RX cmd=%02X mid=%d data=%s"
                             % (cmd, mid, payload.hex(" ")))

    # ---- 高层操作
    async def send_and_wait(self, cmd: int, data: bytes = b"",
                            receiver: int = RECV_BIZ, tag: str = "",
                            timeout: float = 6.0, retries: int = 5) -> bytes:
        """单飞发送：发出后等应答；6s 超时 ×5 重发，重发换 msgId（规格 §3.5）。

        应答按 msgId 路由；若设备不回显 mid，则宽松匹配"唯一等待者"。
        重试耗尽抛 TimeoutError。
        """
        for attempt in range(retries):
            expect = self.mid                       # send() 将使用的 msgId
            ev = asyncio.Event()
            self._resp_ev[expect] = ev
            self._resp_map[expect] = None
            try:
                await self.send(cmd, data, receiver, tag)
                try:
                    await asyncio.wait_for(ev.wait(), timeout)
                    resp = self._resp_map.pop(expect, None)
                    if resp is not None:
                        return resp
                except asyncio.TimeoutError:
                    pass
            finally:
                self._resp_ev.pop(expect, None)
                self._resp_map.pop(expect, None)
            self.log("%s 无应答，重发 %d/%d（新 msgId）" % (tag, attempt + 1, retries))
        raise TimeoutError("%s 重试耗尽（%d 次 × %ds）" % (tag, retries, timeout))

    async def set_temperature(self, value: int):
        v = max(TEMP_MIN, min(TEMP_MAX, int(value)))
        try:
            resp = await self.send_and_wait(
                CMD_WRITE_PARAM,
                bytes([PARAM_TYPE_SETPOINT, PARAM_ID_SETPOINT, 0x00, v]),
                RECV_BIZ, "设置 %d℃" % v)
            LOG.info("[CMD] 设置 %d℃ 应答: %s", v, resp.hex(" "))
        except TimeoutError as e:
            LOG.warning("[CMD] 设置 %d℃ 未获确认: %s", v, e)
            raise

    async def idle_seconds(self) -> float:
        return time.time() - self._last_rx

    # ---- 只读查询（历史 / 故障）
    async def read_history(self, kind: int = 2) -> bytes:
        """读历史数据（B0）。kind: 2=运行统计(次数/小时) 3=洗浴记录(水量/时长)。"""
        return await self.send_and_wait(
            CMD_HISTORY, bytes([kind]), RECV_BIZ, "历史数据 type=%d" % kind)

    async def read_errors(self, count: int = 10) -> bytes:
        """读故障列表（D7）。应答为故障码序列。"""
        return await self.send_and_wait(
            CMD_READ_ERROR, bytes([count]), RECV_BIZ, "故障列表")


# ------------------------------------------------------------------ 常驻服务
class BoschService:
    """常驻服务：后台线程跑 asyncio，自动重连，对外提供同步接口。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_flag = False
        self._reload = False
        self._cmd_q: "queue.Queue[Optional[int]]" = queue.Queue()
        self._req_q: "queue.Queue[tuple]" = queue.Queue()   # 只读查询（历史/故障）
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._state_lock = threading.Lock()

        self.state: Optional[DeviceState] = None
        self.status = "not started"
        self.login_state = "not logged in"
        self.pair_fail_reason: Optional[str] = None
        self.connected = False
        self._rx_frames = 0            # 本次会话收到的 A0 帧数（判定会话有效性）
        # 查询结果：{"history": bytes|None, "history_ts":..., "errors": bytes|None,...}
        self.results: dict = {}

        # 可选的对外回调（在 worker 线程执行）
        self.on_state: Optional[Callable[[DeviceState], None]] = None

    # ---- 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="bosch-service")
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        """优雅停止：先让会话断开蓝牙，再退出线程。"""
        self._stop_flag = True
        self._cmd_q.put(None)
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 对外命令
    def set_temperature(self, value: int):
        self._cmd_q.put(value)

    def query_history(self, kind: int = 2):
        """请求历史数据，结果写入 self.results['history']。"""
        self._req_q.put(("history", kind))

    def query_errors(self, count: int = 10):
        """请求故障列表，结果写入 self.results['errors']。"""
        self._req_q.put(("errors", count))

    def take_logs(self) -> List[str]:
        out = []
        while True:
            try:
                out.append(self._log_q.get_nowait())
            except queue.Empty:
                return out

    def pair_fail_hint(self) -> str:
        """最近一次配对失败的动作建议；无失败或原因未知时返回空串。"""
        return PAIR_HINTS.get(self.pair_fail_reason or "", "")

    def update_config(self, cfg: Config):
        """换配置并立即重连。"""
        self.cfg = cfg
        self._reload = True

    def set_pair_code_and_reconnect(self, code: str, save: bool = True):
        """用面板新生成的动态配对码立即重连。

        配对码是热水器进入配对菜单时才生成的短时效码，所以这条路径
        追求快：更新配置 → 跳过退避等待 → 立即直连。
        """
        if not isinstance(code, str):
            raise ValueError("配对码应为 6 位数字")
        code = code.strip()
        if not code.isdigit() or len(code) != 6:
            raise ValueError("配对码应为 6 位数字")
        self.cfg.pair_code = code
        if save:
            self.cfg.save()
        self._log("收到新配对码，立即重连")
        self._reload = True

    # ---- 内部
    def _log(self, msg: str):
        LOG.info("[SVC] %s" % msg)
        self._log_q.put("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            self._log("服务退出: %s" % e)

    def _set_state(self, st: DeviceState):
        self.state = st
        self._rx_frames += 1
        if self.on_state:
            try:
                self.on_state(st)
            except Exception:
                pass

    async def _main(self):
        fails = 0
        gatt_fails = 0                     # 连续 GATT 未就绪次数（配对自愈触发器）
        reported = set()
        while not self._stop_flag:
            errs = self.cfg.validate()
            if errs:
                self.status = "配置不完整"
                for e in errs:
                    if e not in reported:      # 同一条只提示一次，避免刷屏
                        reported.add(e)
                        self._log("配置错误：" + e)
                end = time.time() + 2
                while time.time() < end and not self._stop_flag:
                    await asyncio.sleep(0.2)
                continue
            reported.clear()

            # 保持系统级配对（解绑后明文链路会被设备静默丢弃，见文件头注释）
            if not await ensure_paired(self.cfg.device_address, log=self._log):
                self.status = "配对失败：请到面板清除配对"
                end = time.time() + 20
                while time.time() < end and not self._stop_flag:
                    await asyncio.sleep(0.5)
                continue

            dev = BoschDevice(self.cfg, on_state=self._set_state,
                              on_log=self._log,
                              on_login=lambda s: setattr(self, "login_state", s))
            ok = False
            t_sess = time.time()
            self._rx_frames = 0
            self.status = "连接中…"
            try:
                await dev.connect()
                self.connected = True
                self.status = "已连接"
                exit_reason = "ok"
                if await dev.login():
                    self.pair_fail_reason = None
                    self._log("登录成功，开始接收数据")
                    # 丢弃断线期间积压的旧温度命令，防止在新会话里意外生效
                    while not self._cmd_q.empty():
                        try:
                            self._cmd_q.get_nowait()
                        except queue.Empty:
                            break
                    while dev.client and dev.client.is_connected:
                        if self._stop_flag:
                            exit_reason = "stop"
                            break
                        if await dev.idle_seconds() > IDLE_TIMEOUT:
                            self._log("超过 %d 秒无数据，链路已失效，重连"
                                      % int(IDLE_TIMEOUT))
                            exit_reason = "idle"
                            break
                        if self._reload:
                            self._reload = False
                            self._log("配置已更新，重新连接")
                            exit_reason = "reload"
                            break
                        try:
                            v = self._cmd_q.get_nowait()
                        except queue.Empty:
                            v = None
                        if v is not None:
                            try:
                                await dev.set_temperature(v)
                            except TimeoutError:
                                # 命令失败已记日志；不断会话（设备下次广播
                                # data[5] 会带回真实设定值，UI 自然纠正）
                                pass
                        # 只读查询（历史 / 故障）
                        try:
                            what, arg = self._req_q.get_nowait()
                        except queue.Empty:
                            what, arg = None, None
                        if what == "history":
                            try:
                                r = await dev.read_history(arg)
                                self.results["history"] = r
                                self.results["history_ts"] = time.time()
                                self._log("历史数据 type=%d: %s" % (arg, r.hex(" ")))
                            except TimeoutError:
                                self.results["history"] = None
                                self._log("历史数据读取超时")
                        elif what == "errors":
                            try:
                                r = await dev.read_errors(arg)
                                self.results["errors"] = r
                                self.results["errors_ts"] = time.time()
                                self._log("故障列表: %s" % r.hex(" "))
                            except TimeoutError:
                                self.results["errors"] = None
                                self._log("故障列表读取超时")
                        await asyncio.sleep(0.5)
                    ok = True
                else:
                    self.pair_fail_reason = dev.pair_fail_reason
                    self._log("登录未通过：%s" % dev.login_state)
                    if dev.pair_fail_hint():
                        self._log("下一步：" + dev.pair_fail_hint())
            except Exception as e:
                msg = str(e)
                if "GATT" in msg:
                    self.status = "设备忙，重试中"
                    gatt_fails += 1
                    if gatt_fails >= 3:
                        # 配对自愈：设备侧密钥已作废而 Windows 侧不知情，
                        # 带旧密钥的链路会被 GATT 层直接拒绝。删掉本机配对，
                        # 下一轮 ensure_paired 会重新绑定。
                        self._log("连续 %d 次 GATT 失败，删除旧配对并重新绑定"
                                  % gatt_fails)
                        await unpair_device(self.cfg.device_address)
                        gatt_fails = 0
                else:
                    gatt_fails = 0
                self._log("异常: %s: %s" % (type(e).__name__, msg))
            finally:
                self.connected = False
                await dev.close()

            if self._stop_flag:
                break
            # 关键：只有"会话真正稳定"才重置退避。
            # 之前只要 connect+login 成功就 fails=0，而设备常在登录后
            # 立刻踢人（限流），结果变成 3 秒一次的密集重连，越连越被拒。
            dur = time.time() - t_sess
            stable = (ok and dur >= SESSION_STABLE_SEC
                      and self._rx_frames >= SESSION_MIN_FRAMES)
            # 设备明确要求冷却（3C=30s / 5A=90s）时，覆盖退避间隔
            cd = getattr(dev, "cooldown_sec", 0.0) or 0.0
            dev.cooldown_sec = 0.0
            if stable:
                fails, wait = 0, 3
                # 会话曾稳定却被异常中断（踢线/断链）时，缓 30 秒再连，
                # 避免反复撞设备限流（规格 §7.10 精神）
                if exit_reason not in ("stop", "reload"):
                    wait = 30
                    self.status = "会话中断，30s 后重连"
                else:
                    self.status = "重连中…"
            else:
                fails += 1
                wait = min(RECONNECT_BASE * (2 ** (fails - 1)), RECONNECT_MAX)
                if cd > wait:
                    wait = cd
                if ok:
                    self.status = "会话不稳定，等待 %ds" % int(wait)
                elif fails >= 3:
                    self.status = "连不上，%ds 后重试" % int(wait)
                else:
                    self.status = "等待重连 %ds" % int(wait)
                if fails >= 3 and fails % 3 == 0:
                    self._log("连续 %d 次未能建立稳定会话，设备可能在限流，"
                              "建议断电重启热水器" % fails)
            end = time.time() + wait
            while (time.time() < end and not self._stop_flag
                   and not self._reload):
                await asyncio.sleep(0.2)

        self.status = "已停止"
        self.connected = False


def local_bt_mac() -> str:
    """读取本机蓝牙地址。失败返回空串。

    Windows 优先用 WinRT（最可靠），退回 PowerShell；Linux 读 sysfs。
    """
    # 1) Windows WinRT
    if sys.platform == "win32":
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter

            async def _get():
                a = await BluetoothAdapter.get_default_async()
                return a.bluetooth_address if a else 0

            addr = asyncio.run(_get())
            if addr:
                return "%012X" % addr
        except Exception:
            pass
        # 2) 退回 PowerShell
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetAdapter -Physical | Where-Object "
                 "{$_.InterfaceDescription -like '*Bluetooth*'}).MacAddress"],
                capture_output=True, text=True, timeout=12,
                creationflags=0x08000000)
            for line in r.stdout.splitlines():
                s = line.strip().replace("-", "").replace(":", "")
                if len(s) == 12 and all(c in "0123456789abcdefABCDEF" for c in s):
                    return s.upper()
        except Exception:
            pass

    # 3) Linux
    try:
        p = pathlib.Path("/sys/class/bluetooth/hci0/address")
        if p.exists():
            s = p.read_text().strip().replace(":", "").upper()
            if len(s) == 12:
                return s
    except Exception:
        pass
    return ""


__all__ = ["Config", "DeviceState", "BoschProtocol", "BoschDevice",
           "BoschService", "TEMP_MIN", "TEMP_MAX", "local_bt_mac",
           "ensure_paired", "paired_state", "parse_mac", "unpair_device"]
