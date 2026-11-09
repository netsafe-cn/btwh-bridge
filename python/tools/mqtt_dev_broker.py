#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mqtt_dev_broker.py - a throwaway MQTT broker for the bridge's development loop.

The firmware needs somewhere to publish before Home Assistant is involved. This starts
amqtt on 0.0.0.0:1883 with authentication off, so a serial console can watch `btwh/#`
traffic with any client.

    python python/tools/mqtt_dev_broker.py                 # port 1883
    python python/tools/mqtt_dev_broker.py --port 1884     # another port
    python python/tools/mqtt_dev_broker.py --quiet         # warnings only

This is a development fixture, not a deployment: no TLS, no authentication, no persistence.
Anything reachable on the LAN can publish `set/temperature` while it runs. For a real
installation use Mosquitto or the broker that ships with Home Assistant.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys

try:
    from amqtt.broker import Broker
except ImportError:                                        # pragma: no cover - dev only
    sys.exit("amqtt is not installed: python -m pip install amqtt")

HERE = pathlib.Path(__file__).resolve().parent


def build_config(port: int) -> dict:
    """Anonymous, in-memory, single listener. Kept in code so the YAML cannot drift.

    amqtt 0.12 moved plugin configuration to a mapping keyed by class path, and the default
    YAML still uses hyphenated keys that the dataclass loader rejects. Both facts were found
    by running it, so the shape below is the working one for 0.12.
    """
    return {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"0.0.0.0:{port}",
                "max_connections": 50,
            }
        },
        # No sys plugin on purpose: $SYS chatter buries the btwh/# traffic being read.
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
        },
    }


async def serve(port: int) -> None:
    broker = Broker(build_config(port))
    await broker.start()
    print(f"[broker] listening on 0.0.0.0:{port}  (anonymous, no TLS - LAN use only)")
    print("[broker] Ctrl-C to stop")
    try:
        await asyncio.Event().wait()                       # run until interrupted
    finally:
        await broker.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--quiet", action="store_true", help="only warnings and errors")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
