# -*- coding: utf-8 -*-
"""device_fsm.py —— ESP32 桥接器「配网 / 配对」状态机（可执行规范）

框架无关：不依赖 Arduino / ESP-IDF / BLE / WiFi 任何 API，可在 PC 的 CI 里
跑全部分支。C++ 固件按本文件的状态与转换规则实现，Python 侧保持同构。

核心设计决策
------------
「配网」与「配对」是**两个正交流程**，绝不能合并成一个「配对模式」开关：

  配网 provisioning : ESP32 拿到 WiFi SSID / 密码      → 网络侧，一次性，凭证持久化
  配对 pairing      : 热水器接受 ESP32 的 BLE 链路授权  → 设备侧，可反复，短时效 6 位码

耦合的后果很具体：换路由器就得跑到热水器面板再「清除配对」一次，
或者热水器掉线就得重新输 WiFi 密码。二者必须能独立重入、独立恢复。

状态轴
------
  WifiState : UNPROVISIONED → PROVISIONING → CONNECTING → CONNECTED
              CONNECTING → FAILED → (退避) → CONNECTING
  PairState : IDLE → WINDOW_WAIT → PIN_READY → BONDING → LOGIN → ONLINE
              任意态 → FAILED → IDLE（重取码即可重试）

对外组合模式 DeviceMode：
  SETUP    无 WiFi 凭证 / 用户主动请求配网
  NORMAL   已配网，正常跑 BLE 桥接
  RECOVERY WiFi 连续失败，开 AP 让人重配，**不清掉热水器绑定**

物理约束（实现必须遵守，不是设计偏好）
  1. 配对窗口只能由人在热水器面板【清除配对】打开，程序无法远程触发。
  2. 6 位码短时效且每次不同：运行时输入，不能编译进固件、不能当长期凭证。
  3. 面板码同时是 BLE SMP passkey 与应用层 A2/08 密码（2026-11-01 实测）。
  4. WiFi 凭证、热水器参数、NimBLE bond 分属不同 NVS namespace；
     清 WiFi 时绝不能顺手删 bond，否则又要去面板重新配对。

安全
  snapshot() 是给 MQTT / HA 上报用的，**绝不输出 pin 与 WiFi 密码**。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class WifiState(str, Enum):
    UNPROVISIONED = "unprovisioned"
    PROVISIONING = "provisioning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class PairState(str, Enum):
    IDLE = "idle"
    WINDOW_WAIT = "window_wait"
    PIN_READY = "pin_ready"
    BONDING = "bonding"
    LOGIN = "login"
    ONLINE = "online"
    FAILED = "failed"


class DeviceMode(str, Enum):
    SETUP = "setup"
    NORMAL = "normal"
    RECOVERY = "recovery"


# NVS 键空间契约：C++ 固件与 Python 工具必须用同一套名字
NVS_LAYOUT = {
    "wifi": {"namespace": "bosch_wifi", "keys": ["ssid", "pass"]},
    "heater": {"namespace": "bosch_heater",
               "keys": ["addr", "login_mac", "pin"]},
    # NimBLE 自行管理，禁止由配网流程删除
    "bond": {"namespace": "nimble_bond", "keys": ["*"], "managed_by": "NimBLE"},
}


@dataclass
class FsmConfig:
    wifi_fail_limit: int = 3          # 连续失败几次进 RECOVERY
    pair_window_sec: float = 60.0     # 面板配对窗口，超时需重取码
    pin_len: int = 6


class BridgeFsm:
    """纯逻辑状态机；所有时间由外部 tick(now) 注入，方便确定性单测。"""

    def __init__(self, cfg: Optional[FsmConfig] = None):
        self.cfg = cfg or FsmConfig()
        self.wifi = WifiState.UNPROVISIONED
        self.pair = PairState.IDLE
        self.mode = DeviceMode.SETUP
        self.ssid: Optional[str] = None
        self.has_bond = False
        self.has_pin = False
        self._pin: Optional[str] = None
        self.wifi_fail = 0
        self.pin_deadline: Optional[float] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ 启动
    def on_boot(self, has_wifi: bool, has_bond: bool,
                has_pin: bool) -> DeviceMode:
        """上电恢复。凭证齐全直接进 NORMAL，不要求用户重新输码。"""
        self.has_bond = has_bond
        self.has_pin = has_pin
        if has_wifi:
            self.wifi = WifiState.CONNECTING
            self.mode = DeviceMode.NORMAL
            # 已绑定且仍存着码：跳过 SMP 配对，直接走应用层登录
            self.pair = PairState.PIN_READY if (has_bond and has_pin) else PairState.IDLE
        else:
            self.wifi = WifiState.UNPROVISIONED
            self.mode = DeviceMode.SETUP
            self.pair = PairState.IDLE
        return self.mode

    # ------------------------------------------------------------ 配网轴
    def request_setup(self) -> DeviceMode:
        """长按 BOOT / 串口命令 / Web 按钮：主动进配网，不影响配对轴。"""
        self.mode = DeviceMode.SETUP
        self.wifi = WifiState.PROVISIONING
        self.wifi_fail = 0
        self.last_error = None
        return self.mode

    def provisioning_done(self, ssid: str) -> WifiState:
        if not ssid:
            raise ValueError("SSID 不能为空")
        self.ssid = ssid
        self.wifi = WifiState.CONNECTING
        self.wifi_fail = 0
        if self.mode == DeviceMode.SETUP:
            self.mode = DeviceMode.NORMAL
        return self.wifi

    def wifi_result(self, ok: bool) -> DeviceMode:
        if ok:
            self.wifi = WifiState.CONNECTED
            self.wifi_fail = 0
            self.last_error = None
            if self.mode == DeviceMode.RECOVERY:
                self.mode = DeviceMode.NORMAL
            return self.mode
        self.wifi = WifiState.FAILED
        self.wifi_fail += 1
        self.last_error = "WiFi 连接失败"
        if self.wifi_fail >= self.cfg.wifi_fail_limit:
            self.mode = DeviceMode.RECOVERY
            self.wifi = WifiState.PROVISIONING   # 开 AP 等重新配网
        return self.mode

    def wifi_retry(self) -> WifiState:
        """退避结束后重试；RECOVERY 下不自动退出，等用户重新提交凭证。"""
        if self.mode == DeviceMode.RECOVERY:
            return self.wifi
        self.wifi = WifiState.CONNECTING
        return self.wifi

    def reset_wifi_only(self) -> DeviceMode:
        """换路由器：只清 WiFi，保留 bond / pin（正交性的关键体现）。"""
        self.ssid = None
        self.wifi_fail = 0
        self.wifi = WifiState.PROVISIONING
        self.mode = DeviceMode.SETUP
        # 故意不动 self.pair / self.has_bond / self._pin
        return self.mode

    # ------------------------------------------------------------ 配对轴
    def request_pairing(self, now: float) -> PairState:
        """用户点『开始配对』→ 等他在面板清除配对并读到码。"""
        self.pair = PairState.WINDOW_WAIT
        self.pin_deadline = now + self.cfg.pair_window_sec
        self.last_error = None
        return self.pair

    def submit_pin(self, code: str, now: float) -> PairState:
        """输入 6 位码 → 立即发起 BLE 连接 + SMP（码同时是 passkey）。"""
        if not isinstance(code, str):
            raise ValueError("配对码应为 %d 位数字" % self.cfg.pin_len)
        code = code.strip()
        if not code.isdigit() or len(code) != self.cfg.pin_len:
            raise ValueError("配对码应为 %d 位数字" % self.cfg.pin_len)
        if self.pin_deadline is not None and now > self.pin_deadline:
            self.pair = PairState.FAILED
            self.last_error = "配对窗口已超时，请到面板重新取码"
            return self.pair
        self._pin = code
        self.has_pin = True
        self.pair = PairState.BONDING
        return self.pair

    def smp_result(self, encrypted: bool) -> PairState:
        """SMP 认证完成回调。"""
        if encrypted:
            self.has_bond = True
            self.pair = PairState.LOGIN
            self.last_error = None
        else:
            self.pair = PairState.FAILED
            self.last_error = "SMP 认证失败：码错 / 窗口已关 / 手机 App 占用"
        return self.pair

    def login_result(self, ok: bool) -> PairState:
        """A2/07 + A2/08 结果；成功即开始收 A0 广播。"""
        if ok:
            self.pair = PairState.ONLINE
            self.pin_deadline = None
            self.last_error = None
        else:
            self.pair = PairState.FAILED
            self.last_error = "应用层登录失败：A2/08 应答非 01"
        return self.pair

    def pair_retry(self) -> PairState:
        """失败后回到 IDLE，可重取码再来；不清 WiFi、不清 bond。"""
        self.pair = PairState.IDLE
        self._pin = None
        self.has_pin = False
        self.pin_deadline = None
        return self.pair

    def tick(self, now: float) -> PairState:
        """配对窗口超时检查。ONLINE 之后不再受窗口约束。"""
        if (self.pair in (PairState.WINDOW_WAIT, PairState.PIN_READY,
                          PairState.BONDING, PairState.LOGIN)
                and self.pin_deadline is not None
                and now > self.pin_deadline):
            self.pair = PairState.FAILED
            self.last_error = "配对窗口已超时，请到面板重新取码"
        return self.pair

    # ------------------------------------------------------------ 全量复位
    def factory_reset(self) -> DeviceMode:
        """恢复出厂：清 WiFi、清 bond、清 pin，回到 SETUP。"""
        self.ssid = None
        self._pin = None
        self.has_pin = False
        self.has_bond = False
        self.wifi_fail = 0
        self.pin_deadline = None
        self.wifi = WifiState.UNPROVISIONED
        self.pair = PairState.IDLE
        self.mode = DeviceMode.SETUP
        self.last_error = None
        return self.mode

    # ------------------------------------------------------------ 对外上报
    def snapshot(self) -> dict:
        """给 MQTT / HA 的结构化状态。故意不含 pin 与 WiFi 密码。"""
        return {
            "mode": self.mode.value,
            "wifi": self.wifi.value,
            "pair": self.pair.value,
            "ssid": self.ssid,
            "has_bond": self.has_bond,
            "has_pin": self.has_pin,      # 只报布尔，不报值
            "wifi_fail": self.wifi_fail,
            "error": self.last_error,
        }

    def config_snapshot(self) -> dict:
        return asdict(self.cfg)


if __name__ == "__main__":
    f = BridgeFsm()
    f.on_boot(has_wifi=False, has_bond=False, has_pin=False)
    print(f.snapshot())
