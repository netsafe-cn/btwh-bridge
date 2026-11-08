#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""watch_a0.py - print the A0 broadcast as a table, straight off the serial port

For checking that the firmware actually follows the heater: open a hot tap and watch `flow`,
`power` and `out` move together; close it and watch `power` fall back.

    python firmware/bridge/tools/watch_a0.py                  # 60 s
    python firmware/bridge/tools/watch_a0.py --seconds 180
    python firmware/bridge/tools/watch_a0.py --csv run.csv     # also write every frame
    python firmware/bridge/tools/watch_a0.py --only-active     # skip frames with flow < 0.5

Why a separate tool: `log_diag.py` answers "why did it not connect", this answers "is the data
alive". Both were written after a change, and both are cheaper than reading the log by eye.

Exit code: 0 if at least one frame arrived, 1 if the port could not be opened or nothing came.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys
import time

FIELDS = ("out", "in", "chamber", "box", "set", "power", "flow", "fan", "flame", "fault")
PATTERN = re.compile(
    r"out([\d.]+) in([\d.]+) chamber([\d.]+) chassis([\d.]+) set(\d+)\s+"
    r"power(\d+)% flow([\d.]+)L/min fan (\d+)rpm flame (\w+) fault (\w+)"
)

HEADER = ("   t", "出水", "进水", "炉腔", "机身", "设定", "功率", "水流", "风机", "火焰", "故障")


def find_port() -> str | None:
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None
    for p in lp.comports():
        if (p.vid, p.pid) in ((0x1A86, 0x55D3), (0x1A86, 0x7523)) or "CH343" in (p.description or ""):
            return p.device
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=60.0, help="how long to watch (default 60)")
    ap.add_argument("--port", default=None, help="serial port; defaults to the CH343 one")
    ap.add_argument("--csv", default=None, help="write every frame to this CSV as well")
    ap.add_argument("--only-active", action="store_true",
                    help="skip frames with flow < 0.5 L/min (when watching a tap)")
    a = ap.parse_args()

    port = a.port or find_port()
    if not port:
        print("no CH343 serial port; check the board is plugged in, or pass --port")
        return 1

    try:
        import serial
    except ImportError:
        print("pyserial is missing:  pip install -r requirements-dev.txt")
        return 1

    try:
        s = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print("cannot open %s: %s" % (port, e))
        print("-> if the console panel or gateway_cli is running, close it (one process per port)")
        return 1

    s.setDTR(False)
    s.setRTS(False)
    time.sleep(0.3)
    s.reset_input_buffer()

    print("%s @115200, watching %.0f s ...\n" % (port, a.seconds))
    print(" ".join("%4s" % h for h in HEADER))
    print("-" * 66)

    writer = None
    handle = None
    if a.csv:
        handle = open(a.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(("t",) + FIELDS)

    t0 = time.time()
    rows = []
    try:
        while time.time() - t0 < a.seconds:
            raw = s.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = PATTERN.search(line)
            if not m:
                continue
            d = dict(zip(FIELDS, m.groups()))
            d["t"] = round(time.time() - t0, 1)
            rows.append(d)
            if writer:
                writer.writerow([d["t"]] + [d[k] for k in FIELDS])
            if a.only_active and float(d["flow"]) < 0.5:
                continue
            print("%5.1f %4s %4s %5s %5s %4s %3s%% %5s %5s %5s %5s" % (
                d["t"], d["out"], d["in"], d["chamber"], d["box"], d["set"],
                d["power"], d["flow"], d["fan"], d["flame"], d["fault"]))
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        s.close()
        if handle:
            handle.close()

    print("-" * 66)
    if not rows:
        print("no A0 frames in %.0f s - is the link up? (status, then go)" % a.seconds)
        return 1

    gaps = [(b["t"] - a_["t"]) for a_, b in zip(rows, rows[1:])]
    print("frames %d, interval %.1f s (min %.1f, max %.1f)" % (
        len(rows), sum(gaps) / len(gaps) if gaps else 0,
        min(gaps) if gaps else 0, max(gaps) if gaps else 0))

    for key, label, fmt in (("out", "出水", "%.1f"), ("in", "进水", "%.1f"),
                            ("chamber", "炉腔", "%.1f"), ("power", "功率", "%d%%"),
                            ("flow", "水流", "%.1f")):
        vals = [float(r[key]) for r in rows]
        print("  %s %s ~ %s" % (label, fmt % min(vals), fmt % max(vals)))

    if a.csv:
        print("csv: %s" % pathlib.Path(a.csv).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
