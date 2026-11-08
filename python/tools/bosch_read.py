#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""bosch_read.py - Bosch Therm 4800i gas water heater BLE reader (verified working)

Required sequence, none of it optional:
  1. subscribe FIFO(b2) / CREDITS(b3) with CCCD = notify (01 00)
  2. log in with A2/07, carrying the 6-byte MAC
  3. submit the panel code with A2/08 (required: without it the device sends nothing)
  4. (optional) A1 sets the broadcast interval [type, seconds] - not actually needed
  -> the device then pushes A0 frames (43 bytes, split 20+20+3 at MTU 23)

Usage:
  python tools/bosch_read.py                  # one shot (30 s by default)
  python tools/bosch_read.py --loop           # keep reading, reconnect on drop
  python tools/bosch_read.py --code 123456    # panel code
  python tools/bosch_read.py --mac AABBCCDDEEFF   # host MAC to log in with
  python tools/bosch_read.py --json           # JSON output (for MQTT / Bemfa)
  python tools/bosch_read.py --no-pass        # skip the code (to prove it is needed)
  python tools/bosch_read.py --no-bcast       # skip the broadcast configuration
"""
import argparse, asyncio, json, sys, time
from bleak import BleakClient

# Everything comes from config.json or the environment; no address or code is hard-coded
try:
    from bosch_api import Config as _Config
    _CFG = _Config.load()
    DEV_ADDR = _CFG.device_address
    try:
        DEFAULT_MAC = _CFG.login_mac_bytes
    except ValueError:
        DEFAULT_MAC = b"\x00" * 6
    DEFAULT_CODE = _CFG.pair_code
except Exception:                      # standalone use without a config is allowed
    DEV_ADDR = ""
    DEFAULT_MAC = b"\x00" * 6
    DEFAULT_CODE = ""

U = "ff4396a6-50a8-4fc0-bcf6-228e82dfed"
FIFO, CREDITS, MODE = U + "b2", U + "b3", U + "b1"
KEY = b"1ag3.-fs7"
T0 = time.time()


def log(s):
    print("[%6.2fs] %s" % (time.time() - T0, s), flush=True)


# ---------- frame codec ----------
def xor(b: bytes) -> bytes:
    return bytes(v ^ KEY[i % 9] for i, v in enumerate(b))


def unstuff(d: bytes) -> bytes:
    """Remove the 0x00 padding inserted after 0x7B/0x7D"""
    if len(d) <= 2:
        return d
    head, body, tail = d[:2], d[2:-2], d[-2:]
    out = bytearray(head)
    skip = False
    for b in body:
        if skip and b == 0x00:
            skip = False
            continue
        out.append(b)
        skip = b in (0x7B, 0x7D)
    return bytes(out) + tail


def enc_pass(code: str) -> bytes:
    """Panel code -> A2/08 payload, one byte per digit: "123456" -> 01 02 03 04 05 06"""
    return bytes(int(c) for c in code)


def build(sender, receiver, mid, cmd, data=b""):
    body = bytes([sender, receiver, mid, cmd]) + data
    return b"\x7b\x7b" + body + bytes([sum(body) & 0xFF]) + b"\x7d\x7d"


# Encrypted frame header (each 0x7B XORed with the key)
FRAME_HEAD_ENC = bytes([0x7B ^ KEY[0], 0x7B ^ KEY[1]])


def decode_stream(buf: bytes):
    """Cut complete frames out of a plaintext stream already decrypted per chunk.

    Important: the device XORs **each notify fragment (<= 20 bytes) from KEY[0]** on its
    own, not the whole frame continuously. So the caller must xor() each fragment, then
    concatenate and hand the result here. Returns (frames, leftover bytes).
    """
    frames = []
    while True:
        if not buf.startswith(b"\x7b\x7b"):
            i = buf.find(b"\x7b\x7b")
            if i < 0:
                return frames, b""
            buf = buf[i:]
        i = buf.find(b"\x7d\x7d", 2)
        if i < 0:
            return frames, buf
        frames.append(unstuff(buf[:i + 2]))
        buf = buf[i + 2:]
    return frames, buf


def parse_a0(data: bytes) -> dict:
    """Decode an A0 broadcast (field indices measured 2026-11-01)"""
    if len(data) < 7:
        return {}
    t = data[0]
    d = {
        "type": t,
        "t_in": round(data[1] + data[2] / 100.0, 2),        # inlet temperature
        "t_alt": round(data[3] + data[4] / 100.0, 2),        # second temperature (TBD)
        "setpoint": data[5],                                  # setpoint temperature
    }
    # Outlet temperature: the offset depends on the frame type
    if t == 0 and len(data) > 22:
        d["t_out"] = round(data[21] + data[22] / 100.0, 2)
    elif t == 1 and len(data) > 29:
        d["t_out"] = round(data[28] + data[29] / 100.0, 2)
    d["raw"] = data.hex(" ")
    return d


# ---------- session ----------
class BoschReader:
    def __init__(self, mac=DEFAULT_MAC, code=DEFAULT_CODE, addr=DEV_ADDR,
                 use_pass=True, use_bcast=True, btype=1, bsec=1,
                 verbose=True):
        self.mac, self.code, self.addr = mac, code, addr
        self.use_pass, self.use_bcast = use_pass, use_bcast
        self.btype, self.bsec = btype, bsec
        self.verbose = verbose
        self.buf = b""
        self.frames = []
        self.latest = None
        self.mid = 0

    def say(self, s):
        if self.verbose:
            log(s)

    def on_notify(self, _s, data):
        """Reassemble notify fragments.

        At MTU 23 a fragment carries at most 20 bytes, and the device restarts the XOR at
        KEY[0] for each one, so xor each fragment first, then concatenate and deframe.
        """
        self.buf += xor(bytes(data))
        frames, self.buf = decode_stream(self.buf)
        for f in frames:
            self.handle(f)
        if len(self.buf) > 400:          # runaway guard
            self.buf = b""

    def handle(self, f: bytes):
        if len(f) < 9:
            return
        sender, receiver, mid, cmd = f[2], f[3], f[4], f[5]
        data, chk = f[6:-3], f[-3]
        ok = (sum(f[2:-3]) & 0xFF) == chk
        self.frames.append(f)
        if cmd == 0xA0:
            self.latest = parse_a0(data)
            self.latest["mid"] = mid
            if self.verbose:
                log("A0  %s" % json.dumps(self.latest, ensure_ascii=False))
            else:
                print(json.dumps(self.latest, ensure_ascii=False), flush=True)
        else:
            self.say("RX cmd=%02X mid=%d data=%s chk=%s"
                     % (cmd, mid, data.hex(" "), "OK" if ok else "BAD"))

    async def send(self, c, cmd, data, tag=""):
        f = build(0x21, 0xAB, self.mid, cmd, data)
        self.mid += 1
        await c.write_gatt_char(FIFO, xor(f), response=True)
        self.say(">> %-6s %s" % (tag, f.hex(" ")))
        await asyncio.sleep(1.0)

    async def run(self, dwell=30.0):
        c = BleakClient(self.addr, timeout=20.0)
        await c.connect()
        self.say("connected, MTU=%s" % getattr(c, "mtu_size", "?"))
        await asyncio.sleep(0.5)                 # wait for service discovery
        try:
            m = await c.read_gatt_char(MODE)
            self.say("MODE = %s" % bytes(m).hex(" "))
        except Exception as e:
            self.say("MODE read skipped (%s)" % type(e).__name__)
        await c.start_notify(FIFO, self.on_notify, force_indicate=False)
        await asyncio.sleep(0.25)
        await c.start_notify(CREDITS, self.on_notify, force_indicate=False)
        self.say("subscribed to FIFO/CREDITS")

        await self.send(c, 0xA2, bytes([0x07]) + self.mac, "LOGIN")
        if self.use_pass:
            await self.send(c, 0xA2, bytes([0x08]) + enc_pass(self.code), "PASS")
        if self.use_bcast:
            await self.send(c, 0xA1, bytes([self.btype, self.bsec]), "BCAST")

        self.say("waiting for data %.0fs ..." % dwell)
        t_end = time.time() + dwell
        while time.time() < t_end:
            if not c.is_connected:
                self.say("device disconnected")
                break
            await asyncio.sleep(1)
        try:
            await c.disconnect()
        except Exception:
            pass
        return self.latest


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", default=DEFAULT_MAC.hex().upper())
    ap.add_argument("--code", default=DEFAULT_CODE)
    ap.add_argument("--addr", default=DEV_ADDR)
    ap.add_argument("--dwell", type=float, default=30.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-pass", action="store_true")
    ap.add_argument("--bcast", action="store_true", help="also send A1 (not needed in practice)")
    ap.add_argument("--btype", type=int, default=1)
    ap.add_argument("--bsec", type=int, default=1)
    a = ap.parse_args()

    while True:
        r = BoschReader(bytes.fromhex(a.mac.replace(":", "")), a.code, a.addr,
                        use_pass=not a.no_pass, use_bcast=a.bcast,
                        btype=a.btype, bsec=a.bsec, verbose=not a.json)
        try:
            await r.run(a.dwell)
        except Exception as e:
            log("exception: %s %s" % (type(e).__name__, e))
        if not a.loop:
            break
        log("reconnecting in 10 s ...")
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
