# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""analyze_collect.py —— 分析 collector.py 产出的 CSV

输出：
  1. 概览：帧数、时长、采样间隔
  2. 各字段取值范围
  3. ASCII 曲线：出水/进水/炉腔/流量/功率/风机
  4. 字节关联分析：哪些偏移与"进水温度"同步变化（用于校验字段映射）

用法：
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
    """生成方块字符曲线。"""
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
        print("没有数据")
        return 1
    n = len(rows)
    ts = [float(r["ts"]) for r in rows]
    dur = ts[-1] - ts[0]
    gaps = [ts[i + 1] - ts[i] for i in range(n - 1)]
    avg_gap = sum(gaps) / len(gaps) if gaps else 0

    print("=" * 70)
    print("文件: %s" % path)
    print("帧数: %d   时长: %.1f 秒   平均间隔: %.1f 秒" % (n, dur, avg_gap))
    print("=" * 70)

    def col(name):
        return [float(r[name]) for r in rows if r[name] not in ("", None)]

    print("\n%-8s %8s %8s %8s" % ("字段", "最小", "最大", "首→末"))
    for key, label in (("out", "出水"), ("in", "进水"), ("chamber", "炉腔"),
                       ("box", "机身"), ("setpoint", "设定"), ("flow", "流量"),
                       ("power", "功率"), ("fan", "风机")):
        v = col(key)
        if not v:
            continue
        print("%-8s %8.1f %8.1f   %.1f → %.1f"
              % (label, min(v), max(v), v[0], v[-1]))

    print("\n--- 曲线（每格约 %d 帧）---" % max(1, n // 60))
    for key, label in (("out", "出水"), ("in", "进水"), ("flow", "流量"),
                       ("power", "功率"), ("fan", "风机")):
        v = col(key)
        if not v:
            continue
        print("%-6s %s  [%.0f~%.0f]" % (label, spark(v), min(v), max(v)))

    # 字节关联：与进水温度变化趋势最一致的偏移
    print("\n--- 字节关联分析（与进水温度同步的偏移）---")
    ins = [float(r["in"]) for r in rows if r["in"] not in ("", None)]
    if len(ins) > 10 and len(set(round(x, 1) for x in ins)) > 3:
        # 归一化后算相关系数（简易）
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
        print("关联度  偏移  最小  最大")
        for s, off, lo, hi in scores[:6]:
            print("%5.2f   [%2d]  %4d  %4d" % (s, off, lo, hi))
    else:
        print("进水温度变化不足，跳过")

    # 故障/电离出现情况
    faults = Counter(r["fault"] for r in rows)
    ions = Counter(r["ion"] for r in rows)
    print("\n故障码分布: %s" % dict(faults))
    print("电离(1=有火): %s" % dict(ions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
