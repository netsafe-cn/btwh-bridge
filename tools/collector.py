# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""collector.py —— 连续采集 A0 广播，逐帧写 CSV

用途：长时间工况（如开启热水循环泵，进水温度缓慢上升）下的报文采集，
用于反推字段语义、绘制曲线。

输出：
    collect_<时间戳>.csv      每帧一行：时间 + 34 字节原始 + 解析字段
    collect_<时间戳>.summary  结束时打印的字节取值范围统计

用法：
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
    log("采集 %.0f 分钟 → %s" % (a.minutes * 60 / 60, out))

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
    log("✓ 链路建立")
    try:
        ok = await dev.login()
        log("登录结果: %s（%s）" % ("成功" if ok else "未通过", dev.login_state))
    except Exception as e:
        log("登录异常: %s" % e)

    end = time.time() + a.minutes * 60
    last_n = 0
    while time.time() < end:
        await asyncio.sleep(2)
        if len(rows) != last_n:
            last_n = len(rows)
            st = dev.state
            if st:
                log("帧 %3d | 出%.1f 进%.1f 炉腔%.1f 设定%s 功率%d%% 水%.1fL 风机%s"
                    % (len(rows), st.temp_out or 0, st.temp_in or 0,
                       st.chamber or 0, st.setpoint, st.burner_power,
                       st.water_flow or 0, st.fan_speed))
        if await dev.idle_seconds() > 30:
            log("超过 30 秒无数据，提前结束")
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
    log("已写入 %d 帧 → %s" % (len(rows), out))

    # 字节统计
    if raw_frames:
        n = min(len(r) for r in raw_frames)
        lines = ["帧数 %d，最短 %d 字节" % (len(raw_frames), n), "",
                 "偏移  最小  最大  极差"]
        for i in range(n):
            v = [r[i] for r in raw_frames]
            lines.append("[%2d]  %4d  %4d  %4d" % (i, min(v), max(v), max(v) - min(v)))
        summary = out.with_suffix(".summary")
        summary.write_text("\n".join(lines), encoding="utf-8")
        log("字节统计 → %s" % summary)
        print("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
