# -*- coding: utf-8 -*-
"""log_diag.py - serial log diagnosis for the ESP32 gateway

Turns the failure matrix in esp32/README.md into executable rules. When the connection
will not come up, run it: it names the layer, the likely cause and the next step.

Usage:
    python log_diag.py                     # newest logs/panel_*.log next to it
    python log_diag.py <logfile>           # a specific file

Criteria match esp32/README.md section 4:
    [SMP] authentication complete: link encrypted
    [AUTH] A2/07 answered, and A2/08 reply = 01
    [A0] frames arriving with sane fields
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
LOGDIR = HERE.parent / "logs"


def find_latest() -> pathlib.Path | None:
    files = sorted(LOGDIR.glob("panel_*.log"))
    files += sorted(LOGDIR.glob("esp32_*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def diagnose(text: str) -> list[tuple[str, str, str]]:
    """Return [(stage, verdict, advice)] in link order, one layer at a time."""
    out = []

    # ---- layer 1: BLE link and SMP
    smp_ok = bool(re.search(r"authentication complete.*link encrypted", text))
    pin_asked = "asks for the PIN" in text or "PROVIDE_PIN" in text
    smp_fail = bool(re.search(r"authentication complete.*not encrypted|SMP.*fail", text))
    connected = bool(re.search(r"\[CONN\]|link established|Connected", text, re.I))

    if smp_ok:
        out.append(("SMP", "+ link encrypted, bonded", "nothing to do"))
    elif pin_asked or smp_fail:
        out.append(("SMP", "x failed at the PIN stage",
                    "wrong code / window closed / phone app holding the link; "
                    "clear pairing on the panel, read a new code, type it again"))
    else:
        out.append(("SMP", "x never reached the PIN stage%s" %
                    (" (no link at all)" if not connected else ""),
                    "check the panel is awake, phone Bluetooth off, distance; use scan"))

    # ---- layer 2: application login
    a2_07 = re.findall(r"A2/07\s*reply\s*=\s*([0-9A-Fa-f]{2})", text)
    a2_08 = re.findall(r"A2/08\s*reply\s*=\s*([0-9A-Fa-f]{2})", text)
    if a2_07:
        last07 = a2_07[-1].upper()
        if last07 == "00":
            out.append(("AUTH/A2-07", "x reply 00: pairing window not open or expired",
                        "this is what a pairing timeout looks like: clear pairing, read a"
                        " code, enter it within 30 s without leaving the pairing screen."
                        "Editing login_mac will not help"))
        elif last07 in ("3C", "5A"):
            out.append(("AUTH/A2-07", "x reply %s: device rate limit" % last07,
                        "wait 30-90 s; do not reconnect back to back"))
        elif last07 == "02":
            out.append(("AUTH/A2-07", "+ reply 02: window open, code required", "nothing to do"))
        elif last07 == "01":
            # Measured 2026-11-07: 01 does not mean authorised. It only means the device
            # has this MAC registered; A2/08 must still pass (01 -> A2/08=00 was seen).
            out.append(("AUTH/A2-07", "- reply 01: MAC registered (code still required)",
                        "not a terminal state; judge by A2/08 below"))
        else:
            out.append(("AUTH/A2-07", "? reply %s: undefined" % last07,
                        "record the value and report it; possibly new firmware"))
    else:
        out.append(("AUTH/A2-07", "- no reply",
                    "if SMP passed this is usually enumeration; check FIFO/CREDITS"))

    if a2_08:
        last08 = a2_08[-1].upper()
        if last08 == "01":
            out.append(("AUTH/A2-08", "+ reply 01: application login passed", "nothing to do"))
        else:
            hint = "code expired or mistyped; read a new one, no reflash needed"
            if a2_07 and a2_07[-1].upper() == "00":
                hint = ("the code is not the cause: A2/07 was 00, so the window never"
                        " opened. Clear pairing and read a new code first")
            out.append(("AUTH/A2-08", "x reply %s: code rejected" % last08, hint))
    else:
        out.append(("AUTH/A2-08", "- no reply",
                    "confirm the six digits reached the serial console"))

    # ---- layer 3: state broadcast
    a0_hits = len(re.findall(r"\[A0\]", text))
    if a0_hits:
        out.append(("A0", "+ %d state frames received" % a0_hits,
                    "expect flow 0-11 L/min, power 0-50%, fan 700-1500 rpm;"
                    " if values look wrong, raw on and check the frame is 34 bytes"))
    elif a2_08 and a2_08[-1].upper() == "01":
        out.append(("A0", "x login passed but no broadcast",
                    "confirm both CCCDs are subscribed; raw on to spot partial frames"))
    else:
        out.append(("A0", "- not reached yet", "fix the problems above first"))

    # ---- link layer: repeated failures are usually the rate limit
    # Measured 2026-11-07: three failures after 19:17:53, recovered only at 19:21:55 (~3 min).
    link_fail = len(re.findall(r"cannot connect", text))
    if link_fail >= 2:
        out.append(("LINK", "! %d consecutive failures: likely the device rate limit" % link_fail,
                    "stop retrying for 2-3 minutes; check the pairing screen stayed open"))

    # ---- extra signals
    if re.search(r"service not found|GATT not ready", text):
        out.append(("GATT", "x service enumeration failed",
                    "usually a broken pairing: run pair to clear the bond and re-pair"))
    if re.search(r"no reply|timeout", text, re.I):
        out.append(("RX", "! timeout or no reply seen",
                    "check the phone app is not holding the device, or the panel left the menu"))
    return out


def main() -> int:
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
    else:
        path = find_latest()
    if not path or not path.exists():
        print("no log found. Run the control panel once; logs land in logs/panel_*.log")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    print("log: %s (%d lines)" % (path, len(text.splitlines())))
    print("=" * 68)
    stages = diagnose(text)
    for stage, verdict, advice in stages:
        print("[%-12s] %s" % (stage, verdict))
        if advice != "nothing to do":
            print("               → %s" % advice)
    print("=" * 68)
    # The verdict must rest on A2/08=01 (application auth), never on A2/07 - measured:
    # A2/07=01 only means the MAC is registered, and A2/08 can still be rejected.
    ok = (bool(re.search(r"A2/08\s*reply\s*=\s*01", text))
          and "[A0]" in text)
    print("overall: %s" % ("+ all three criteria met, link verified" if ok
                       else "x not working yet; follow the first x above"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
