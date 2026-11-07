# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""collector.py - capture A0 broadcasts continuously, one CSV row per frame

For long runs (a circulation pump slowly raising the inlet temperature, say), to
reverse-engineer field semantics and draw curves.

Output:
    collect_<timestamp>.csv      one row per frame: time + 34 raw bytes + parsed fields
    collect_<timestamp>.summary  byte range statistics, written at the end

Usage:
    python collector.py --minutes 10
"""
import argparse
import asyncio
import csv
import pathlib
import sys
import time

from bosch_api import BoschDevice, BoschProtocol, Config

T0 = time.time()
rows = []
raw_frames = []


def log(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    cfg = Config.load()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = pathlib.Path(a.out or ("collect_%s.csv" % stamp))
    log("capturing for %.0f minutes -> %s" % (a.minutes, out))

    def on_state(st):
        raw_frames.append(st.raw)
        rows.append([
            time.strftime("%H:%M:%S"), "%.1f" % st.ts,
            st.raw.hex(" "),
            st.frame_type,
            "%.2f" % (st.temp_out or 0), "%.2f" % (st.temp_in or 0),
            "%.2f" % (st.chamber or 0), "%.2f" % (st.temp_box or 0),
            st.setpoint if st.setpoint is not None else "",
            "%.1f" % (st.water_flow or 0), st.burner_power,
            st.fan_speed if st.fan_speed is not None else "",
            1 if st.ionized else 0, st.failure,
        ])

    dev = BoschDevice(cfg, on_state=on_state, on_log=lambda m: log("  [dev] " + m))
    await dev.connect()
    log("[OK] link established")
    try:
        ok = await dev.login()
        log("login result: %s (%s)" % ("ok" if ok else "failed", dev.login_state))
    except Exception as e:
        log("login raised: %s" % e)

    end = time.time() + a.minutes * 60
    last_n = 0
    while time.time() < end:
        await asyncio.sleep(2)
        if len(rows) != last_n:
            last_n = len(rows)
            st = dev.state
            if st:
                log("frame %3d | out%.1f in%.1f chamber%.1f set%s power%d%% flow%.1fL fan%s"
                    % (len(rows), st.temp_out or 0, st.temp_in or 0,
                       st.chamber or 0, st.setpoint, st.burner_power,
                       st.water_flow or 0, st.fan_speed))
        if await dev.idle_seconds() > 30:
            log("no data for 30 s, stopping early")
            break

    try:
        await dev.close()
    except Exception:
        pass

    header = ["time", "ts", "raw", "type", "out", "in", "chamber", "box",
              "setpoint", "flow", "power", "fan", "ion", "fault"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    log("wrote %d frames -> %s" % (len(rows), out))

    # Byte statistics
    if raw_frames:
        n = min(len(r) for r in raw_frames)
        lines = ["frames %d, shortest %d bytes" % (len(raw_frames), n), "",
                 "offset  min  max  range"]
        for i in range(n):
            v = [r[i] for r in raw_frames]
            lines.append("[%2d]  %4d  %4d  %4d" % (i, min(v), max(v), max(v) - min(v)))
        summary = out.with_suffix(".summary")
        summary.write_text("\n".join(lines), encoding="utf-8")
        log("byte statistics -> %s" % summary)
        print("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
