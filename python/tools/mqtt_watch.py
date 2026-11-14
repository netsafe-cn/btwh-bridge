#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mqtt_watch.py - watch the bridge, or stand in for it.

Two jobs, one file, because both need the same topic knowledge:

  watch     subscribe to btwh/# and print what arrives: state, availability, attributes and the
            discovery entities. This is how the firmware's output is checked before Home
            Assistant is involved, and how a consumer bug is told apart from a bridge bug.

  demo      publish the contract's golden payload as a fake bridge, so Home Assistant, the touch
            panel or a Node-RED flow can be built while the heater is somewhere else. Without
            this, every UI change waits for a 4-second broadcast.

Examples:
    python python/tools/mqtt_watch.py --host 192.0.2.10
    python python/tools/mqtt_watch.py --demo --device btwh_eb6f5f
    python python/tools/mqtt_watch.py --host 127.0.0.1 --set 42      # publish a setpoint

Nothing here is imported by the library: it is a development tool, like the serial console.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:                                        # pragma: no cover - dev only
    sys.exit("paho-mqtt is not installed: python -m pip install paho-mqtt")

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "spec" / "mqtt.json"
PROFILES_SPEC = REPO / "spec" / "profiles.json"

# Human order: what a person looks at first when the shower is running.
COLUMNS = [
    ("temp_out", "out", "C"),
    ("temp_in", "in", "C"),
    ("chamber", "chamber", "C"),
    ("setpoint", "set", "C"),
    ("water_flow", "flow", "L/min"),
    ("burner_power", "power", "%"),
    ("fan_speed", "fan", "rpm"),
    ("flame", "flame", ""),
    ("mode", "mode", ""),
    ("fault", "fault", ""),
]


def load_spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def golden_payload(spec: dict) -> str:
    """The contract's sample, rendered with the same rules the firmware uses.

    Imported from the generator rather than reimplemented: the rounding rule (half away from
    zero) is the one place where Python and C++ can silently disagree, and this tool must show
    what the firmware would send, not what Python would.
    """
    sys.path.insert(0, str(REPO / "firmware" / "bridge" / "tools"))
    from gen_mqtt import SAMPLE, render_state      # noqa: E402 - path set above
    sample = dict(SAMPLE)
    sample["ts"] = int(time.time())
    return render_state(spec["state_fields"], sample)


def fmt_state(payload: str) -> str:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return "  (not JSON) " + payload[:100]
    parts = []
    for key, label, unit in COLUMNS:
        if key not in data:
            continue                                    # consumer_rules[0]: absent is not zero
        value = data[key]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        parts.append("%s=%s%s" % (label, value, (" " + unit) if unit else ""))
    if "ts" in data:
        age = int(time.time()) - int(data["ts"])
        parts.append("ts=%ds" % age)
    return "  " + "  ".join(parts)


def publish_discovery(c, spec: dict, device: str) -> int:
    """Publish the HA discovery topics, exactly as the firmware does on connect.

    Same source as the firmware: the list comes from gen_ha.entities(), which is what
    publishDiscovery() iterates on the board. This function used to walk state_fields and
    attributes_fields itself and had quietly fallen two entities behind - the update entity and
    the fault binary sensor - which is exactly the drift a reference publisher must not have: a
    demo advertising a different device than the bridge is worse than no demo.
    """
    sys.path.insert(0, str(REPO / "firmware" / "bridge" / "tools"))
    from gen_ha import DEV, discovery_topic, entities, render_payload   # noqa: E402
    sent = 0
    for entity in entities(spec):
        topic = discovery_topic(entity["component"], entity["key"], spec).replace(DEV, device)
        payload = render_payload(entity["config"]).replace(DEV, device)
        c.publish(topic, payload, retain=True)
        sent += 1
    return sent


def main() -> int:
    # Line-buffered even when piped: the serial console's output is often captured to a file,
    # and a tool that prints nothing until it exits looks broken.
    sys.stdout.reconfigure(line_buffering=True)

    spec = load_spec()
    base = spec["base_topic"]

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--device", help="device id, e.g. btwh_eb6f5f (default: every device)")
    ap.add_argument("--demo", action="store_true", help="publish as a fake bridge")
    ap.add_argument("--set", type=int, dest="setpoint", help="publish this setpoint and exit")
    ap.add_argument("--ota", metavar="URL",
                    help="publish a firmware URL (or 'install') to the device's ota topic and "
                         "exit (live-only: sent unretained, exactly as the firmware requires)")
    ap.add_argument("--manifest", metavar="LATEST,URL",
                    help="publish the retained update manifest (latest version, image URL) "
                         "for --device and exit; the bridge turns it into update/state")
    ap.add_argument("--interval", type=float, default=4.0, help="demo publish interval")
    ap.add_argument("--user", help="broker username (empty: anonymous)")
    ap.add_argument("--password", help="broker password (use with --user)")
    args = ap.parse_args()

    device = args.device or ("btwh_demo01" if args.demo else None)
    root = "%s/%s" % (base, device) if device else base

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="mqtt_watch" if not args.demo else device)
    if args.demo:
        # Same will as the firmware: a crash leaves "offline", a clean exit says it explicitly.
        client.will_set("%s/%s/availability" % (base, device), "offline", qos=0, retain=True)

    # Demo setpoint echo: the last value a consumer commanded. None = golden sample value.
    # The real bridge reads it back from the heater's next A0; a demo that ignores commands
    # makes Home Assistant's number entity look broken ("I set 45 and it still says 42").
    demo_setpoint = [None]

    def demo_state() -> str:
        payload = golden_payload(spec)
        if demo_setpoint[0] is not None:
            data = json.loads(payload)
            data["setpoint"] = demo_setpoint[0]
            payload = json.dumps(data, separators=(",", ":"))
        return payload

    def on_connect(c, _userdata, _flags, reason_code, _props):
        if reason_code != 0:
            print("[watch] connect refused: %s" % reason_code)
            return
        # A filter without a wildcard matches exactly one topic: `btwh/<dev>` alone hears
        # nothing. Demo needs only the command topics (its own states are printed by the loop,
        # and a wildcard would print them twice); watch mode wants the whole tree.
        if args.demo:
            c.subscribe("%s/set/temperature" % root, 0)
            c.subscribe("%s/cmd" % root, 0)
            print("[watch] subscribed %s/set/temperature + cmd" % root)
        else:
            tree = "%s/#" % root
            c.subscribe(tree, 0)
            print("[watch] subscribed %s" % tree)
        if args.demo:
            dev_root = "%s/%s" % (base, device)
            # Same shape the firmware publishes: every attributes field, including the
            # diagnostics (rssi, last disconnect), so the demo exercises the full contract.
            attrs = json.dumps({"fw": "demo", "hw": "mqtt_watch", "heater": "-",
                                "pair_state": "paired",
                                "uptime": int(time.time()) % 86400,
                                "rssi": -64,
                                "last_disconnect": "demo bridge (never drops)",
                                "last_disconnect_at": 0},
                               separators=(",", ":"))
            c.publish("%s/availability" % dev_root, "online", retain=True)
            # Freshness is the second signal, and omitted it is not neutral: the measurements
            # bind availability + link with availability_mode all (ADR-014), so a demo that
            # never publishes link advertises a device whose numbers Home Assistant refuses to
            # show. Retained, like the firmware's.
            c.publish("%s/link" % dev_root, "online", retain=True)
            # Readings are live-only (spec retain_policy, ADR-015): a retained demo state
            # would outlive the process and keep answering after Ctrl-C.
            c.publish("%s/attributes" % dev_root, attrs, retain=False)
            n = publish_discovery(c, spec, device)
            print("[demo] discovery: %d entities published" % n)
            payload = demo_state()
            c.publish("%s/state" % dev_root, payload, retain=False)
            print("[demo] %s" % payload)
            print("[demo] publishing every %.1fs; Ctrl-C to stop" % args.interval)

    def on_message(_c, _userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace")
        if topic.endswith("/state"):
            print("[state] %s" % fmt_state(payload))
        elif topic.endswith("/availability"):
            print("[avail] %s" % payload)
        elif topic.endswith("/attributes"):
            print("[attrs] %s" % payload)
            if args.setpoint is not None:
                cmd_topic = "%s/%s/set/temperature" % (base, device)
                _c.publish(cmd_topic, str(args.setpoint))
                print("[set  ] %s <- %d" % (cmd_topic, args.setpoint))
        elif topic.endswith("/config"):
            print("[ha   ] %s" % topic)
        elif "/set/" in topic or topic.endswith("/cmd"):
            # A consumer (HA, the panel) commanding the bridge: the whole point of the demo.
            print("[cmd  ] %s <- %s" % (topic, payload))
            if topic.endswith("/set/temperature") and args.demo:
                if payload[:1].isspace():
                    # The firmware's rules::parseSetpoint refuses leading whitespace (no
                    # legitimate consumer sends it); the demo rehearses the firmware.
                    print("[demo] setpoint leading whitespace, ignored: %r" % payload)
                else:
                    try:
                        value = float(payload)
                    except ValueError:
                        print("[demo] setpoint not a number, ignored: %r" % payload)
                    else:
                        # Whole degrees only, mirroring the firmware's contract; bounds come
                        # from the model profile so this demo cannot disagree with the real
                        # bridge. Out-of-range is REFUSED, not clamped, exactly like
                        # rules::parseSetpoint: the demo exists to rehearse the bridge's
                        # behaviour, and the bridge never silently turns a 70 into a 60.
                        if value != int(value):
                            print("[demo] setpoint not a whole degree, ignored: %r" % payload)
                        else:
                            profiles = json.loads(PROFILES_SPEC.read_text(encoding="utf-8"))
                            row = next(p for p in profiles["profiles"]
                                       if p["id"] == profiles["default_profile"])
                            lo = row["setpoint"]["min"]
                            hi = row["setpoint"]["max"]
                            if not (lo <= value <= hi):
                                print("[demo] setpoint out of range %d..%d, ignored: %r"
                                      % (lo, hi, payload))
                            else:
                                demo_setpoint[0] = int(value)
                                print("[demo] setpoint accepted -> %d (echoed from next state)"
                                      % int(value))

    client.on_connect = on_connect
    client.on_message = on_message
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.connect(args.host, args.port, 30)
    client.loop_start()

    try:
        if args.demo and args.setpoint is None:
            attrs_at = time.time()      # attributes heartbeat: same 60 s cadence as the firmware
            while True:
                time.sleep(args.interval)
                payload = demo_state()
                client.publish("%s/%s/state" % (base, device), payload, retain=False)
                print("[state] %s" % fmt_state(payload))
                if time.time() - attrs_at >= 60:
                    attrs_at = time.time()
                    attrs = json.dumps({"fw": "demo", "hw": "mqtt_watch", "heater": "-",
                                        "pair_state": "paired",
                                        "uptime": int(time.time()) % 86400,
                                        "rssi": -64,
                                        "last_disconnect": "demo bridge (never drops)",
                                        "last_disconnect_at": 0},
                                       separators=(",", ":"))
                    client.publish("%s/%s/attributes" % (base, device), attrs, retain=False)
                    print("[attrs] %s" % attrs)
        elif args.setpoint is not None:
            time.sleep(1.0)          # let the subscription settle so attributes arrive
        elif args.ota:
            if not args.device:
                print("[ota  ] --ota needs --device: two bridges, two otas")
                return 2
            time.sleep(1.0)          # let the connection settle before the one live shot
            ota_topic = "%s/%s/ota" % (base, args.device)
            client.publish(ota_topic, args.ota, qos=0, retain=False)
            print("[ota  ] %s <- %s (unretained on purpose: a retained URL would "
                  "reflash on every reconnect)" % (ota_topic, args.ota))
        elif args.manifest:
            if not args.device:
                print("[manif] --manifest needs --device")
                return 2
            latest, _, url = args.manifest.partition(",")
            time.sleep(1.0)
            # Retained is the point: a rebooted bridge re-learns the manifest from the
            # broker, and the HA update entity follows the bridge's own update/state.
            client.publish("%s/%s/update/latest" % (base, args.device), latest, retain=True)
            client.publish("%s/%s/update/url" % (base, args.device), url, retain=True)
            print("[manif] %s/%s/update/latest <- %s (retained)"
                  % (base, args.device, latest))
            print("[manif] %s/%s/update/url <- %s (retained)"
                  % (base, args.device, url))
        else:
            print("[watch] Ctrl-C to stop")
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if args.demo:
            # Leave the retained status honest: a stale "online" outlives this process, and
            # both signals have to go or the measurements stay available on the broker's word.
            client.publish("%s/%s/availability" % (base, device), "offline", retain=True)
            client.publish("%s/%s/link" % (base, device), "offline", retain=True)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
