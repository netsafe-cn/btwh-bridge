# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""analyze_collect.py - analyse the CSV produced by collector.py

Output:
  1. overview: frame count, duration, sampling interval
  2. value range per field
  3. ASCII curves: outlet / inlet / chamber / flow / power / fan
  4. byte correlation: which offsets move with inlet temperature (field map check)

Usage:
    python analyze_collect.py collect_20261101_140230.csv
"""
import csv
import sys
from collections import Counter


def load(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                r["rawb"] = bytes.fromhex(r["raw"])
            except Exception:
                continue
            rows.append(r)
    return rows


def spark(vals, width=60, lo=None, hi=None):
    """Render a block-character curve."""
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1e-6:
        hi = lo + 1
    chars = " ▁▂▃▄▅▆▇█"
    out = []
    step = max(1, len(vals) // width)
    for i in range(0, len(vals), step):
        v = vals[i]
        k = int((v - lo) / (hi - lo) * (len(chars) - 1))
        out.append(chars[max(0, min(len(chars) - 1, k))])
    return "".join(out)


def main():
    path = sys.argv[1]
    rows = load(path)
    if not rows:
        print("no data")
        return 1
    n = len(rows)
    ts = [float(r["ts"]) for r in rows]
    dur = ts[-1] - ts[0]
    gaps = [ts[i + 1] - ts[i] for i in range(n - 1)]
    avg_gap = sum(gaps) / len(gaps) if gaps else 0

    print("=" * 70)
    print("file: %s" % path)
    print("frames: %d   duration: %.1f s   mean interval: %.1f s" % (n, dur, avg_gap))
    print("=" * 70)

    def col(name):
        return [float(r[name]) for r in rows if r[name] not in ("", None)]

    print("\n%-8s %8s %8s %8s" % ("field", "min", "max", "first->last"))
    for key, label in (("out", "outlet"), ("in", "inlet"), ("chamber", "chamber"),
                       ("box", "chassis"), ("setpoint", "setpoint"), ("flow", "flow"),
                       ("power", "power"), ("fan", "fan")):
        v = col(key)
        if not v:
            continue
        print("%-8s %8.1f %8.1f   %.1f → %.1f"
              % (label, min(v), max(v), v[0], v[-1]))

    print("\n--- curves (about %d frames per column) ---" % max(1, n // 60))
    for key, label in (("out", "outlet"), ("in", "inlet"), ("flow", "flow"),
                       ("power", "power"), ("fan", "fan")):
        v = col(key)
        if not v:
            continue
        print("%-6s %s  [%.0f~%.0f]" % (label, spark(v), min(v), max(v)))

    # Byte correlation: offsets whose trend best matches the inlet temperature
    print("\n--- byte correlation (offsets tracking inlet temperature) ---")
    ins = [float(r["in"]) for r in rows if r["in"] not in ("", None)]
    if len(ins) > 10 and len(set(round(x, 1) for x in ins)) > 3:
        # Normalised correlation coefficient (simplified)
        def corr(a, b):
            m = min(len(a), len(b))
            a, b = a[:m], b[:m]
            ma = sum(a) / m
            mb = sum(b) / m
            va = sum((x - ma) ** 2 for x in a) ** .5
            vb = sum((y - mb) ** 2 for y in b) ** .5
            if va < 1e-9 or vb < 1e-9:
                return 0.0
            return sum((a[i] - ma) * (b[i] - mb) for i in range(m)) / (va * vb)

        flen = min(len(ins), n)
        scores = []
        for off in range(34):
            v = [rows[i]["rawb"][off] for i in range(flen)]
            scores.append((abs(corr(ins[:flen], v)), off,
                           min(v), max(v)))
        scores.sort(reverse=True)
        print("correlation  offset  min  max")
        for s, off, lo, hi in scores[:6]:
            print("%5.2f   [%2d]  %4d  %4d" % (s, off, lo, hi))
    else:
        print("inlet temperature barely moved; skipping")

    # Fault / ionisation occurrences
    faults = Counter(r["fault"] for r in rows)
    ions = Counter(r["ion"] for r in rows)
    print("\nfault code distribution: %s" % dict(faults))
    print("ionisation (1 = flame): %s" % dict(ions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
