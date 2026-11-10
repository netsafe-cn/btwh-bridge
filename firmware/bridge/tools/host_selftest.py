# -*- coding: utf-8 -*-
"""Compile the pure-C++ selftest layers and run them on the host.

Usage:
  python firmware/bridge/tools/host_selftest.py          # compile if needed, run
  python firmware/bridge/tools/host_selftest.py --force  # recompile first

The layers this compiles are the ones CI's layering check already forces to be
Arduino-free (proto/*, and net/state_json|mqtt_wire|discovery). The board keeps
running the same suites against real hardware; the point of this runner is the
window between "written" and "flashed", where until now nothing executed them.

Compiler lookup order: g++ / clang++ on PATH, then the pip package `ziglang`
(`python -m ziglang c++`, a single-binary drop-in). MSVC is not attempted: cl
needs a Visual Studio environment this script would have to reconstruct.

Exit code is the test binary's exit code: 0 = all suites green.
"""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"

SOURCES = [
    HERE / "host_selftest_main.cpp",
    SRC / "proto" / "a0.cpp",
    SRC / "proto" / "frame.cpp",
    SRC / "proto" / "selftest.cpp",
    SRC / "net" / "state_json.cpp",
    SRC / "net" / "mqtt_wire.cpp",
    SRC / "net" / "mqtt_rules.cpp",
    SRC / "net" / "provision.cpp",
    SRC / "net" / "discovery.cpp",
]

# Headers the sources include. A generated header changed by gen_*.py must trigger a rebuild
# too, or a stale exe keeps passing against a spec that moved - measured: a regenerated
# vectors_gen.h did not rebuild, and the self test reported the fixed bug as still broken.
HEADERS = [
    SRC / "proto" / "a0.h",
    SRC / "proto" / "frame.h",
    SRC / "proto" / "vectors_gen.h",
    SRC / "proto" / "profiles_gen.h",
    SRC / "proto" / "selftest.h",
    SRC / "net" / "state_json.h",
    SRC / "net" / "mqtt_wire.h",
    SRC / "net" / "mqtt_rules.h",
    SRC / "net" / "provision.h",
    SRC / "net" / "mqtt_contract.h",
    SRC / "net" / "discovery.h",
    SRC / "net" / "selftest.h",
]

BUILD = HERE / ".hosttest"
EXE = BUILD / ("host_selftest.exe" if os.name == "nt" else "host_selftest")


def find_compiler():
    for c in ("g++", "clang++"):
        if shutil.which(c):
            return [c]
    try:
        ver = subprocess.run([sys.executable, "-m", "ziglang", "version"],
                             capture_output=True, text=True, timeout=60)
        if ver.returncode == 0:
            return [sys.executable, "-m", "ziglang", "c++"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def newer(a: pathlib.Path, b: pathlib.Path) -> bool:
    return not b.exists() or a.stat().st_mtime > b.stat().st_mtime


def build(compiler, force: bool) -> pathlib.Path:
    for src in SOURCES:
        if not src.exists():
            raise SystemExit("missing source: %s" % src)
    stale = force or any(newer(s, EXE) for s in SOURCES + HEADERS)
    if stale:
        BUILD.mkdir(exist_ok=True)
        cmd = compiler + ["-std=c++17", "-I", str(SRC)] \
              + [str(s) for s in SOURCES] + ["-o", str(EXE)]
        print("[HOST] compiling %d sources with %s" % (len(SOURCES), " ".join(compiler)))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            raise SystemExit("compile failed (%d)" % rc)
    return EXE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="recompile even if up to date")
    args = ap.parse_args()

    compiler = find_compiler()
    if not compiler:
        print("[HOST] no host C++ compiler found (g++/clang++/ziglang)")
        return 2

    exe = build(compiler, args.force)
    proc = subprocess.run([str(exe)])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
