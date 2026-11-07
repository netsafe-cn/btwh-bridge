# -*- coding: utf-8 -*-
"""协议层单元测试

运行：  python -m unittest test_protocol -v
全部用例都用**实机抓到的真实帧**做基准，任何改动导致不通过都说明协议实现被改坏了。
"""
import unittest

from bosch_api import BoschProtocol, DeviceState

# 真实 A0 状态帧（34 字节 data，Type1，2026-11-01 13:28 实机抓取，设定 42℃）
# 帧内容仅是设备读数，不含任何与个人相关的信息
A0_DATA = bytes.fromhex(
    "01 19 50 1a 5a 2a 1a 46 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 "
    "00 00 00 00 00 00 19 50 00 00 00 00")

# 下面两个是"结构基准帧"，MAC 与配对码都已替换成示例值
DEMO_MAC = bytes.fromhex("AABBCCDDEEFF")
DEMO_CODE = "123456"

REAL_LOGIN = "7b 7b 21 ab 00 a2 07 aa bb cc dd ee ff 70 7d 7d"
REAL_SET = "7b 7b 21 00 02 d3 02 55 00 26 73 7d 7d"


class TestProtocol(unittest.TestCase):

    def test_pair_code(self):
        """配对码每位数字转 1 字节"""
        self.assertEqual(BoschProtocol.encode_pair_code(DEMO_CODE),
                         bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06]))

    def test_build_login_frame(self):
        """登录帧结构正确（基准帧的 MAC 段已匿名化）"""
        f = BoschProtocol.build(0x21, 0xAB, 0, 0xA2, bytes([0x07]) + DEMO_MAC)
        self.assertEqual(f.hex(" "), REAL_LOGIN)

    def test_build_set_temperature_frame(self):
        """写温度帧与实机抓包逐字节一致"""
        f = BoschProtocol.build(0x21, 0x00, 2, 0xD3, bytes([0x02, 0x55, 0x00, 0x26]))
        self.assertEqual(f.hex(" "), REAL_SET)

    def test_checksum(self):
        """校验和 = sender..data 求和取低 8 位"""
        f = BoschProtocol.build(0x21, 0x00, 2, 0xD3, bytes([0x02, 0x55, 0x00, 0x26]))
        body = bytes.fromhex("210002d302550026")
        self.assertEqual(f[-3], sum(body) & 0xFF)

    def test_stuffing_roundtrip(self):
        """数据区含 7B/7D 时：插填充 → 加密 → 解密 → 去填充，要还原成逻辑帧"""
        data = bytes([0x7B, 0x00, 0x7D, 0x7B, 0x11])
        frame = BoschProtocol.build(0x21, 0x00, 5, 0xD2, data)
        wire = BoschProtocol.stuff(frame)
        self.assertGreater(len(wire), len(frame), "应该插入了填充字节")

        enc = b"".join(BoschProtocol.encode_chunks(wire))
        frames, rest = BoschProtocol.decode_stream(
            BoschProtocol.decrypt_chunk(enc))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)
        self.assertEqual(rest, b"")

    def test_a0_chunked_roundtrip(self):
        """43 字节状态帧在 MTU=23 下被拆成 20+20+3，要能正确还原

        这是最容易错的地方：设备对每个分片独立从 KEY[0] 开始异或。
        """
        frame = BoschProtocol.build(0x00, 0xFF, 0x6D, 0xA0, A0_DATA)
        self.assertEqual(len(frame), 43)
        wire = BoschProtocol.stuff(frame)
        self.assertEqual(len(wire), 43, "该帧不含定界符，不应插入填充")

        enc = b"".join(BoschProtocol.encode_chunks(wire))
        self.assertEqual(len(enc), 43)

        buf, got = b"", []
        for i in range(0, len(enc), 20):          # 模拟底层自动分片
            buf += BoschProtocol.decrypt_chunk(enc[i:i + 20])
            frames, buf = BoschProtocol.decode_stream(buf)
            got.extend(frames)

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], frame)

    def test_parse_state(self):
        """字段解析：Type1 真实帧（2026-11-01 13:28 抓取）"""
        st = BoschProtocol.parse_state(A0_DATA)
        self.assertIsInstance(st, DeviceState)
        self.assertEqual(st.frame_type, 1)              # data[0]=0x01 → Type1
        self.assertAlmostEqual(st.temp_in, 25.80)       # [1]+[2]/100
        self.assertAlmostEqual(st.chamber, 26.90)       # [3]+[4]/100
        self.assertEqual(st.setpoint, 42)               # [5]=0x2A
        self.assertAlmostEqual(st.temp_box, 26.70)      # [6]+[7]/100
        self.assertEqual(st.water_flow, 0.0)            # [8]/10
        self.assertEqual(st.failure, 0)                 # [11]
        self.assertAlmostEqual(st.temp_out, 25.80)      # Type1: [28]+[29]/100
        self.assertEqual(st.mode, 0)                    # Type1: [32]
        self.assertEqual(st.mode_text, "待机")

    def test_parse_state_burning(self):
        """燃烧态（实测偏移）：功率 [13]、风机 [15]*100+[16]、流量 [21]/10"""
        d = bytearray(A0_DATA)
        d[5] = 48               # 设定 48℃
        d[13] = 44              # 燃烧功率 44%（实测偏移）
        d[10] = 0x10            # 电离 bit4
        d[21] = 111             # 水流量 11.1 L/min（实测偏移）
        d[15], d[16] = 15, 11   # 风机 1511 rpm
        d[28], d[29] = 0x2A, 0x5A   # Type1 出水 42.90
        st = BoschProtocol.parse_state(bytes(d))
        self.assertEqual(st.setpoint, 48)
        self.assertEqual(st.burner_power, 44)
        self.assertTrue(st.burning)
        self.assertTrue(st.ionized)
        self.assertAlmostEqual(st.water_flow, 11.1)
        self.assertEqual(st.fan_speed, 1511)
        self.assertAlmostEqual(st.temp_out, 42.90)

    def test_parse_state_type0_offsets(self):
        """Type0 沿用另一套偏移：出水[21]、功率[9]、风机[13][14]、模式[16]"""
        d = bytearray(A0_DATA)
        d[0] = 0x00                       # Type0
        d[9] = 0x28                       # 功率 40%
        d[13], d[14] = 0x08, 0x70         # 风机功率 8 / 转速 0x70
        d[16] = 0x01                      # 模式 [16]=燃烧
        d[21], d[22] = 0x30, 0x39         # 出水 [21]+[22]/100 = 48.57
        st = BoschProtocol.parse_state(bytes(d))
        self.assertEqual(st.frame_type, 0)
        self.assertEqual(st.burner_power, 40)
        self.assertEqual(st.fan_speed, 0x70)
        self.assertEqual(st.mode_text, "燃烧")
        self.assertAlmostEqual(st.temp_out, 48.57)

    def test_xor_symmetric(self):
        """异或是自反的"""
        raw = bytes(range(30))
        self.assertEqual(BoschProtocol.xor(BoschProtocol.xor(raw)), raw)

    def test_parse_history(self):
        """B0/02 应答：type + 两个大端 32 位计数（2026-11-01 实机：4711 / 1462）"""
        p = bytes.fromhex("02 00 00 12 67 00 00 05 b6")
        info = BoschProtocol.parse_history(p)
        self.assertEqual(info["type"], 2)
        self.assertEqual(info["a"], 0x1267)      # 4711 分钟 ≈ 78.5 小时
        self.assertEqual(info["b"], 0x05B6)      # 1462 次
        self.assertAlmostEqual(info["a"] / 60.0, 78.5, places=1)

    def test_parse_errors(self):
        """D7 应答：[0xEA 状态字节][故障码...]，EA 不是故障码（实测 1/5/10 条均以 EA 开头）"""
        # 无历史故障（App 显示一致）
        self.assertEqual(
            BoschProtocol.parse_errors(bytes.fromhex("ea 00 00 00 00 00 00 00 00 00")), [])
        self.assertEqual(BoschProtocol.parse_errors(bytes.fromhex("ea")), [])
        # 有故障时：状态字节之后才是故障码
        self.assertEqual(
            BoschProtocol.parse_errors(bytes.fromhex("ea 2d 00 1f 00 00")), [0x2D, 0x1F])

    def test_decode_partial_frame(self):
        """半帧应留在缓冲里，不丢数据"""
        frame = BoschProtocol.build(0x00, 0xFF, 1, 0xA0, A0_DATA)
        enc = b"".join(BoschProtocol.encode_chunks(BoschProtocol.stuff(frame)))
        part = BoschProtocol.decrypt_chunk(enc[:20])
        frames, rest = BoschProtocol.decode_stream(part)
        self.assertEqual(frames, [])
        self.assertEqual(rest, part)


if __name__ == "__main__":
    unittest.main(verbosity=2)
