#!/usr/bin/env python3
"""Generate docs/diagrams/architecture-{light,dark}.svg.

One description, two colour schemes: GitHub picks the right one with
<picture> + prefers-color-scheme, so the diagram stays readable in both themes
without shipping two hand-drawn copies that drift apart.

    python docs/diagrams/gen_architecture.py          # write both files
    python docs/diagrams/gen_architecture.py --check  # CI: fail if stale

The content here mirrors what the firmware actually does; if a layer, a topic
or the entity count changes, change it here and re-run rather than editing SVG
by hand.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

W, H = 1600, 730
OUT = pathlib.Path(__file__).resolve().parent

SANS = ("font-family=\"-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif\"")
MONO = ("font-family=\"SFMono-Regular,Consolas,'Liberation Mono',Menlo,"
        "monospace\"")

THEMES = {
    "light": {
        "bg": "#ffffff", "frame": "#d8dee4",
        "card": "#f6f8fa", "card_stroke": "#d0d7de",
        "chip": "#ffffff", "chip_stroke": "#d0d7de",
        "head": "#1f2328", "body": "#48505a", "muted": "#87909a",
        "ble": "#bc4c00", "mqtt": "#0969da", "ha": "#8250df", "ok": "#1a7f37",
        "arrow": "#5c6570",
    },
    "dark": {
        "bg": "#0d1117", "frame": "#30363d",
        "card": "#161b22", "card_stroke": "#30363d",
        "chip": "#1c2128", "chip_stroke": "#30363d",
        "head": "#e6edf3", "body": "#aeb7c2", "muted": "#7d8590",
        "ble": "#db6d28", "mqtt": "#4493f8", "ha": "#a371f7", "ok": "#3fb950",
        "arrow": "#8b949e",
    },
}


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Svg:
    def __init__(self, t: dict):
        self.t = t
        self.parts: list[str] = []

    def add(self, s: str) -> None:
        self.parts.append(s)

    # ---- primitives -------------------------------------------------------
    def rect(self, x, y, w, h, fill, stroke=None, rx=12, sw=1.5, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                 f'fill="{fill}"{s}{d}/>')

    def text(self, x, y, s, size=13, fill=None, weight="400", mono=False,
             anchor="start", opacity=1.0):
        fill = fill or self.t["body"]
        fam = MONO if mono else SANS
        op = f' opacity="{opacity}"' if opacity != 1.0 else ""
        self.add(f'<text x="{x}" y="{y}" {fam} font-size="{size}" '
                 f'font-weight="{weight}" fill="{fill}" '
                 f'text-anchor="{anchor}"{op}>{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, color, w=1.5, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="{color}" stroke-width="{w}" stroke-linecap="round"{d}/>')

    def arrow(self, x1, y1, x2, y2, color, w=2, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                 f'stroke-width="{w}" stroke-linecap="round" marker-end="url(#tip)"{d}/>')

    def dot(self, cx, cy, r=3.2, fill=None):
        self.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill or self.t["body"]}"/>')

    # ---- composites -------------------------------------------------------
    def card(self, x, y, w, h, accent, title, sub=None):
        t = self.t
        self.rect(x, y, w, h, t["card"], t["card_stroke"])
        self.line(x + 18, y + 2, x + w - 18, y + 2, accent, w=3)
        self.text(x + 22, y + 38, title, size=17, fill=t["head"], weight="600")
        if sub:
            self.text(x + 22, y + 60, sub, size=12, fill=t["muted"])

    def bullets(self, x, y, items, size=12.5, lh=30, color=None):
        for i, it in enumerate(items):
            self.dot(x, y + i * lh - 4, 2.8, color or self.t["muted"])
            self.text(x + 14, y + i * lh, it, size=size, fill=self.t["body"])

    def chip(self, x, y, w, h, name, desc, accent):
        t = self.t
        self.rect(x, y, w, h, t["chip"], t["chip_stroke"], rx=8, sw=1)
        self.text(x + 12, y + 21, name, size=12, fill=accent, weight="600", mono=True)
        self.text(x + 12, y + 38, desc, size=10.5, fill=t["muted"])

    def step(self, x, y, w, h, n, title, note, accent):
        t = self.t
        self.rect(x, y, w, h, t["chip"], t["chip_stroke"], rx=8, sw=1)
        self.add(f'<circle cx="{x + 20}" cy="{y + 22}" r="10" fill="none" '
                 f'stroke="{accent}" stroke-width="1.5"/>')
        self.text(x + 20, y + 26.5, str(n), size=11, fill=accent, weight="600",
                  anchor="middle", mono=True)
        self.text(x + 38, y + 27, title, size=11.5, fill=t["head"], weight="600")
        self.text(x + 14, y + 52, note, size=10, fill=t["muted"])


def build(theme: str) -> str:
    t = THEMES[theme]
    s = Svg(t)

    # --- header ------------------------------------------------------------
    s.text(36, 32, "btwh-bridge  ·  runtime architecture", size=13,
           fill=t["muted"], weight="600")
    s.text(1564, 32, "no cloud in the loop", size=12, fill=t["ok"],
           weight="600", anchor="end")

    # --- band 1: heater -> bridge -> broker -> home assistant ---------------
    y1, h1 = 60, 272

    # heater
    hx, hw = 36, 232
    s.card(hx, y1, hw, h1, t["ble"], "Therm 4800i", "Bosch gas water heater")
    s.bullets(hx + 22, 172, [
        "BLE, advertises BTWH-001-…",
        "SMP pairing, PIN = panel code",
        "A2/07 MAC, A2/08 code",
        "A0 frame every ~4 s",
        "11 live measurements",
    ])

    # bridge
    bx, bw = 316, 480
    s.card(bx, y1, bw, h1, t["ok"], "ESP32-S3 bridge",
           "always on, one binary, no display")
    chips = [
        ("src/proto/", "frames, stuffing, A0 fields", t["ok"]),
        ("src/ble/", "NimBLE: the only place it appears", t["ble"]),
        ("src/auth/", "A2/07 + A2/08 behind a Transport", t["ble"]),
        ("src/app/", "session order, NVS config", t["ok"]),
        ("src/net/", "Wi-Fi, MQTT, discovery, OTA", t["mqtt"]),
        ("main.cpp", "setup, loop, serial console", t["ok"]),
    ]
    cw, ch, gap = 218, 48, 12
    for i, (name, desc, accent) in enumerate(chips):
        col, row = i % 2, i // 2
        s.chip(bx + 22 + col * (cw + 12), 138 + row * (ch + gap), cw, ch,
               name, desc, accent)

    # broker
    mx, mw = 844, 232
    s.card(mx, y1, mw, h1, t["mqtt"], "MQTT broker",
           "Mosquitto on HAOS, port 1883")
    for i, tp in enumerate([
        "btwh/<id>/state",
        "btwh/<id>/attributes",
        "btwh/<id>/availability",
        "btwh/<id>/set/temperature",
        "btwh/<id>/cmd",
        "btwh/<id>/ota",
        "btwh/<id>/update/*",
    ]):
        s.text(mx + 22, 152 + i * 22, tp, size=11, fill=t["body"], mono=True)

    # home assistant
    ax, aw = 1124, 440
    s.card(ax, y1, aw, h1, t["ha"], "Home Assistant", "auto-discovery, no YAML")
    s.bullets(ax + 22, 172, [
        "22 entities on one device",
        "water heater + number 35-60 C",
        "update entity: Install button",
        "LWT to unavailable in ~1 s",
        "setpoint round-trips, non-optimistic",
    ])

    # arrows in band 1
    for x_from, x_to, label in (
        (hx + hw, bx, "BLE"),
        (bx + bw, mx, "MQTT"),
        (mx + mw, ax, "MQTT"),
    ):
        s.arrow(x_from + 6, y1 + 136, x_to - 6, y1 + 136, t["arrow"])
        s.text((x_from + x_to) / 2, y1 + 124, label, size=10.5, fill=t["muted"],
               mono=True, anchor="middle")

    # --- band 2: reference implementation and the update loop ---------------
    y2, h2 = 430, 260

    # python reference implementation
    px, pw = 36, 380
    s.card(px, y2, pw, h2, t["ble"], "Python reference",
           "pip install btwh-bridge  ·  Python 3.9+")
    s.bullets(px + 22, 530, [
        "same protocol as the firmware",
        "shares spec/protocol_vectors.json",
        "desktop console + Bemfa bridge",
        "capture, log_diag: bench tools",
        "237 offline tests",
    ])

    # update loop
    ux, uw = 464, 1100
    s.card(ux, y2, uw, h2, t["ok"], "Remote update",
           "the bridge reflashes itself; a bad image never touches the running firmware")
    steps = [
        ("manifest", "retained on the broker", t["mqtt"]),
        ("update entity",  "shows latest vs installed", t["ha"]),
        ("btwh/<id>/ota", "live-only: a replay is a bug", t["mqtt"]),
        ("HTTP GET", "2 KB chunks, 120 s budget", t["ble"]),
        ("reboot", "checksum-gated, then restart", t["ok"]),
    ]
    sw = 202
    for i, (title, note, accent) in enumerate(steps):
        s.step(ux + 24 + i * (sw + 10), 520, sw, 74, i + 1, title, note, accent)
        if i < len(steps) - 1:
            s.arrow(ux + 24 + i * (sw + 10) + sw + 1, 557,
                    ux + 24 + (i + 1) * (sw + 10) - 2, 557, t["arrow"], w=1.5)
    s.text(ux + 24, 640,
           "One flasher, two triggers: the MQTT topic and the HA button. The serial command only prints the URL — no second parser to keep honest.",
           size=11, fill=t["muted"])

    # dashed arrows: python -> heater (bench), update loop -> bridge and HA
    s.arrow(226, y2 - 6, 226, y1 + h1 + 8, t["arrow"], w=1.8, dash="5 5")
    s.text(236, y2 - 20, "BLE, from a laptop", size=10.5, fill=t["muted"])
    s.arrow(560, y2 - 6, 560, y1 + h1 + 8, t["arrow"], w=1.8, dash="5 5")
    s.text(570, y2 - 20, "pulls the image", size=10.5, fill=t["muted"])
    s.arrow(1360, y2 - 6, 1360, y1 + h1 + 8, t["arrow"], w=1.8, dash="5 5")
    s.text(1370, y2 - 20, "Install button", size=10.5, fill=t["muted"])

    body = "\n  ".join(s.parts)
    return f'''<!-- generated by docs/diagrams/gen_architecture.py ({theme}) -->
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"
     viewBox="0 0 {W} {H}" role="img"
     aria-label="btwh-bridge runtime architecture: a Bosch Therm 4800i water heater over BLE into an ESP32-S3 bridge, out over MQTT to Home Assistant, with a separate remote-update path">
  <defs>
    <marker id="tip" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse"
            orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="{t['arrow']}"/>
    </marker>
  </defs>
  <rect x="0" y="0" width="{W}" height="{H}" rx="16" fill="{t['bg']}"
        stroke="{t['frame']}" stroke-width="1"/>
  {body}
</svg>
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed files differ from the output")
    args = ap.parse_args()

    stale = []
    for theme in THEMES:
        path = OUT / f"architecture-{theme}.svg"
        svg = build(theme)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != svg:
                stale.append(path.name)
        else:
            path.write_text(svg, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(OUT.parent.parent)}")

    if stale:
        print("stale: " + ", ".join(stale) + " (run gen_architecture.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
