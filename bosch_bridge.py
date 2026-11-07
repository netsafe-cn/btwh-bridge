# -*- coding: utf-8 -*-
"""bosch_bridge.py —— 无界面的后台桥接（BLE ↔ 巴法云）

适合挂在开机自启/服务里跑，不需要窗口。
和 bosch_ui.py 共用同一套底层（bosch_api + bemfa），逻辑只有一份。

用法:
    python bosch_bridge.py                 # 读 config.json
    python bosch_bridge.py --uid <私钥>     # 覆盖配置里的私钥
    python bosch_bridge.py --gap 15        # 上报间隔（秒）
"""
import argparse
import sys
import time

from bemfa import BemfaClient
from bosch_api import Config, BoschService, TEMP_MIN, TEMP_MAX


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="", help="巴法云私钥（覆盖 config.json）")
    ap.add_argument("--gap", type=float, default=10.0, help="上报间隔秒")
    a = ap.parse_args()

    # 与界面共用同一把单实例锁：同时跑会互踢设备、云端指令回环
    from singlelock import acquire
    if not acquire():
        print("已有实例在运行（界面或另一个桥接），本进程退出。")
        return 2

    cfg = Config.load()
    if a.uid:
        cfg.bemfa_uid = a.uid
    if not cfg.bemfa_uid:
        print("没有巴法云私钥（config.json 或 --uid）", file=sys.stderr)
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
    log("桥接已启动，Ctrl+C 退出")

    last_report = 0.0
    last_status = ""
    try:
        while True:
            st = svc.state
            if svc.status != last_status:
                log("[设备] %s" % svc.status)
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
        log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
