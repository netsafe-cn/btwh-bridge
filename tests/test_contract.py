# -*- coding: utf-8 -*-
"""test_contract.py —— 接口契约测试

把 API.md 里"只增不改"的口头承诺，变成 CI 上会变红的断言。

覆盖：
  1. 签名冻结：对比 api_contract.json 快照
     - 删除符号 / 改参数名 / 删参数 / 改默认值 / 把可选改成必填 => 失败
     - 末尾新增【带默认值】的可选参数 => 允许（契约规定的扩展方式）
  2. 常量冻结：UUID、XOR_KEY、命令字、分片大小，改了必须显式升主版本号
  3. 跨端黄金向量：protocol_vectors.json
     Python 参考实现与 ESP32 C++ 固件逐字节对照，防止两端协议漂移

更新快照（仅在有意做兼容扩展、且已审过 API.md 变更记录时）：
    python tests/test_contract.py --update
"""
from __future__ import annotations

# Allow `python tests/test_contract.py --update` from the repository root.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import inspect
import json
import pathlib
import sys
import unittest
from dataclasses import asdict

import bosch_api as api
from bosch_api import BoschProtocol

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                      # tests/ lives one level below the repo root
CONTRACT = ROOT / "api_contract.json"
VECTORS = ROOT / "protocol_vectors.json"

# API.md 声明的全部公开符号。新增公开 API 时，除了写文档也要加进这里。
API_SYMBOLS = [
    "parse_mac", "paired_state", "ensure_paired", "unpair_device", "local_bt_mac",
    "Config.load", "Config.save", "Config.validate",
    "BoschProtocol.xor", "BoschProtocol.encode_chunks", "BoschProtocol.build",
    "BoschProtocol.stuff", "BoschProtocol.unstuff", "BoschProtocol.decrypt_chunk",
    "BoschProtocol.decode_stream", "BoschProtocol.parse_state",
    "BoschProtocol.parse_history", "BoschProtocol.parse_errors",
    "BoschProtocol.encode_pair_code",
    "BoschDevice.connect", "BoschDevice.close", "BoschDevice.login",
    "BoschDevice.send", "BoschDevice.send_and_wait",
    "BoschDevice.set_temperature", "BoschDevice.idle_seconds",
    "BoschDevice.read_history", "BoschDevice.read_errors",
    "BoschDevice.pair_fail_hint", "BoschDevice.pair_summary",
    "BoschService.pair_fail_hint",
    "BoschService.start", "BoschService.stop",
    "BoschService.set_temperature", "BoschService.query_history",
    "BoschService.query_errors", "BoschService.take_logs",
    "BoschService.update_config", "BoschService.set_pair_code_and_reconnect",
]

API_PROPERTIES = [
    "Config.login_mac_bytes",
    "BoschDevice.pair_fail_reason",
    "DeviceState.outlet", "DeviceState.inlet", "DeviceState.exchanger",
    "DeviceState.power", "DeviceState.burning", "DeviceState.mode_text",
    "DeviceState.failure_text", "BoschService.running",
]

CONSTANT_NAMES = [
    "SERVICE_BASE", "CHAR_MODE", "CHAR_FIFO", "CHAR_CREDITS", "CHAR_COUNTER",
    "XOR_KEY", "FRAME_START", "FRAME_END", "SENDER", "RECV_BIZ", "RECV_LOGIN",
    "CMD_STATE", "CMD_BCAST_CFG", "CMD_LOGIN", "CMD_READ_PARAM",
    "CMD_WRITE_PARAM", "CMD_HISTORY", "CMD_READ_ERROR", "CHUNK",
    "TEMP_MIN", "TEMP_MAX",
]


def resolve(dotted):
    """'BoschDevice.send_and_wait' -> 可调用对象；符号不存在直接抛 AttributeError。"""
    obj = api
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def shape(fn) -> list:
    """签名 -> 可比对结构。忽略类型注解（未来注解调整不该算 breaking）与 self/cls。"""
    out = []
    for p in inspect.signature(fn).parameters.values():
        if p.name in ("self", "cls"):
            continue
        has_default = p.default is not inspect.Parameter.empty
        out.append({
            "name": p.name,
            "kind": p.kind.name,
            "has_default": has_default,
            "default": repr(p.default) if has_default else None,
        })
    return out


def freeze(v):
    """递归把 bytes 转成 hex 标记，避免 json.dumps 报错（DeviceState 含 raw 字节）。"""
    if isinstance(v, bytes):
        return {"__bytes_hex__": v.hex()}
    if isinstance(v, dict):
        return {k: freeze(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [freeze(x) for x in v]
    return v


def build_vectors() -> dict:
    """生成 ESP32 C++ 固件单测可直接引用的黄金向量。"""
    mac = bytes.fromhex("C0335E0FF68A")
    code = "311522"

    def one(name, frame, note):
        stuffed = BoschProtocol.stuff(frame)
        return {
            "name": name,
            "note": note,
            "frame_hex": frame.hex(),
            "stuffed_hex": stuffed.hex(),
            "chunks_hex": [c.hex() for c in BoschProtocol.encode_chunks(stuffed)],
        }

    cases = [
        one("a2_07_submit_mac",
            BoschProtocol.build(0x21, 0xAB, 0x05, 0xA2, b"\x07" + mac),
            "登录第 1 步：提交已授权设备 MAC（receiver=0xAB）"),
        one("a2_08_submit_code",
            BoschProtocol.build(0x21, 0xAB, 0x06, 0xA2,
                                b"\x08" + BoschProtocol.encode_pair_code(code)),
            "登录第 2 步：提交面板 6 位码，每位 1 字节"),
        one("d3_set_temperature_42",
            BoschProtocol.build(0x21, 0x00, 0x10, 0xD3, bytes([0x02, 0x55, 42])),
            "写设定温度 42 度"),
        one("b0_read_history",
            BoschProtocol.build(0x21, 0x00, 0x11, 0xB0, bytes([2])),
            "读运行统计"),
        one("stuff_7b_7d",
            BoschProtocol.build(0x21, 0x00, 0x12, 0xA0,
                                bytes([0x7B, 0x7D, 0x00, 0x01])),
            "填充边界：数据区出现 7B/7D 时后插 00"),
    ]

    # A0 34 字节 Type1 状态帧，钉死 11-01 实测得到的字段偏移。
    # 温度编码是 [整数][百分位]，即 payload[i] + payload[i+1]/100。
    payload = bytearray(34)
    payload[0] = 1                              # 分型：1 = Type1
    payload[1], payload[2] = 20, 0             # 进水 20.00
    payload[3], payload[4] = 30, 0             # 炉腔 30.00
    payload[5] = 42                            # 设定温度 42
    payload[6], payload[7] = 26, 0             # 机身 26.00
    payload[10] = 0x10                         # 电离火焰
    payload[11] = 0                            # 无故障
    payload[12] = 35                           # 风机功率
    payload[13] = 35                           # 燃烧功率 35%
    payload[15], payload[16] = 8, 0            # 风机 800 rpm
    payload[21] = 80                           # 水流量 8.0 L/min
    payload[27] = 0                            # 压力
    payload[28], payload[29] = 45, 0           # 出水 45.00
    payload[32] = 1                            # 运行模式：燃烧
    payload[33] = 60                           # 浴室控制器上限 60
    state = freeze(asdict(BoschProtocol.parse_state(bytes(payload))))
    state.pop("ts", None)                      # 解析时间戳非确定性，不能进黄金向量
    cases.append({
        "name": "a0_state_type1",
        "note": "A0 34 字节 Type1：字段偏移依据 2026-11-01 实测采集；ts 已排除",
        "payload_hex": bytes(payload).hex(),
        "state": state,
    })

    return {
        "contract_version": "v1.0",
        "generated_by": "python test_contract.py --update",
        "usage": "ESP32 C++ 固件单测直接读这些 hex，逐字节对照自己的组帧/填充/加密实现",
        "cases": cases,
    }


class TestSignatureContract(unittest.TestCase):
    """签名冻结：任何 breaking change 必须在这里变红。"""

    @classmethod
    def setUpClass(cls):
        if not CONTRACT.exists():
            raise unittest.SkipTest("快照缺失，先跑 python test_contract.py --update")
        cls.snap = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_snapshot_covers_all_declared_symbols(self):
        missing = set(API_SYMBOLS) - set(self.snap["signatures"])
        self.assertFalse(missing, "契约快照漏登记符号：%s" % sorted(missing))

    def test_no_symbol_removed(self):
        for name in self.snap["signatures"]:
            with self.subTest(symbol=name):
                try:
                    resolve(name)
                except AttributeError:
                    self.fail("公开符号被删除：%s" % name)

    def test_signatures_backward_compatible(self):
        for name, old in self.snap["signatures"].items():
            with self.subTest(symbol=name):
                new = shape(resolve(name))
                self.assertGreaterEqual(len(new), len(old),
                                        "%s 参数变少（breaking）" % name)
                self.assertEqual(new[:len(old)], old,
                                 "%s 已有参数被改名/改默认值/改成必填（breaking）" % name)
                for extra in new[len(old):]:
                    self.assertTrue(extra["has_default"],
                                    "%s 新增必填参数 %s（breaking）" %
                                    (name, extra["name"]))

    def test_properties_exist(self):
        for name in API_PROPERTIES:
            with self.subTest(prop=name):
                try:
                    resolve(name)
                except AttributeError:
                    self.fail("兼容属性被删除：%s" % name)

    def test_constants_frozen(self):
        for name, old in self.snap["constants"].items():
            with self.subTest(const=name):
                self.assertEqual(freeze(getattr(api, name)), old,
                                 "协议常量 %s 被改动，必须升主版本号" % name)


class TestGoldenVectors(unittest.TestCase):
    """跨端一致性：这些 hex 同时是 ESP32 固件的单测输入。"""

    @classmethod
    def setUpClass(cls):
        if not VECTORS.exists():
            raise unittest.SkipTest("向量缺失，先跑 python test_contract.py --update")
        cls.v = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_frames_roundtrip(self):
        for case in self.v["cases"]:
            if "frame_hex" not in case:
                continue
            with self.subTest(case=case["name"]):
                frame = bytes.fromhex(case["frame_hex"])
                stuffed = BoschProtocol.stuff(frame)
                self.assertEqual(stuffed.hex(), case["stuffed_hex"],
                                 "填充结果与黄金向量不一致")
                self.assertEqual(
                    [c.hex() for c in BoschProtocol.encode_chunks(stuffed)],
                    case["chunks_hex"], "分片/逐片异或结果与黄金向量不一致")
                # 模拟设备侧收包：逐片解密 -> 拼接 -> 解帧 -> 还原逻辑帧
                plain = b"".join(BoschProtocol.decrypt_chunk(bytes.fromhex(c))
                                 for c in case["chunks_hex"])
                frames, rest = BoschProtocol.decode_stream(plain)
                self.assertEqual(rest, b"", "有未收全的残余字节")
                self.assertEqual([f.hex() for f in frames], [frame.hex()],
                                 "解帧未能还原原始逻辑帧")

    def test_a0_state_stable(self):
        case = next(c for c in self.v["cases"] if c["name"] == "a0_state_type1")
        st = freeze(asdict(
            BoschProtocol.parse_state(bytes.fromhex(case["payload_hex"]))))
        st.pop("ts", None)                     # 解析时间戳每次不同，不参与比对
        self.assertEqual(st, case["state"], "A0 字段解析发生漂移")
        # 向量本身必须是有意义的 Type1 样本，防止退化成全 0
        self.assertEqual(st["frame_type"], 1)
        self.assertEqual(st["temp_out"], 45.0)
        self.assertEqual(st["water_flow"], 8.0)
        self.assertEqual(st["fan_speed"], 800)


def main():
    if "--update" in sys.argv:
        snap = {
            "contract_version": "v1.0",
            "note": "API.md v1.0 签名快照；只允许末尾追加带默认值的可选参数",
            "signatures": {n: shape(resolve(n)) for n in API_SYMBOLS},
            "properties": API_PROPERTIES,
            "constants": {n: freeze(getattr(api, n)) for n in CONSTANT_NAMES},
        }
        CONTRACT.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        VECTORS.write_text(json.dumps(build_vectors(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print("已生成 api_contract.json / protocol_vectors.json")
        return 0
    unittest.main(verbosity=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
