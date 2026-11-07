#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Bosch heater ESP32 gateway - build / flash / serial console / logging
#   python gateway_cli.py --flash   build, flash, then open the console (first run)
#   python gateway_cli.py           console only
#   python gateway_cli.py --build   build only
from __future__ import annotations
import argparse, datetime, os, pathlib, shutil, subprocess, sys, threading, time

ROOT = pathlib.Path(__file__).resolve().parent
TOOLCHAIN = ROOT.parent / "toolchain"
LOGDIR = ROOT.parent / "logs"

# arduino-cli: prefer the one on PATH, fall back to the bundled toolchain/ (not committed)
CLI = pathlib.Path(shutil.which("arduino-cli") or (TOOLCHAIN / "arduino-cli.exe"))
CONFIG = TOOLCHAIN / "arduino-cli.yaml"

# Windows path limit: the build workspace must live on a short path; sources stay here.
# Defaults to ~/esp, override with BTWH_WORK; --config-file is skipped when absent.
WORK = pathlib.Path(os.environ.get("BTWH_WORK") or (pathlib.Path.home() / "esp"))
SKETCH_SRC = ROOT / "arduino_sketch"
SKETCH = WORK / "arduino_sketch"
BUILD = WORK / "build"
LIBS = WORK / "user" / "libraries"
# Matches the firmware already verified on the board. esptool reports 16 MB flash plus
# 8 MB Octal PSRAM, but enabling them needs FlashSize=16M,PartitionScheme=default_8MB,
# PSRAM=opi and triggers a full rebuild - not needed for the BLE proof, so it waits.
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
    # Overwrite files only, never delete the directory (bulk deletes trip the guard)
    WORK.mkdir(parents=True, exist_ok=True)
    SKETCH.mkdir(parents=True, exist_ok=True)
    for f in SKETCH_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, SKETCH / f.name)


def _cmd(*args):
    """Assemble the arduino-cli command; config and libs are added only when present."""
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
    print("\nSerial log: " + str(logpath))
    print("Type the six-digit panel code; quit to exit; help lists every command.\n")
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except Exception as e:
        print("cannot open %s: %s" % (port, e))
        return 1
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                raw = ser.readline()
            except Exception as e:
                print("[serial closed] %s" % e)
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            out = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), line)
            print(out, flush=True)
            log.write(out + "\n")

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1)
    ser.write(b"reboot\n")   # do not touch DTR/RTS; the board would enter the bootloader
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
        print("\nlog saved: " + str(logpath))
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
            print("\nbuild failed, exit=%d" % rc); return rc
        print("\n[OK] build passed")
        if not a.flash:
            return 0
        rc = upload(port)
        if rc != 0:
            print("\nflash failed, exit=%d" % rc); return rc
        print("\n[OK] flashed, opening the console in 3 s...")
        time.sleep(3)
        if a.no_monitor:
            return 0
    if not port:
        print("no serial port found; pass --port COM3 (the CH343 one)")
        return 1
    print("using %s @ %d" % (port, a.baud))
    return serial_session(port, a.baud)


if __name__ == "__main__":
    sys.exit(main())
