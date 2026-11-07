#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 博世热水器 ESP32 网关 —— 编译 / 烧录 / 串口交互 / 日志
#   python gateway_cli.py --flash   编译+烧录+进入交互（第一次用这个）
#   python gateway_cli.py           只连串口进入交互
#   python gateway_cli.py --build   只编译
from __future__ import annotations
import argparse, datetime, os, pathlib, shutil, subprocess, sys, threading, time

ROOT = pathlib.Path(__file__).resolve().parent
TOOLCHAIN = ROOT.parent / "toolchain"
LOGDIR = ROOT.parent / "logs"

# arduino-cli：优先 PATH 里的，其次工程内 toolchain/（本地工具链不入库）
CLI = pathlib.Path(shutil.which("arduino-cli") or (TOOLCHAIN / "arduino-cli.exe"))
CONFIG = TOOLCHAIN / "arduino-cli.yaml"

# Windows 路径长度限制：编译工作区必须放在短路径，源码仍留在工程里。
# 默认 ~/esp，可用环境变量 BTWH_WORK 覆盖；没有 arduino-cli.yaml 时自动省略该参数。
WORK = pathlib.Path(os.environ.get("BTWH_WORK") or (pathlib.Path.home() / "esp"))
SKETCH_SRC = ROOT / "arduino_sketch"
SKETCH = WORK / "arduino_sketch"
BUILD = WORK / "build"
LIBS = WORK / "user" / "libraries"
# 与板载已验证固件保持一致。esptool flash_id 实测硬件是 16MB Flash + 8MB Octal PSRAM，
# 但启用需 FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi（见 README 七/九节），
# 会触发一次 10~15 分钟的全量重编译；BLE 可行性实测不需要它，故留到实测通过后再切。
FQBN = "esp32:esp32:esp32s3:FlashSize=8M"


def find_port():
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None
    ports = list(lp.comports())
    for p in ports:
        if (p.vid, p.pid) in ((0x1A86, 0x55D3), (0x1A86, 0x7523)) or "CH343" in (p.description or ""):
            return p.device
    for p in ports:
        if p.device.upper().startswith("COM"):
            return p.device
    return None


def run(cmd):
    print("\n>>> " + " ".join(str(x) for x in cmd) + "\n", flush=True)
    return subprocess.call([str(x) for x in cmd])


def sync_sketch():
    # 只覆盖文件，不删目录（避免触发批量删除保护）
    WORK.mkdir(parents=True, exist_ok=True)
    SKETCH.mkdir(parents=True, exist_ok=True)
    for f in SKETCH_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, SKETCH / f.name)


def _cmd(*args):
    """组装 arduino-cli 命令：配置文件、库目录按存在性附加，别人 clone 后也能直接跑。"""
    out = [CLI]
    if CONFIG.exists():
        out += ["--config-file", CONFIG]
    return out + list(args)


def build():
    sync_sketch()
    libs = ["--libraries", LIBS] if LIBS.is_dir() else []
    return run(_cmd("compile", "--fqbn", FQBN, *libs,
                    "--build-path", BUILD, SKETCH))


def upload(port):
    return run(_cmd("upload", "-p", port, "--fqbn", FQBN,
                    "--build-path", BUILD, SKETCH))


def serial_session(port, baud):
    import serial
    LOGDIR.mkdir(parents=True, exist_ok=True)
    logpath = LOGDIR / ("esp32_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    log = open(logpath, "a", encoding="utf-8", errors="replace", buffering=1)
    print("\n串口日志：" + str(logpath))
    print("直接输入 6 位面板码回车；quit 退出；help 看全部命令。\n")
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except Exception as e:
        print("打不开 %s：%s" % (port, e))
        return 1
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                raw = ser.readline()
            except Exception as e:
                print("[串口断开] %s" % e)
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            out = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), line)
            print(out, flush=True)
            log.write(out + "\n")

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1)
    ser.write(b"reboot\n")   # 不要手动拉 DTR/RTS，板子会进 bootloader
    ser.flush()
    try:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            log.write("[TX] " + line + "\n")
            ser.write((line + "\n").encode("ascii", "replace"))
            ser.flush()
    finally:
        stop.set(); time.sleep(0.2); ser.close(); log.close()
        print("\n日志已保存：" + str(logpath))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--flash", action="store_true")
    ap.add_argument("--no-monitor", action="store_true")
    a = ap.parse_args()
    port = a.port or find_port()
    if a.build or a.flash:
        rc = build()
        if rc != 0:
            print("\n编译失败 exit=%d" % rc); return rc
        print("\n✓ 编译通过")
        if not a.flash:
            return 0
        rc = upload(port)
        if rc != 0:
            print("\n烧录失败 exit=%d" % rc); return rc
        print("\n✓ 烧录完成，3 秒后打开串口…")
        time.sleep(3)
        if a.no_monitor:
            return 0
    if not port:
        print("找不到串口，用 --port COM3 指定（CH343 那个）")
        return 1
    print("使用串口：%s @ %d" % (port, a.baud))
    return serial_session(port, a.baud)


if __name__ == "__main__":
    sys.exit(main())
