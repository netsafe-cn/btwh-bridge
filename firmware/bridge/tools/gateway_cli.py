#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Bosch heater ESP32 gateway - build / flash / serial console / logging
#   python firmware/bridge/tools/gateway_cli.py --flash   build, flash, console
#   python firmware/bridge/tools/gateway_cli.py           console only
#   python firmware/bridge/tools/gateway_cli.py --build   build only
from __future__ import annotations
import argparse, datetime, os, pathlib, re, shutil, subprocess, sys, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent   # firmware/bridge
TOOLCHAIN = ROOT / "toolchain"
LOGDIR = ROOT / "logs"

# arduino-cli: prefer the one on PATH, fall back to the bundled toolchain/ (not committed)
CLI = pathlib.Path(shutil.which("arduino-cli") or (TOOLCHAIN / "arduino-cli.exe"))
CONFIG = TOOLCHAIN / "arduino-cli.yaml"

# Windows path limit: the build workspace must live on a short path; sources stay here.
# Defaults to ~/esp, override with BTWH_WORK; --config-file is skipped when absent.
WORK = pathlib.Path(os.environ.get("BTWH_WORK") or (pathlib.Path.home() / "esp"))
SRC = ROOT / "src"                       # the only copy of the sources
SKETCH = WORK / "arduino_sketch"         # generated shell for arduino-cli
SKETCH_NAME = SKETCH.name
BUILD = WORK / "build"
LIBS = WORK / "user" / "libraries"
# The FQBN is composed, not hardcoded, because a fixed `FlashSize=16M` made every other board
# fail at the flash step with an error the guide never explained. FlashSize now follows the chip
# that is actually on the desk; the partition scheme does not.
#
# The scheme stays `default_8MB` on every board (app0/app1 at 0x10000/0x340000, 3.19 MB each,
# otadata booting app1). Letting it float with FlashSize would be worse than wasteful: a bare
# FlashSize=8M selects the 4 MB `default` scheme, whose app slot is 1.31 MB at 0x10000, so the
# image compiles for a slot otadata never boots and a flash succeeds without changing what runs.
# One fixed layout also keeps releases interchangeable between an 8 MB and a 16 MB board.
#
# PSRAM stays on `opi` by default because that is the state the bench board was verified in:
# 2026-11-12 the banner moved from `PSRAM=0 bytes` to `8386279 bytes` when the flag was added and
# the boot path changed to qio_opi, so it was flashed as its own run and checked before anything
# depended on it. Turning it off again would silently undo that check on the one board where it
# was actually done.
#
# It is not required, though. Nothing under src/ allocates from PSRAM - the only reference is the
# boot line that prints its size - so a board without an Octal part runs the same firmware. The
# `--psram none` switch exists because a hardcoded `PSRAM=opi` turned every plain N8 or N16 board
# (the ones sold without an R) into a boot-time coin flip that the getting-started guide never
# mentioned, and a first-time builder has no way to diagnose that.
BASE_FQBN = "esp32:esp32:esp32s3"
SCHEME = "default_8MB"          # 3.19 MB per OTA slot
MIN_FLASH_MB = 8                # two slots; anything smaller cannot update itself
DEFAULT_FLASH_MB = 16           # the bench board: esptool 0x46/0x4018, silkscreen says N8R8


def make_fqbn(flash_mb=DEFAULT_FLASH_MB, psram="opi"):
    """Compose the FQBN for one board. arduino-esp32 accepts 4 / 8 / 16 MB of flash."""
    fqbn = "%s:FlashSize=%dM,PartitionScheme=%s" % (BASE_FQBN, flash_mb, SCHEME)
    if psram and psram != "none":
        fqbn += ",PSRAM=" + psram
    return fqbn


FQBN = make_fqbn()


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


def _esptool():
    """Return the command prefix for esptool, or None when there is no copy to run.

    Three sources, cheapest first: an esptool importable by this interpreter, then `esptool` on
    PATH, then the one arduino-esp32 installs under Arduino15. None of them is guaranteed -
    arduino-cli builds a board happily without a Python esptool anywhere - so probing is a
    convenience and must degrade to the default, never to a failed build.
    """
    try:
        import esptool  # noqa: F401
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    for name in ("esptool", "esptool.py"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    if os.name == "nt":
        pattern = "AppData/Local/Arduino15/packages/esp32/tools/esptool_py/*/esptool.exe"
        found = sorted(pathlib.Path.home().glob(pattern))
        if found:
            return [str(found[-1])]
    return None


def detect_flash_mb(port):
    """Ask the chip how much flash it has; None when that question cannot be answered.

    Called only on the --flash path. esptool enters the bootloader by pulling DTR/RTS, which
    resets the board, and a reset drops the BLE link - the heater then refuses the next session
    for minutes (measured 2026-11-08, twice). A flash resets the board anyway, so paying that cost
    here is free; `--build` must never pay it, which is why it does not call this.
    """
    if not port:
        return None
    prefix = _esptool()
    if not prefix:
        return None
    try:
        done = subprocess.run(
            prefix + ["--chip", "esp32s3", "--port", port, "--baud", "115200", "flash_id"],
            capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    # "Detected flash size: 16MB" - the number is ground truth, the surrounding sentence is not
    # parsed: esptool prefixes it with a timestamp and the wording changes between versions.
    hit = re.search(r"flash size:\s*(\d+)\s*MB", (done.stdout or "") + (done.stderr or ""),
                    re.IGNORECASE)
    return int(hit.group(1)) if hit else None


def clamp_flash_mb(mb):
    """Snap a reported size onto a value arduino-esp32 knows (4 / 8 / 16).

    A 32 MB part still compiles as 16 MB: the scheme only reaches 0x670000, so the upper half is
    unused either way and there is no layout to invent for it. Sizes below 8 are returned as 4 on
    purpose - the caller refuses them with an explanation instead of building something that
    cannot update itself.
    """
    return 16 if mb >= 16 else (8 if mb >= MIN_FLASH_MB else 4)


def run(cmd):
    print("\n>>> " + " ".join(str(x) for x in cmd) + "\n", flush=True)
    return subprocess.call([str(x) for x in cmd])


def sync_sketch():
    """Materialise an arduino-cli sketch around firmware/bridge/src/.

    arduino-cli insists on a directory containing an .ino of the same name. Rather than keep a
    second copy of the sources (which drifts the moment anyone forgets), the .ino is generated as
    a comment and the real code is copied in under src/. PlatformIO reads src/ directly, so both
    build systems see one copy of the truth.
    """
    WORK.mkdir(parents=True, exist_ok=True)
    SKETCH.mkdir(parents=True, exist_ok=True)
    (SKETCH / (SKETCH_NAME + ".ino")).write_text(
        "// GENERATED by tools/gateway_cli.py - do not edit.\n"
        "// arduino-cli needs an .ino whose name matches the sketch directory; the application,\n"
        "// including setup() and loop(), lives in src/.\n",
        encoding="utf-8")
    dst = SKETCH / "src"
    for f in SRC.rglob("*"):
        if f.is_file():
            target = dst / f.relative_to(SRC)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    print("  synced %d source files to %s" % (sum(1 for _ in SRC.rglob("*") if _.is_file()), dst))


def _cmd(*args):
    """Assemble the arduino-cli command; config and libs are added only when present."""
    out = [CLI]
    if CONFIG.exists():
        out += ["--config-file", CONFIG]
    return out + list(args)


def build(clean=False, fqbn=None):
    fqbn = fqbn or FQBN
    sync_sketch()
    if clean and BUILD.exists():
        # arduino-cli's dependency tracking missed a header-only edit once (2026-11-11):
        # mqtt_client.h changed after mqtt_client.cpp.o existed, only main.cpp was rebuilt,
        # and the flashed binary said 2.6.1-golden on the banner while publishing
        # 2.6.0-rules in attributes. A mixed build is worse than no build, so --clean is
        # the default advice after editing any header.
        # The stale tree is RENAMED, not deleted: bulk-deleting from a build script trips
        # every sane safety hook, and a rename achieves the same forced full rebuild with
        # nothing destroyed. Delete the .stale-* dirs by hand when disk gets tight.
        stale = WORK / ("build.stale-" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        print("  moving %s aside -> %s (clean build)" % (BUILD, stale))
        BUILD.rename(stale)
    libs = ["--libraries", LIBS] if LIBS.is_dir() else []
    # arduino-cli adds an include path for each subdirectory of src/ but not for src/ itself, so
    # a layered include written as "app/session.h" resolves relative to the caller and then
    # fails one level down (src/app/app/session.h). PlatformIO puts src/ on the path natively;
    # this line makes arduino-cli agree, so both build systems read the same include lines.
    # Needs a workspace path without spaces - the default ~/esp has none.
    inc = ["--build-property", "compiler.cpp.extra_flags=-I" + str(SKETCH / "src")]
    return run(_cmd("compile", "--fqbn", fqbn, *libs, *inc,
                    "--build-path", BUILD, SKETCH))


def upload(port, fqbn=None):
    return run(_cmd("upload", "-p", port, "--fqbn", fqbn or FQBN,
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
    # Attach, do not reset. `ESP.restart()` drops the BLE link at the radio level and the heater
    # then refuses the next session for minutes - measured 2026-11-08: opening a console cost
    # ~15 minutes of no service, twice. `status` gives the same orientation for free.
    ser.write(b"status\n")   # do not touch DTR/RTS; the board would enter the bootloader
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
    ap.add_argument("--clean", action="store_true",
                    help="remove the build directory first (use after editing any header)")
    ap.add_argument("--no-monitor", action="store_true")
    ap.add_argument("--flash-size", choices=("auto", "8", "16"), default="auto",
                    help="flash size of the board; auto asks the chip on --flash, else 16")
    ap.add_argument("--psram", choices=("opi", "none", "quad"), default="opi",
                    help="PSRAM mode; opi is the verified default, none for boards without it")
    a = ap.parse_args()
    port = a.port or find_port()

    flash_mb = DEFAULT_FLASH_MB
    if a.flash_size != "auto":
        flash_mb = int(a.flash_size)
    elif a.flash:
        # Only here: probing resets the board, and --build must leave a running bridge running.
        reported = detect_flash_mb(port)
        if reported:
            flash_mb = clamp_flash_mb(reported)
            if reported != flash_mb:
                print("  board reports %d MB flash, compiling as %d MB" % (reported, flash_mb))
            else:
                print("  board reports %d MB flash" % reported)
        else:
            print("  flash size could not be read; compiling for %d MB "
                  "(pass --flash-size 8 for an 8 MB board)" % flash_mb)

    if flash_mb < MIN_FLASH_MB:
        # One 3.19 MB app slot and no second one: OTA is the whole point of this firmware, so a
        # board that cannot hold two slots is not a smaller answer, it is a different product.
        print("\nthis board has %d MB of flash; the bridge needs >= %d MB for two 3.19 MB OTA "
              "slots.\nA 4 MB ESP32-S3 Mini / Zero will not do - buy an 8 MB or 16 MB part."
              % (flash_mb, MIN_FLASH_MB))
        return 1

    fqbn = make_fqbn(flash_mb, a.psram)
    print("  fqbn: " + fqbn)

    if a.build or a.flash:
        rc = build(clean=a.clean, fqbn=fqbn)
        if rc != 0:
            print("\nbuild failed, exit=%d" % rc); return rc
        print("\n[OK] build passed")
        if not a.flash:
            return 0
        rc = upload(port, fqbn=fqbn)
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
