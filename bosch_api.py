# -*- coding: utf-8 -*-
"""bosch_api.py —— 博世燃气热水器 BLE 客户端库

无界面依赖，可被 GUI、命令行工具、云桥接程序复用。

分层设计
    BoschProtocol   纯协议编解码（无 IO，可单元测试）
    BoschDevice     异步单次会话（连接 / 登录 / 收发 / 断开）
    BoschService    常驻服务（后台线程 + 自动重连 + 状态缓存 + 命令队列）

推荐用法
    from bosch_api import Config, BoschService

    cfg = Config.load()
    svc = BoschService(cfg)
    svc.start()

    svc.state                     # DeviceState | None
    svc.status                    # 连接状态文案
    svc.login_state               # 登录状态文案
    svc.set_temperature(42)       # 入队，异步执行
    svc.take_logs()               # 取走日志（列表）

    svc.stop()                    # 优雅退出，会先断开蓝牙

线程模型
    BoschService 内部起一个 daemon 线程跑 asyncio；对外全部是线程安全的同步调用。
    state / status 是"整体替换"式赋值，读取无需加锁。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional

from bleak import BleakClient

from boschlog import logger as LOG

# ------------------------------------------------------------------ 协议常量
SERVICE_BASE = "ff4396a6-50a8-4fc0-bcf6-228e82dfed"
CHAR_MODE = SERVICE_BASE + "b1"        # 读写，出厂 ff，不要动
CHAR_FIFO = SERVICE_BASE + "b2"        # 收发主通道
CHAR_CREDITS = SERVICE_BASE + "b3"     # 只需订阅
CHAR_COUNTER = SERVICE_BASE + "d1"     # 运行秒数

XOR_KEY = b"1ag3.-fs7"                 # 9 字节
FRAME_START = b"\x7b\x7b"
FRAME_END = b"\x7d\x7d"

SENDER = 0x21                          # 中央设备固定
RECV_BIZ = 0x00                        # 业务帧地址
RECV_LOGIN = 0xAB                      # 登录/配置帧地址

CMD_STATE = 0xA0                       # 设备主动推送的状态广播
CMD_BCAST_CFG = 0xA1                   # 配置广播周期 [type, sec]
CMD_LOGIN = 0xA2                       # 07=提交MAC 08=提交配对码
CMD_READ_PARAM = 0xD2
CMD_WRITE_PARAM = 0xD3
CMD_HISTORY = 0xB0                     # 历史数据：data=[type] 2=运行统计 3=洗浴记录
CMD_READ_ERROR = 0xD7                  # 故障列表：data=[条数]
PARAM_TYPE_SETPOINT = 0x02
PARAM_ID_SETPOINT = 0x55

CHUNK = 20                             # 单包最大负载（MTU 23 时）
TEMP_MIN, TEMP_MAX = 35, 60
IDLE_TIMEOUT = 25.0                    # 无数据多久判定链路假活
RECONNECT_BASE = 8.0                   # 会话失败后首次等待秒数（之后翻倍）
RECONNECT_MAX = 60.0                   # 重连等待上限
SESSION_STABLE_SEC = 15.0              # 会话持续多久才算"稳定"，才重置退避
SESSION_MIN_FRAMES = 2                 # 一次会话至少收到几帧 A0 才算有效

# ------------------------------------------------------ 配对/登录失败原因码
# 2026-11-07 ESP32 真机实测确认：A2/07 的应答位是「配对窗口状态」，不是「MAC 授权状态」——
#   02 = 窗口开着，要求提交码；01 = 免密授权；00 = 窗口未打开或已超时关闭。
# 把 00 当成「MAC 未授权」会误导用户去改 login_mac，实际该做的是回面板重新取码。
# 失败时优先用原因码判读，不要匹配 login_state 的中文文案（文案会改，原因码是契约）。
PAIR_FAIL_WINDOW_CLOSED = "window_closed"    # A2/07 = 00
PAIR_FAIL_CODE_REJECTED = "code_rejected"    # A2/08 = 00（窗口开着但码失效）
PAIR_FAIL_CODE_MISMATCH = "code_mismatch"    # A2/08 = 02
PAIR_FAIL_NO_RESPONSE = "no_response"        # 6s × 5 次重发耗尽
PAIR_FAIL_COOLDOWN = "cooldown"              # A2/07 = 3C / 5A 设备频率保护
PAIR_FAIL_LINK = "link_failed"               # 链路未建立

PAIR_HINTS = {
    PAIR_FAIL_WINDOW_CLOSED:
        "配对窗口未打开或已超时：面板『清除配对』→ 立刻『连接/配对』取码 → 30 秒内输入",
    PAIR_FAIL_CODE_REJECTED:
        "配对码已失效：重新取码后立即输入，取码到输码别超过 30 秒，中途不要退出配对界面",
    PAIR_FAIL_CODE_MISMATCH:
        "配对码不匹配：核对是否为当前面板显示的 6 位码（不是铭牌 S/N）",
    PAIR_FAIL_NO_RESPONSE:
        "设备无应答：确认还停留在配对界面；必要时给热水器断电重启",
    PAIR_FAIL_COOLDOWN:
        "设备连接频率保护：等 30~90 秒再试，不要连续重连",
    PAIR_FAIL_LINK:
        "链路未建立：靠近设备、按一下面板唤醒、关掉手机 App 再试",
}


# ------------------------------------------------------------------ 系统配对
# 背景（2026-11-01 血泪教训）：Windows 里删除配对后，设备侧仍记着本机地址
# 的旧链路密钥，此时以明文链路连接，设备会静默丢弃一切业务帧——
# 症状是"能连接、能读 GATT 静态值、登录零应答"。必须保持系统级配对。
# 设备走 Just Works（CONFIRM_ONLY，无需 PIN），程序可自动完成。

try:                                   # 仅 Windows 有 WinRT
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import (
        DevicePairingKinds, DevicePairingResultStatus,
        DeviceUnpairingResultStatus,
    )
    _WINRT_OK = True
except Exception:
    _WINRT_OK = False


def parse_mac(address: str) -> int:
    """'AA:BB:CC:DD:EE:FF' -> int。"""
    return int(address.replace(":", "").replace("-", ""), 16)


async def unpair_device(address: str) -> bool:
    """删除本机与设备的系统配对（配对自愈第一步）。

    场景：设备侧"清除配对/配对菜单超时"会作废链路密钥，而 Windows 侧
    仍显示 paired=True，导致带旧密钥的加密链路被设备在 GATT 层直接拒绝
    （服务枚举失败）。此时必须先删本机配对，再由 ensure_paired 重建。
    """
    if not _WINRT_OK:
        return False
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:
            return False
        p = dev.device_information.pairing
        if not p.is_paired:
            return True
        res = await p.unpair_async()
        ok = int(res.status) == int(DeviceUnpairingResultStatus.UNPAIRED)
        LOG.info("[PAIR] 解绑%s", "成功" if ok else "失败(status=%s)" % res.status)
        return ok
    except Exception as e:
        LOG.warning("[PAIR] 解绑异常: %s: %s", type(e).__name__, e)
        return False


async def paired_state(address: str) -> str:
    """返回 'paired' / 'unpaired' / 'unknown'（设备不可达或非 Windows）。"""
    if not _WINRT_OK:
        return "unknown"
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:
            return "unknown"
        return "paired" if dev.device_information.pairing.is_paired else "unpaired"
    except Exception:
        return "unknown"


async def ensure_paired(address: str, log: Optional[Callable[[str], None]] = None
                        ) -> bool:
    """确保系统级配对存在；未配对则自动以 Just Works 绑定。

    返回 True 表示"已配对或无需处理"；False 表示配对失败。
    非 Windows 平台直接返回 True（BlueZ 由系统管理）。
    """
    def say(msg):
        LOG.info("[PAIR] %s" % msg)
        if log:
            log(msg)

    if not _WINRT_OK:
        return True
    try:
        dev = await BluetoothLEDevice.from_bluetooth_address_async(
            parse_mac(address))
        if dev is None:                # 不在系统缓存（可能休眠），连接流程自会处理
            return True
        p = dev.device_information.pairing
        if p.is_paired:
            return True
        if not p.can_pair:
            say("设备当前不可配对（可能休眠或被占用）")
            return False

        custom = p.custom
        if custom is None:             # 老接口，走系统默认
            say("未配对，发起系统默认配对…")
            res = await p.pair_async()
        else:
            say("未配对，自动以 Just Works 绑定…")

            def on_req(_sender, args):
                # 设备实测只请求 CONFIRM_ONLY；出现其它方式一律拒绝，
                # 避免卡在没有 UI 可弹的场景
                from winrt.windows.devices.enumeration import DevicePairingKinds
                if args.pairing_kind == DevicePairingKinds.CONFIRM_ONLY:
                    args.accept()

            custom.add_pairing_requested(on_req)
            res = await custom.pair_async(DevicePairingKinds.CONFIRM_ONLY)

        ok = int(res.status) == int(DevicePairingResultStatus.PAIRED)
        if ok:
            say("自动配对成功")
        else:
            # 实测：设备不在配对窗口时，Just Works 可能返回失败，
            # 需要用户在面板上执行"清除配对"来打开窗口
            say("自动配对失败(status=%s)：请到热水器面板执行「清除配对」，"
                "再重新连接" % res.status)
        return ok
    except Exception as e:
        say("配对检查异常: %s: %s" % (type(e).__name__, e))
        return False

CONFIG_FILE = pathlib.Path(__file__).with_name("config.json")


# ---------------------------------------------------------------------- 配置
@dataclass
class Config:
    """运行配置。**任何敏感值都不写进代码**，只从 config.json 或环境变量来。

    环境变量优先级最高，命名规则：BOSCH_ + 字段名大写
        BOSCH_DEVICE_ADDRESS / BOSCH_LOGIN_MAC / BOSCH_PAIR_CODE / BOSCH_BEMFA_UID
    适合容器、CI 或不想把配对码落到磁盘的场景。
    """

    device_address: str = ""           # 热水器蓝牙地址，必填
    login_mac: str = ""                # 本机蓝牙地址，设备白名单用，必填
    pair_code: str = ""                # 面板配对码 / 临时码，必填
    bemfa_uid: str = ""                # 巴法云私钥，可空（空=不同步米家）
    topic_set: str = "boschset004"
    topic_up: str = "boschup006"
    topic_down: str = "boschdown006"

    ENV_PREFIX = "BOSCH_"

    @classmethod
    def load(cls, path: Optional[pathlib.Path] = None) -> "Config":
        path = path or CONFIG_FILE
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        for name in cfg.__dataclass_fields__:
            env = os.environ.get(cls.ENV_PREFIX + name.upper())
            if env:
                setattr(cfg, name, env)
        return cfg

    def save(self, path: Optional[pathlib.Path] = None):
        path = path or CONFIG_FILE
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                        "utf-8")

    def validate(self) -> List[str]:
        """返回配置问题列表，空列表表示可用。"""
        errs = []
        if not (self.device_address or "").strip():
            errs.append("未配置热水器蓝牙地址")
        if not (self.pair_code or "").strip():
            errs.append("未配置配对码")
        try:
            self.login_mac_bytes
        except ValueError:
            errs.append("本机蓝牙地址无效（应为 12 位十六进制）")
        return errs

    @property
    def login_mac_bytes(self) -> bytes:
        s = (self.login_mac or "").replace(":", "").replace("-", "").strip()
        if len(s) != 12:
            raise ValueError("本机蓝牙地址应为 12 位十六进制")
        return bytes.fromhex(s)


# ------------------------------------------------------------------ 设备状态
@dataclass
class DeviceState:
    """设备状态（字段语义依据 2026-11 实机采样，见 BLE_PROTOCOL.md §6）。

    0xA0 广播分 Type0 / Type1 两型，以 data[0] 区分；部分字段偏移不同。
    """

    frame_type: int = 0                 # 0=Type0, 1=Type1
    temp_in: Optional[float] = None     # 进水温度 data[1]+data[2]/100
    chamber: Optional[float] = None     # 炉腔（燃烧室）温度 data[3]+data[4]/100
    setpoint: Optional[int] = None      # 当前设定温度 data[5]
    temp_box: Optional[float] = None    # 机身温度 data[6]+data[7]/100
    water_flow: Optional[float] = None  # 水流量 L/min data[8]/10
    burner_power: int = 0               # 燃烧功率 % data[9]
    ionized: bool = False               # 电离（火焰）检测 data[10] & 0x10
    failure: int = 0                    # 活动故障码 data[11]，0=无故障
    fan_power: Optional[int] = None     # 风机功率 (T0:[13] T1:[12])
    fan_speed: Optional[int] = None     # 风机转速 (T0:[14] T1:[13])
    mode: Optional[int] = None          # 0待机 1燃烧 2舒适 (T0:[16] T1:[32])
    temp_out: Optional[float] = None    # 出水温度 (T0:[21]+[22] T1:[28]+[29])
    bathroom: bool = False              # 浴室控制器在位 (T0:[17]&0x80 T1:[33]&0x80)
    max_setpoint: Optional[int] = None  # 最大可设定温度
    air_flow: Optional[float] = None    # 空气流量 (仅 T1)
    air_setpoint: Optional[float] = None
    gas_setpoint: Optional[float] = None
    pressure: Optional[int] = None      # 气压 (仅 T1，data[27]*10)

    raw: bytes = b""
    ts: float = 0.0

    # ---- 兼容旧字段名（界面/外部代码仍在用）
    @property
    def outlet(self) -> Optional[float]:
        return self.temp_out

    @property
    def inlet(self) -> Optional[float]:
        return self.temp_in

    @property
    def exchanger(self) -> Optional[float]:
        return self.chamber

    @property
    def power(self) -> int:
        return self.burner_power

    @property
    def burning(self) -> bool:
        return self.burner_power > 0

    @property
    def mode_text(self) -> str:
        return {0: "待机", 1: "燃烧", 2: "舒适"}.get(self.mode, "未知")

    @property
    def failure_text(self) -> str:
        return "无" if not self.failure else "%02X" % self.failure


# ---------------------------------------------------------------------- 协议
class BoschProtocol:
    """纯编解码，不做任何 IO。所有方法都是静态/类方法，方便单独测试。"""

    # ---- 加密
    @staticmethod
    def xor(data: bytes) -> bytes:
        return bytes(v ^ XOR_KEY[i % 9] for i, v in enumerate(data))

    @staticmethod
    def encode_chunks(plain: bytes, chunk: int = CHUNK) -> List[bytes]:
        """按包分片并逐片加密。

        注意：设备要求**每个 notify 分片独立从 KEY[0] 开始异或**，
        不是整帧连续，所以必须自己切片，不能整体 xor 后交给底层拆包。
        """
        out = []
        for i in range(0, len(plain), chunk):
            out.append(BoschProtocol.xor(plain[i:i + chunk]))
        return out

    # ---- 组帧
    @staticmethod
    def build(sender: int, receiver: int, mid: int, cmd: int,
              data: bytes = b"") -> bytes:
        """组一个**逻辑帧**（含校验，不含填充）。

        传输前还要过一遍 stuff() 插填充、再 encode_chunks() 加密。
        逻辑帧与"线上字节"分开，收发两端才能对称。
        """
        body = bytes([sender, receiver, mid & 0xFF, cmd]) + data
        return FRAME_START + body + bytes([sum(body) & 0xFF]) + FRAME_END

    @staticmethod
    def stuff(frame: bytes) -> bytes:
        """传输前插填充：数据区出现 7B/7D 时后面补 00（帧头帧尾不处理）"""
        if len(frame) <= 4:
            return frame
        head, body, tail = frame[:2], frame[2:-2], frame[-2:]
        out = bytearray(head)
        for b in body:
            out.append(b)
            if b in (0x7b, 0x7d):
                out.append(0x00)
        return bytes(out) + tail

    # ---- 解帧
    @staticmethod
    def decrypt_chunk(data: bytes) -> bytes:
        """解密一个 notify 分片。每片独立从 KEY[0] 开始。"""
        return BoschProtocol.xor(bytes(data))

    @staticmethod
    def unstuff(frame: bytes) -> bytes:
        """去掉数据区里 7B/7D 之后插入的 00 填充（帧头帧尾不参与）"""
        if len(frame) <= 2:
            return frame
        head, body, tail = frame[:2], frame[2:-2], frame[-2:]
        out = bytearray(head)
        skip = False
        for b in body:
            if skip and b == 0x00:
                skip = False
                continue
            out.append(b)
            skip = b in (0x7b, 0x7d)
        return bytes(out) + tail

    @staticmethod
    def decode_stream(plain: bytes):
        """从**已逐片解密**的明文流中切出完整帧。

        返回 (完整帧列表, 尚未收全的剩余字节)。
        调用方式必须是：先 decrypt_chunk 每片 → 拼接 → 再送这里。
        """
        frames, buf = [], plain
        while True:
            if not buf.startswith(FRAME_START):
                i = buf.find(FRAME_START)
                if i < 0:
                    return frames, b""
                buf = buf[i:]
            i = buf.find(FRAME_END, 2)
            if i < 0:
                return frames, buf
            frames.append(BoschProtocol.unstuff(buf[:i + 2]))
            buf = buf[i + 2:]

    # ---- 字段解析
    @staticmethod
    def parse_state(payload: bytes) -> DeviceState:
        """解析 0xA0 广播（字段偏移依据 2026-11-01 实机采样，Type0/Type1 分型）。"""
        n = len(payload)

        def b(i):
            return payload[i] if n > i else None

        def temp(i):
            if n > i + 1:
                return payload[i] + payload[i + 1] / 100.0
            return None

        ftype = b(0) or 0
        t1 = (ftype == 1)

        # 温度类：两型偏移一致（已实机验证）
        # 风机关 / 运行模式 / 出水 / 浴室控制器：两型偏移不同（实机对照）
        mode_i = 32 if t1 else 16
        bath_i = 33 if t1 else 17
        out_i = 28 if t1 else 21

        bath_raw = b(bath_i) or 0
        maxsp = (bath_raw & 0x7F) if n > bath_i else None
        ion_raw = b(10) or 0

        # 流量 / 功率 / 风机：实测与本机固件不符，以下按 2026-11-01
        # 大小水流对照实验（13:41 大水流 vs 13:42 关水）实测得出：
        #   [21] 75~111 → 关水 2~3      = 水流量 ×0.1 L/min
        #   [13] 36~47  → 关水 15~16    = 燃烧功率 %（关水后低温保温）
        #   [15][16] 15/11 → 7/5        = 风机转速（[15]*100+[16] rpm）
        # 注意：仅对 Type1 帧验证过，Type0 沿用另一套偏移。
        if t1:
            water_flow = (b(21) / 10.0) if n > 21 else None
            burner = b(13) or 0
            fan_speed = ((b(15) or 0) * 100 + (b(16) or 0)) if n > 16 else None
            fan_power = b(12)
        else:
            water_flow = (b(8) / 10.0) if n > 8 else None
            burner = b(9) or 0
            fan_speed = b(14)
            fan_power = b(13)

        return DeviceState(
            frame_type=ftype,
            temp_in=temp(1),
            chamber=temp(3),
            setpoint=b(5),
            temp_box=temp(6),
            water_flow=water_flow,
            burner_power=burner,
            ionized=bool(ion_raw & 0x10),
            failure=b(11) or 0,
            fan_power=fan_power,
            fan_speed=fan_speed,
            mode=b(mode_i),
            temp_out=temp(out_i),
            bathroom=bool(bath_raw & 0x80),
            max_setpoint=maxsp,
            air_flow=temp(16) if t1 else None,
            air_setpoint=temp(18) if t1 else None,
            gas_setpoint=temp(22) if t1 else None,
            pressure=(b(27) * 10) if (t1 and n > 27) else None,
            raw=bytes(payload),
            ts=time.time(),
        )

    # ---- 配对码编码（每位数字转 1 字节）
    @staticmethod
    def encode_pair_code(code: str) -> bytes:
        return bytes(int(c) for c in code if c.isdigit())

    # ---- 应答解析（历史 / 故障；格式依据 2026-11-01 实测）
    @staticmethod
    def parse_history(payload: bytes) -> dict:
        """B0 应答：[type][4字节大端 A][4字节大端 B]。

        实测 type=2 返回 02 | 00 00 12 67 | 00 00 05 b6 → 4711 / 1462。
        计数为大端，÷60 得小时：A 为运行分钟数，B 为点火次数。
        """
        if len(payload) < 9:
            return {"type": payload[0] if payload else None,
                    "a": None, "b": None, "raw": payload.hex(" ")}
        return {
            "type": payload[0],
            "a": int.from_bytes(payload[1:5], "big"),
            "b": int.from_bytes(payload[5:9], "big"),
            "raw": payload.hex(" "),
        }

    @staticmethod
    def parse_errors(payload: bytes) -> List[int]:
        """D7 应答：[状态字节][故障码...]。

        实测（2026-11-01）：请求 1/5/10 条，应答长度严格等于请求条数，
        首字节恒为 0xEA —— 它是状态/起始标识，**不是故障码**（App 显示无历史故障）。
        其后各字节为故障码，0x00 表示空槽。
        """
        return [b for b in payload[1:] if b]


# ------------------------------------------------------------------ 单次会话
class BoschDevice:
    """一次连接的完整生命周期。由 BoschService 驱动，也可单独使用。

    事件回调都在 asyncio 线程里执行，不要在里面做阻塞操作。
    """

    def __init__(self, cfg: Config,
                 on_state: Optional[Callable[[DeviceState], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_login: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_state = on_state
        self.on_log = on_log
        self.on_login = on_login

        self.client: Optional[BleakClient] = None
        self.login_state = "未登录"
        self.state: Optional[DeviceState] = None
        self.mid = 0
        self._buf = b""
        self._login_resp: Optional[bytes] = None
        self._login_ev = asyncio.Event()
        self.cooldown_sec: float = 0.0   # 设备要求的最短重连间隔（3C/5A 时设置）
        self._pair_fail_reason: Optional[str] = None
        self.pair_code_ts: Optional[float] = None   # 最近一次提交配对码的时刻
        # ---- 认证阶段的结构化结果（上层不要解析 login_state 文案）
        self.mac_registered = False        # 设备认得本机 MAC（A2/07 = 01/02）
        self.authorized = False            # 应用层授权通过（A2/08 = 01），收 A0 的唯一判据
        self.pair_kind = ""                # 'first_pair'(07=02) | 'rebind'(07=01) | ''
        self.pair_code_verified_at: Optional[float] = None
        self._last_rx = time.time()
        # 单飞应答路由（规格 §3.5）：mid -> (Event, payload)
        self._resp_ev: "dict[int, asyncio.Event]" = {}
        self._resp_map: "dict[int, Optional[bytes]]" = {}

    # ---- 日志
    def log(self, msg: str):
        LOG.info(msg)
        if self.on_log:
            self.on_log(msg)

    def tx(self, mid, cmd, data: bytes, tag: str = ""):
        LOG.debug("[TX] mid=%3d cmd=%02X len=%2d %s %s",
                  mid, cmd, len(data), data.hex(" "), tag)

    def rx(self, mid, cmd, payload: bytes):
        LOG.debug("[RX] mid=%3d cmd=%02X len=%2d %s",
                  mid, cmd, len(payload), payload.hex(" "))

    def _set_login(self, s: str):
        self.login_state = s
        if self.on_login:
            self.on_login(s)

    @property
    def pair_fail_reason(self) -> Optional[str]:
        """最近一次配对/登录失败的结构化原因；成功或未失败时为 None。

        取值见模块级 PAIR_FAIL_* 常量。界面、CLI 与诊断工具应优先用它，
        不要靠匹配 login_state 的中文文案——文案会调整，原因码是契约。
        """
        return self._pair_fail_reason

    def pair_fail_hint(self) -> str:
        """把失败原因翻译成给用户的一步动作；无失败返回空串。"""
        return PAIR_HINTS.get(self._pair_fail_reason or "", "")

    def pair_summary(self) -> dict:
        """认证阶段的结构化摘要，供界面 / MQTT 上报使用。故意不含码明文。"""
        return {
            "mac_registered": self.mac_registered,
            "authorized": self.authorized,
            "pair_kind": self.pair_kind,
            "fail_reason": self._pair_fail_reason,
            "fail_hint": self.pair_fail_hint(),
            "code_verified_at": self.pair_code_verified_at,
        }

    # ---- 连接
    async def connect(self):
        """对齐实机观测到的连接序列：
        无任何读操作、无 MTU 请求；订阅 FIFO CCCD → 200ms → 订阅 CREDITS CCCD。
        """
        self.client = BleakClient(self.cfg.device_address, timeout=25.0)
        await self.client.connect()
        LOG.info("[CONN] 链路建立 address=%s", self.cfg.device_address)
        await asyncio.sleep(0.6)
        # 不预检 services（Windows 服务发现未完成时会误判"GATT 未就绪"）；
        # 直接订阅，bleak 内部会等服务发现完成。失败重试 2 次。
        for attempt in range(3):
            try:
                await self.client.start_notify(CHAR_FIFO, self._on_notify,
                                               force_indicate=False)
                break
            except Exception as e:
                if attempt == 2:
                    LOG.error("[CONN] 订阅 FIFO 失败（服务枚举未就绪）: %s",
                              type(e).__name__)
                    raise RuntimeError("GATT not ready")
                LOG.info("[CONN] 服务未就绪，1s 后重试订阅（%d/3）", attempt + 1)
                await asyncio.sleep(1.0)
        await asyncio.sleep(0.2)          # App: postDelayed(200ms) 后订 CREDITS
        await self.client.start_notify(CHAR_CREDITS, self._on_notify,
                                       force_indicate=False)
        self._last_rx = time.time()

    async def close(self):
        if self.client is not None:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
                    LOG.info("[CONN] 已断开")
            except Exception:
                pass
            self.client = None

    # ---- 登录
    async def _send_login_wait(self, cmd_data: bytes, tag: str) -> Optional[bytes]:
        """登录帧发送：6s 超时 ×5 次重发，重发换 msgId（规格 §3.5/§6.6）。

        send() 每次调用自增 self.mid，天然满足"重发换 msgId"。
        """
        for attempt in range(5):
            self._login_resp = None
            self._login_ev.clear()
            await self.send(CMD_LOGIN, cmd_data, RECV_LOGIN, tag)
            if await self._wait_login(6):
                LOG.info("[AUTH] %s 应答: %s", tag,
                         (self._login_resp or b"").hex(" "))
                return self._login_resp
            LOG.warning("[AUTH] %s 无应答，重发 %d/5（新 msgId）",
                        tag, attempt + 1)
        return None

    async def login(self) -> bool:
        """登录 → 提交配对码。两步都必须做，缺第二步设备不推数据。

        时序依据实机观测：提交 MAC 前 sleep(1000)；
        A2/07 应答后走 sendLoginPass（开头 sleep(1000)）。00 拒绝与 02 同样
        进入密码流程（两种情况设备侧处理一致）。
        """
        try:
            mac = self.cfg.login_mac_bytes
        except ValueError:
            self._set_login("本机蓝牙地址无效")
            return False
        code = (self.cfg.pair_code or "").strip()

        self._set_login("认证中…")
        await asyncio.sleep(1.0)          # App: 提交 MAC 前 sleep(1000)
        self._pair_fail_reason = None      # 每次登录先清空上一次的失败原因
        self.mac_registered = False
        self.authorized = False
        self.pair_kind = ""
        r = await self._send_login_wait(bytes([0x07]) + mac, "登录")
        if r is None:
            self._pair_fail_reason = PAIR_FAIL_NO_RESPONSE
            self._set_login("无应答")
            return False

        kind = r[1] if len(r) > 1 else -1
        # 冷却/等待（这两种应答设备会直接断线——我们也断开并拉长重连间隔）
        if kind == 0x3C:
            self._pair_fail_reason = PAIR_FAIL_COOLDOWN
            self._set_login("设备冷却中")
            self.cooldown_sec = 30.0
            return False
        if kind == 0x5A:
            self._pair_fail_reason = PAIR_FAIL_COOLDOWN
            self._set_login("设备要求等待 90 秒")
            self.cooldown_sec = 90.0
            return False
        if kind == 0x01:
            # 实测 2026-11-07：01 表示设备端**已登记本机 MAC**，但**仍需 A2/08 校验**。
            # 出现过 01 → A2/08=00 失败的组合，所以 01 不是「免密通过」。
            self.mac_registered = True
            self.pair_kind = "rebind"
            self._set_login("MAC 已登记，仍需配对码验证")
        elif kind == 0x02:
            self.mac_registered = True
            self.pair_kind = "first_pair"
            self._set_login("设备要求配对码（窗口已开）")
        elif kind == 0x00:
            # 2026-11-07 ESP32-S3 真机实测：00 表示设备端不在配对窗口
            # （从未「清除配对」，或窗口已超时关闭），**不是**「MAC 未授权」。
            # 仍补发 A2/08（设备侧同样处理），但原因码如实记 window_closed。
            self._pair_fail_reason = PAIR_FAIL_WINDOW_CLOSED
            self._set_login("配对窗口未打开或已超时（仍尝试提交码）")
        else:
            self._set_login("未知应答 %s" % r.hex(" "))
            return False

        if not code:
            self._set_login("需要配对码（未填写）")
            self.log("请在配置里填写配对码，否则设备不会推送数据")
            return False

        # 对齐 App 时序：提交配对码前固定 sleep(1000ms)
        await asyncio.sleep(1.0)
        self.pair_code_ts = time.time()
        r2 = await self._send_login_wait(
            bytes([0x08]) + BoschProtocol.encode_pair_code(code), "配对码")
        if r2 is None:
            if self._pair_fail_reason is None:
                self._pair_fail_reason = PAIR_FAIL_NO_RESPONSE
            self._set_login("配对码无应答")
            return False

        k2 = r2[1] if len(r2) > 1 else -1
        if k2 == 0x01:
            self._pair_fail_reason = None
            self.authorized = True
            self.pair_code_verified_at = time.time()
            self._set_login("已授权（配对码）")
            return True
        if k2 == 0x02:
            self._pair_fail_reason = PAIR_FAIL_CODE_MISMATCH
            self._set_login("配对码错误（与当前面板码不一致）")
        elif k2 == 0x00:
            # 实测：窗口已关或码已过期都会回 00。
            # 若 A2/07 已经是 00，说明窗口从头就没开，此处保持 window_closed——
            # 否则会把「没开窗口」误报成「码错」，用户去核对码是白折腾。
            if self._pair_fail_reason != PAIR_FAIL_WINDOW_CLOSED:
                self._pair_fail_reason = PAIR_FAIL_CODE_REJECTED
            self._set_login("配对码被拒绝（窗口已关或码已过期）")
        else:
            self._set_login("配对码应答异常 %s" % r2.hex(" "))
        return False

    async def _wait_login(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._login_ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- 收发
    async def send(self, cmd: int, data: bytes = b"",
                   receiver: int = RECV_BIZ, tag: str = ""):
        if self.client is None or not self.client.is_connected:
            raise RuntimeError("not connected")
        frame = BoschProtocol.build(SENDER, receiver, self.mid, cmd, data)
        self.mid = (self.mid + 1) & 0xFF
        wire = BoschProtocol.stuff(frame)
        self.tx(self.mid - 1, cmd, data, tag)
        for chunk in BoschProtocol.encode_chunks(wire):
            await self.client.write_gatt_char(CHAR_FIFO, chunk, response=True)
            await asyncio.sleep(0.02)
        if tag:
            self.log("%s → %s" % (tag, frame.hex(" ")))
        await asyncio.sleep(0.1)          # App 无此延迟；留小间隔防淹没

    def _on_notify(self, _sender, data: bytearray):
        self._last_rx = time.time()
        self._buf += BoschProtocol.decrypt_chunk(bytes(data))
        frames, self._buf = BoschProtocol.decode_stream(self._buf)
        for f in frames:
            if len(f) < 9:
                continue
            if sum(f[2:-3]) & 0xFF != f[-3]:      # CheckSum.check 等价
                LOG.warning("[RX] 丢弃坏帧 chk: %s", f.hex(" "))
                continue
            mid, cmd, payload = f[4], f[5], f[6:-3]
            self.rx(mid, cmd, payload)
            if cmd == CMD_STATE:
                st = BoschProtocol.parse_state(payload)
                self.state = st
                LOG.debug("[STATE] T%d 出%.1f 进%.1f 炉腔%.1f 机身%.1f 设定%s "
                          "功率%d%% 风机%s/%srpm 水流%.1f 模式%s 故障%s%s",
                          st.frame_type,
                          st.temp_out or 0, st.temp_in or 0, st.chamber or 0,
                          st.temp_box or 0, st.setpoint,
                          st.burner_power, st.fan_power, st.fan_speed,
                          st.water_flow or 0, st.mode_text, st.failure_text,
                          " 电离" if st.ionized else "")
                LOG.debug("[STATE] raw(%d)=%s", len(payload), payload.hex(" "))
                if self.on_state:
                    self.on_state(st)
            elif cmd == CMD_LOGIN:
                self._login_resp = payload
                self._login_ev.set()
                ev = self._resp_ev.get(mid)
                if ev is not None:
                    self._resp_map[mid] = payload
                    ev.set()
            else:
                # 应答路由：有等待者则投递（单飞机制，规格 §3.5）
                ev = self._resp_ev.get(mid)
                if ev is not None:
                    self._resp_map[mid] = payload
                    ev.set()
                else:
                    # D2/D8 等业务应答统一记下来，方便探测未知命令
                    self.log("RX cmd=%02X mid=%d data=%s"
                             % (cmd, mid, payload.hex(" ")))

    # ---- 高层操作
    async def send_and_wait(self, cmd: int, data: bytes = b"",
                            receiver: int = RECV_BIZ, tag: str = "",
                            timeout: float = 6.0, retries: int = 5) -> bytes:
        """单飞发送：发出后等应答；6s 超时 ×5 重发，重发换 msgId（规格 §3.5）。

        应答按 msgId 路由；若设备不回显 mid，则宽松匹配"唯一等待者"。
        重试耗尽抛 TimeoutError。
        """
        for attempt in range(retries):
            expect = self.mid                       # send() 将使用的 msgId
            ev = asyncio.Event()
            self._resp_ev[expect] = ev
            self._resp_map[expect] = None
            try:
                await self.send(cmd, data, receiver, tag)
                try:
                    await asyncio.wait_for(ev.wait(), timeout)
                    resp = self._resp_map.pop(expect, None)
                    if resp is not None:
                        return resp
                except asyncio.TimeoutError:
                    pass
            finally:
                self._resp_ev.pop(expect, None)
                self._resp_map.pop(expect, None)
            self.log("%s 无应答，重发 %d/%d（新 msgId）" % (tag, attempt + 1, retries))
        raise TimeoutError("%s 重试耗尽（%d 次 × %ds）" % (tag, retries, timeout))

    async def set_temperature(self, value: int):
        v = max(TEMP_MIN, min(TEMP_MAX, int(value)))
        try:
            resp = await self.send_and_wait(
                CMD_WRITE_PARAM,
                bytes([PARAM_TYPE_SETPOINT, PARAM_ID_SETPOINT, 0x00, v]),
                RECV_BIZ, "设置 %d℃" % v)
            LOG.info("[CMD] 设置 %d℃ 应答: %s", v, resp.hex(" "))
        except TimeoutError as e:
            LOG.warning("[CMD] 设置 %d℃ 未获确认: %s", v, e)
            raise

    async def idle_seconds(self) -> float:
        return time.time() - self._last_rx

    # ---- 只读查询（历史 / 故障）
    async def read_history(self, kind: int = 2) -> bytes:
        """读历史数据（B0）。kind: 2=运行统计(次数/小时) 3=洗浴记录(水量/时长)。"""
        return await self.send_and_wait(
            CMD_HISTORY, bytes([kind]), RECV_BIZ, "历史数据 type=%d" % kind)

    async def read_errors(self, count: int = 10) -> bytes:
        """读故障列表（D7）。应答为故障码序列。"""
        return await self.send_and_wait(
            CMD_READ_ERROR, bytes([count]), RECV_BIZ, "故障列表")


# ------------------------------------------------------------------ 常驻服务
class BoschService:
    """常驻服务：后台线程跑 asyncio，自动重连，对外提供同步接口。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_flag = False
        self._reload = False
        self._cmd_q: "queue.Queue[Optional[int]]" = queue.Queue()
        self._req_q: "queue.Queue[tuple]" = queue.Queue()   # 只读查询（历史/故障）
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._state_lock = threading.Lock()

        self.state: Optional[DeviceState] = None
        self.status = "未启动"
        self.login_state = "未登录"
        self.pair_fail_reason: Optional[str] = None
        self.connected = False
        self._rx_frames = 0            # 本次会话收到的 A0 帧数（判定会话有效性）
        # 查询结果：{"history": bytes|None, "history_ts":..., "errors": bytes|None,...}
        self.results: dict = {}

        # 可选的对外回调（在 worker 线程执行）
        self.on_state: Optional[Callable[[DeviceState], None]] = None

    # ---- 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="bosch-service")
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        """优雅停止：先让会话断开蓝牙，再退出线程。"""
        self._stop_flag = True
        self._cmd_q.put(None)
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 对外命令
    def set_temperature(self, value: int):
        self._cmd_q.put(value)

    def query_history(self, kind: int = 2):
        """请求历史数据，结果写入 self.results['history']。"""
        self._req_q.put(("history", kind))

    def query_errors(self, count: int = 10):
        """请求故障列表，结果写入 self.results['errors']。"""
        self._req_q.put(("errors", count))

    def take_logs(self) -> List[str]:
        out = []
        while True:
            try:
                out.append(self._log_q.get_nowait())
            except queue.Empty:
                return out

    def pair_fail_hint(self) -> str:
        """最近一次配对失败的动作建议；无失败或原因未知时返回空串。"""
        return PAIR_HINTS.get(self.pair_fail_reason or "", "")

    def update_config(self, cfg: Config):
        """换配置并立即重连。"""
        self.cfg = cfg
        self._reload = True

    def set_pair_code_and_reconnect(self, code: str, save: bool = True):
        """用面板新生成的动态配对码立即重连。

        配对码是热水器进入配对菜单时才生成的短时效码，所以这条路径
        追求快：更新配置 → 跳过退避等待 → 立即直连。
        """
        if not isinstance(code, str):
            raise ValueError("配对码应为 6 位数字")
        code = code.strip()
        if not code.isdigit() or len(code) != 6:
            raise ValueError("配对码应为 6 位数字")
        self.cfg.pair_code = code
        if save:
            self.cfg.save()
        self._log("收到新配对码，立即重连")
        self._reload = True

    # ---- 内部
    def _log(self, msg: str):
        LOG.info("[SVC] %s" % msg)
        self._log_q.put("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            self._log("服务退出: %s" % e)

    def _set_state(self, st: DeviceState):
        self.state = st
        self._rx_frames += 1
        if self.on_state:
            try:
                self.on_state(st)
            except Exception:
                pass

    async def _main(self):
        fails = 0
        gatt_fails = 0                     # 连续 GATT 未就绪次数（配对自愈触发器）
        reported = set()
        while not self._stop_flag:
            errs = self.cfg.validate()
            if errs:
                self.status = "配置不完整"
                for e in errs:
                    if e not in reported:      # 同一条只提示一次，避免刷屏
                        reported.add(e)
                        self._log("配置错误：" + e)
                end = time.time() + 2
                while time.time() < end and not self._stop_flag:
                    await asyncio.sleep(0.2)
                continue
            reported.clear()

            # 保持系统级配对（解绑后明文链路会被设备静默丢弃，见文件头注释）
            if not await ensure_paired(self.cfg.device_address, log=self._log):
                self.status = "配对失败：请到面板清除配对"
                end = time.time() + 20
                while time.time() < end and not self._stop_flag:
                    await asyncio.sleep(0.5)
                continue

            dev = BoschDevice(self.cfg, on_state=self._set_state,
                              on_log=self._log,
                              on_login=lambda s: setattr(self, "login_state", s))
            ok = False
            t_sess = time.time()
            self._rx_frames = 0
            self.status = "连接中…"
            try:
                await dev.connect()
                self.connected = True
                self.status = "已连接"
                exit_reason = "ok"
                if await dev.login():
                    self.pair_fail_reason = None
                    self._log("登录成功，开始接收数据")
                    # 丢弃断线期间积压的旧温度命令，防止在新会话里意外生效
                    while not self._cmd_q.empty():
                        try:
                            self._cmd_q.get_nowait()
                        except queue.Empty:
                            break
                    while dev.client and dev.client.is_connected:
                        if self._stop_flag:
                            exit_reason = "stop"
                            break
                        if await dev.idle_seconds() > IDLE_TIMEOUT:
                            self._log("超过 %d 秒无数据，链路已失效，重连"
                                      % int(IDLE_TIMEOUT))
                            exit_reason = "idle"
                            break
                        if self._reload:
                            self._reload = False
                            self._log("配置已更新，重新连接")
                            exit_reason = "reload"
                            break
                        try:
                            v = self._cmd_q.get_nowait()
                        except queue.Empty:
                            v = None
                        if v is not None:
                            try:
                                await dev.set_temperature(v)
                            except TimeoutError:
                                # 命令失败已记日志；不断会话（设备下次广播
                                # data[5] 会带回真实设定值，UI 自然纠正）
                                pass
                        # 只读查询（历史 / 故障）
                        try:
                            what, arg = self._req_q.get_nowait()
                        except queue.Empty:
                            what, arg = None, None
                        if what == "history":
                            try:
                                r = await dev.read_history(arg)
                                self.results["history"] = r
                                self.results["history_ts"] = time.time()
                                self._log("历史数据 type=%d: %s" % (arg, r.hex(" ")))
                            except TimeoutError:
                                self.results["history"] = None
                                self._log("历史数据读取超时")
                        elif what == "errors":
                            try:
                                r = await dev.read_errors(arg)
                                self.results["errors"] = r
                                self.results["errors_ts"] = time.time()
                                self._log("故障列表: %s" % r.hex(" "))
                            except TimeoutError:
                                self.results["errors"] = None
                                self._log("故障列表读取超时")
                        await asyncio.sleep(0.5)
                    ok = True
                else:
                    self.pair_fail_reason = dev.pair_fail_reason
                    self._log("登录未通过：%s" % dev.login_state)
                    if dev.pair_fail_hint():
                        self._log("下一步：" + dev.pair_fail_hint())
            except Exception as e:
                msg = str(e)
                if "GATT" in msg:
                    self.status = "设备忙，重试中"
                    gatt_fails += 1
                    if gatt_fails >= 3:
                        # 配对自愈：设备侧密钥已作废而 Windows 侧不知情，
                        # 带旧密钥的链路会被 GATT 层直接拒绝。删掉本机配对，
                        # 下一轮 ensure_paired 会重新绑定。
                        self._log("连续 %d 次 GATT 失败，删除旧配对并重新绑定"
                                  % gatt_fails)
                        await unpair_device(self.cfg.device_address)
                        gatt_fails = 0
                else:
                    gatt_fails = 0
                self._log("异常: %s: %s" % (type(e).__name__, msg))
            finally:
                self.connected = False
                await dev.close()

            if self._stop_flag:
                break
            # 关键：只有"会话真正稳定"才重置退避。
            # 之前只要 connect+login 成功就 fails=0，而设备常在登录后
            # 立刻踢人（限流），结果变成 3 秒一次的密集重连，越连越被拒。
            dur = time.time() - t_sess
            stable = (ok and dur >= SESSION_STABLE_SEC
                      and self._rx_frames >= SESSION_MIN_FRAMES)
            # 设备明确要求冷却（3C=30s / 5A=90s）时，覆盖退避间隔
            cd = getattr(dev, "cooldown_sec", 0.0) or 0.0
            dev.cooldown_sec = 0.0
            if stable:
                fails, wait = 0, 3
                # 会话曾稳定却被异常中断（踢线/断链）时，缓 30 秒再连，
                # 避免反复撞设备限流（规格 §7.10 精神）
                if exit_reason not in ("stop", "reload"):
                    wait = 30
                    self.status = "会话中断，30s 后重连"
                else:
                    self.status = "重连中…"
            else:
                fails += 1
                wait = min(RECONNECT_BASE * (2 ** (fails - 1)), RECONNECT_MAX)
                if cd > wait:
                    wait = cd
                if ok:
                    self.status = "会话不稳定，等待 %ds" % int(wait)
                elif fails >= 3:
                    self.status = "连不上，%ds 后重试" % int(wait)
                else:
                    self.status = "等待重连 %ds" % int(wait)
                if fails >= 3 and fails % 3 == 0:
                    self._log("连续 %d 次未能建立稳定会话，设备可能在限流，"
                              "建议断电重启热水器" % fails)
            end = time.time() + wait
            while (time.time() < end and not self._stop_flag
                   and not self._reload):
                await asyncio.sleep(0.2)

        self.status = "已停止"
        self.connected = False


def local_bt_mac() -> str:
    """读取本机蓝牙地址。失败返回空串。

    Windows 优先用 WinRT（最可靠），退回 PowerShell；Linux 读 sysfs。
    """
    # 1) Windows WinRT
    if sys.platform == "win32":
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter

            async def _get():
                a = await BluetoothAdapter.get_default_async()
                return a.bluetooth_address if a else 0

            addr = asyncio.run(_get())
            if addr:
                return "%012X" % addr
        except Exception:
            pass
        # 2) 退回 PowerShell
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetAdapter -Physical | Where-Object "
                 "{$_.InterfaceDescription -like '*Bluetooth*'}).MacAddress"],
                capture_output=True, text=True, timeout=12,
                creationflags=0x08000000)
            for line in r.stdout.splitlines():
                s = line.strip().replace("-", "").replace(":", "")
                if len(s) == 12 and all(c in "0123456789abcdefABCDEF" for c in s):
                    return s.upper()
        except Exception:
            pass

    # 3) Linux
    try:
        p = pathlib.Path("/sys/class/bluetooth/hci0/address")
        if p.exists():
            s = p.read_text().strip().replace(":", "").upper()
            if len(s) == 12:
                return s
    except Exception:
        pass
    return ""


__all__ = ["Config", "DeviceState", "BoschProtocol", "BoschDevice",
           "BoschService", "TEMP_MIN", "TEMP_MAX", "local_bt_mac",
           "ensure_paired", "paired_state", "parse_mac", "unpair_device"]
