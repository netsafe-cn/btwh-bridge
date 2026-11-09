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
    python python/tools/mqtt_watch.py --host 192.168.110.138
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
    ap.add_argument("--interval", type=float, default=4.0, help="demo publish interval")
    args = ap.parse_args()

    device = args.device or ("btwh_demo01" if args.demo else None)
    root = "%s/%s" % (base, device) if device else "%s/#" % base

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="mqtt_watch" if not args.demo else device)

    def on_connect(c, _userdata, _flags, reason_code, _props):
        if reason_code != 0:
            print("[watch] connect refused: %s" % reason_code)
            return
        c.subscribe(root, 0)
        print("[watch] subscribed %s" % root)
        if args.demo:
            payload = golden_payload(spec)
            c.publish("%s/availability" % ("%s/%s" % (base, device)), "online", retain=True)
            c.publish("%s/state" % ("%s/%s" % (base, device)), payload, retain=True)
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

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, 30)
    client.loop_start()

    try:
        if args.demo and args.setpoint is None:
            payload = golden_payload(spec)
            while True:
                time.sleep(args.interval)
                payload = golden_payload(spec)
                client.publish("%s/%s/state" % (base, device), payload, retain=True)
                print("[state] %s" % fmt_state(payload))
        elif args.setpoint is not None:
            time.sleep(1.0)          # let the subscription settle so attributes arrive
        else:
            print("[watch] Ctrl-C to stop")
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
