# -*- coding: utf-8 -*-
"""pair_once.py —— 一条命令跑完「输码 → 连接 → SMP → 登录 → 收 A0」

配对窗口只有几十秒，手动开窗口、找输入框、敲码很容易错过。
这个脚本把整个流程压成一条命令，跑完直接给结论。

用法：
    python pair_once.py 311522              # 默认观察 30 秒
    python pair_once.py 311522 --wait 45    # 设备响应慢时多等一会儿
    python pair_once.py 311522 --reboot     # 先重启板子再发码（默认不重启，最快）
    python pair_once.py 311522 --port COM3

退出码：0 = 三段判据全部满足；2 = 未通过（原因见输出）
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
except Exception:                       # 诊断器缺失也不影响主流程
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
    ap.add_argument("code", help="热水器面板显示的 6 位配对码")
    ap.add_argument("--wait", type=float, default=30.0, help="发码后观察秒数（默认 30）")
    ap.add_argument("--port", default=None, help="串口，默认自动找 CH343")
    ap.add_argument("--reboot", action="store_true", help="先重启板子再发码")
    a = ap.parse_args()

    code = (a.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        print("配对码应为 6 位数字，收到：%r" % a.code)
        return 1

    import serial
    port = a.port or pick_port()
    if not port:
        print("找不到 CH343 串口，请确认板子已插好，或用 --port 指定")
        return 1

    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print("打不开 %s：%s" % (port, e))
        print("→ 如果控制面板 / gateway_cli 正在运行，先关掉它（同一串口只能一个进程占用）")
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
            emit("[--] 重启板子…")
            ser.write(b"reboot\n")
            ser.flush()
            time.sleep(2.0)
            ser.reset_input_buffer()

        emit("[--] 串口 %s @115200，发送面板码 %s" % (port, code))
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
            if advice != "无需处理":
                print("               → %s" % advice)
        ok = (any("✓ 链路已加密" in v for _, v, _ in stages)
              and any("✓ 应答 01" in v for _, v, _ in stages)
              and any("✓ 收到" in v for _, v, _ in stages))
    else:
        ok = "A2/08 应答 = 01" in text and "认证完成" in text
    print("=" * 68)
    print("日志：%s" % logpath)
    print("结论：%s" % ("✓ 通过，三段判据全部满足" if ok
                       else "✗ 未通过，按上面第一条 ✗ 处理"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
