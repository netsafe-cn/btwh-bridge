#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Assert two things about a built image that the compiler cannot: it still fits, and it is
called what the release says it is.

    python firmware/bridge/tools/check_image.py                 # version + size, default paths
    python firmware/bridge/tools/check_image.py --tag v2.9.0    # only when tagging

Why this exists: `--build --flash` on a desk prints `Sketch uses 1181545 bytes (35%) of program
storage` and a human reads it. Nobody reads it at 2 a.m., and the day the image crosses the slot
boundary the failure is not a compiler error - arduino-cli is happy, the flash succeeds, and the
board either refuses the OTA or boots nothing. That belongs in CI, where it is a red job.

The version check is the other half. `kFirmwareVersion` is a string literal in a header and the
git tag is a string in another system; nothing links them, and a release that ships the previous
number is invisible until someone reads the banner. When the ref is a tag, they must agree.
"""
from __future__ import annotations
import argparse, os, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent        # firmware/bridge
DEFAULT_HEADER = ROOT / "src" / "net" / "mqtt_client.h"
DEFAULT_BIN = pathlib.Path(os.environ.get("BTWH_WORK") or (pathlib.Path.home() / "esp")) \
    / "build" / "arduino_sketch.ino.bin"

# default_8MB.csv, which gateway_cli.py pins on every board: app0 at 0x10000, app1 at 0x340000,
# otadata between them. The slot is the gap, not the named partition size, because an image that
# only just fits its own row leaves no room for the OTA magic the updater writes after it.
#
# The same number is printed by arduino-cli as `Maximum is 3342336 bytes`, so this constant is
# cross-checkable from any build log rather than being folklore.
SLOT_BYTES = 0x340000 - 0x10000

# Comfort ceiling, not a hardware limit. The image was 1,181,545 B on 2026-11-13 with the OTA
# flasher already in it; the remaining headroom is what a TLS client and a manifest verifier will
# want. Crossing this is a warning, not a build failure - only the slot is fatal.
BUDGET_BYTES = 1_500_000


def read_version(header_text):
    """Pull the version literal out of mqtt_client.h, or None when it is not there."""
    hit = re.search(r'kFirmwareVersion\s*=\s*"([^"]*)"', header_text)
    return hit.group(1) if hit else None


def tag_version(ref):
    """Turn a release ref into the version it claims, or None when this is not a tag.

    Accepts `refs/tags/v2.9.0`, `refs/tags/2.9.0` and a bare `v2.9.0`, because the workflow and
    a human at a shell pass different spellings of the same thing.
    """
    if not ref:
        return None
    name = ref.rstrip("/").rsplit("/", 1)[-1]
    if not (ref.startswith("refs/tags/") or re.match(r"^v?\d", name)):
        return None
    return name[1:] if name[:1] in ("v", "V") else name


def check_version(version, tag):
    """(ok, message). A tag is a promise about the banner; a branch is not."""
    if not version:
        return False, "no kFirmwareVersion literal found in the header"
    if tag is None:
        return True, "version %s (not a tag, nothing to compare)" % version
    # A tag may name the release line (`v2.9.0`) while the banner carries the build (`2.9.0-ota11`),
    # so the tag must be a prefix of the version, not its equal.
    if version == tag or version.startswith(tag + "-"):
        return True, "version %s matches tag %s" % (version, tag)
    return False, "tag says %s, firmware says %s" % (tag, version)


def check_size(size, slot=SLOT_BYTES, budget=BUDGET_BYTES):
    """(ok, message) for one image size in bytes."""
    if size > slot:
        return False, ("image is %d B, larger than the %d B app slot: the flash would succeed "
                       "and the board would boot nothing" % (size, slot))
    pct = 100.0 * size / slot
    if size > budget:
        return True, ("image is %d B (%.1f%% of the slot) - over the %d B comfort budget; the "
                      "rest of the slot is the only room left for TLS"
                      % (size, pct, budget))
    return True, "image is %d B (%.1f%% of the slot, budget %d B)" % (size, pct, budget)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", default=str(DEFAULT_HEADER))
    ap.add_argument("--bin", default=str(DEFAULT_BIN))
    ap.add_argument("--tag", default=os.environ.get("GITHUB_REF", ""),
                    help="release tag to compare against; defaults to GITHUB_REF")
    a = ap.parse_args(argv)

    header = pathlib.Path(a.header)
    if not header.is_file():
        print("no header at %s" % header)
        return 2
    ok, msg = check_version(read_version(header.read_text(encoding="utf-8")), tag_version(a.tag))
    print(("OK: " if ok else "FAIL: ") + msg)
    if not ok:
        return 1

    binary = pathlib.Path(a.bin)
    if not binary.is_file():
        # Not a failure: this runs on the tag path too, where the build may be a separate job.
        print("no image at %s; size not checked" % binary)
        return 0
    ok, msg = check_size(binary.stat().st_size)
    print(("OK: " if ok else "FAIL: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
