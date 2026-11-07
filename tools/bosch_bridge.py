# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""bosch_bridge.py - headless bridge (BLE <-> Bemfa cloud)

Meant for a startup entry or a service, with no window at all.
Shares its core with tools/bosch_ui.py (bosch_api + bemfa); the logic exists once.

Usage:
    python tools/bosch_bridge.py                # read config.json
    python tools/bosch_bridge.py --uid <key>    # override the key from the config
    python tools/bosch_bridge.py --gap 15       # reporting interval in seconds
"""
import argparse
import sys
import time

from bemfa import BemfaClient
from bosch_api import Config, BoschService, TEMP_MIN, TEMP_MAX


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="", help="Bemfa key (overrides config.json)")
    ap.add_argument("--gap", type=float, default=10.0, help="reporting interval, seconds")
    a = ap.parse_args()

    # Shares the single-instance lock with the console: both running evicts the device
    from singlelock import acquire
    if not acquire():
        print("another instance is running (console or another bridge); exiting.")
        return 2

    cfg = Config.load()
    if a.uid:
        cfg.bemfa_uid = a.uid
    if not cfg.bemfa_uid:
        print("no Bemfa key (config.json or --uid)", file=sys.stderr)
        return 1

    def log(s):
        print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)

    svc = BoschService(cfg)
    svc.on_state = lambda st: None

    bemfa = BemfaClient(cfg.bemfa_uid,
                        [cfg.topic_set, cfg.topic_up, cfg.topic_down])

    def on_command(topic, msg):
        low = str(msg).strip().lower()
        st = svc.state
        if topic == cfg.topic_up and low in ("on", "1"):
            if st and st.setpoint is not None:
                svc.set_temperature(st.setpoint + 1)
            bemfa.push(topic, "off")
        elif topic == cfg.topic_down and low in ("on", "1"):
            if st and st.setpoint is not None:
                svc.set_temperature(st.setpoint - 1)
            bemfa.push(topic, "off")
        elif topic == cfg.topic_set:
            raw = str(msg).strip()
            if "#" in raw:
                parts = [p for p in raw.split("#") if p.strip()]
                raw = parts[0] if parts else ""
            try:
                v = float(raw)
            except ValueError:
                return
            if TEMP_MIN <= v <= TEMP_MAX:
                svc.set_temperature(int(v))

    bemfa.on_command = on_command
    bemfa.on_log = log
    svc.start()
    bemfa.start()
    log("bridge started, Ctrl+C to stop")

    last_report = 0.0
    last_status = ""
    try:
        while True:
            st = svc.state
            if svc.status != last_status:
                log("[device] %s" % svc.status)
                last_status = svc.status
            if st and st.setpoint is not None \
                    and time.time() - last_report >= a.gap:
                last_report = time.time()
                bemfa.push(cfg.topic_set, "#%d" % st.setpoint)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bemfa.stop()
        svc.stop()
        log("exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
