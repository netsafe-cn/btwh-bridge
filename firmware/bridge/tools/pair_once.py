# -*- coding: utf-8 -*-
"""pair_once.py - one command: code -> connect -> SMP -> login -> A0

The pairing window lasts tens of seconds, and opening it, finding the box and typing the
code by hand is easy to miss. This collapses the whole flow into one command.

Usage:
    python pair_once.py 311522              # watch for 30 s by default
    python pair_once.py 311522 --wait 45    # wait longer on a slow device
    python pair_once.py 311522 --reboot     # reboot the board first (skipped by default)
    python pair_once.py 311522 --port COM3

Exit code: 0 = all three criteria met; 2 = not working (reason in the output)
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
LOGDIR = HERE.parent / "logs"

sys.path.insert(0, str(HERE))
try:
    from log_diag import diagnose
except Exception:                       # a missing diagnostic helper must not break the flow
    diagnose = None


def pick_port() -> str | None:
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None
    ports = list(lp.comports())
    for p in ports:
        if (p.vid, p.pid) in ((0x1A86, 0x55D3), (0x1A86, 0x7523)) or "CH343" in (p.description or ""):
            return p.device
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="six-digit pairing code shown on the heater panel")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to observe (default 30)")
    ap.add_argument("--port", default=None, help="serial port; defaults to the CH343 one")
    ap.add_argument("--reboot", action="store_true", help="reboot the board before sending")
    a = ap.parse_args()

    code = (a.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        print("pairing code must be six digits, got: %r" % a.code)
        return 1

    import serial
    port = a.port or pick_port()
    if not port:
        print("no CH343 serial port; check the board is plugged in, or pass --port")
        return 1

    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print("cannot open %s: %s" % (port, e))
        print("-> if the panel or gateway_cli is running, close it (one process per port)")
        return 1

    LOGDIR.mkdir(parents=True, exist_ok=True)
    logpath = LOGDIR / ("pair_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    logf = open(logpath, "w", encoding="utf-8", errors="replace", buffering=1)

    def emit(line: str):
        print(line, flush=True)
        logf.write(line + "\n")

    try:
        ser.setDTR(False)
        ser.setRTS(False)
        time.sleep(0.3)
        ser.reset_input_buffer()

        if a.reboot:
            emit("[--] rebooting the board...")
            ser.write(b"reboot\n")
            ser.flush()
            time.sleep(2.0)
            ser.reset_input_buffer()

        emit("[--] %s @115200, sending panel code %s" % (port, code))
        ser.write((code + "\n").encode("ascii"))
        ser.flush()

        t0 = time.time()
        while time.time() - t0 < a.wait:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                emit("[%5.1fs] %s" % (time.time() - t0, line))
    finally:
        try:
            ser.close()
        except Exception:
            pass
        logf.close()

    text = logpath.read_text(encoding="utf-8", errors="replace")
    print()
    print("=" * 68)
    if diagnose:
        stages = diagnose(text)
        for stage, verdict, advice in stages:
            print("[%-12s] %s" % (stage, verdict))
            if advice != "nothing to do":
                print("               → %s" % advice)
        ok = (any("+ link encrypted" in v for _, v, _ in stages)
              and any("+ reply 01" in v for _, v, _ in stages)
              and any("state frames received" in v for _, v, _ in stages))
    else:
        ok = "A2/08 reply = 01" in text and "authentication complete" in text
    print("=" * 68)
    print("log: %s" % logpath)
    print("verdict: %s" % ("+ passed, all three criteria met" if ok
                       else "x failed; follow the first x above"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
