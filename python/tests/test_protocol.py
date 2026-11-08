# -*- coding: utf-8 -*-
"""Protocol-layer tests

Run:  python run_tests.py -v   (from the repository root)
Every case is pinned to a frame captured on hardware, so a failure means the codec broke.
"""
import unittest

from bosch_api import BoschProtocol, DeviceState

# Real A0 state frame: 34-byte data, Type1, captured 2026-11-01 13:28, setpoint 42 C
# Device readings only; nothing personally identifying
A0_DATA = bytes.fromhex(
    "01 19 50 1a 5a 2a 1a 46 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 "
    "00 00 00 00 00 00 19 50 00 00 00 00")

# The next two are structural reference frames, MAC and code replaced by samples
DEMO_MAC = bytes.fromhex("AABBCCDDEEFF")
DEMO_CODE = "123456"

REAL_LOGIN = "7b 7b 21 ab 00 a2 07 aa bb cc dd ee ff 70 7d 7d"
REAL_SET = "7b 7b 21 00 02 d3 02 55 00 26 73 7d 7d"


class TestProtocol(unittest.TestCase):

    def test_pair_code(self):
        """One byte per digit of the pairing code"""
        self.assertEqual(BoschProtocol.encode_pair_code(DEMO_CODE),
                         bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06]))

    def test_build_login_frame(self):
        """Login frame structure (the reference frame has an anonymised MAC)"""
        f = BoschProtocol.build(0x21, 0xAB, 0, 0xA2, bytes([0x07]) + DEMO_MAC)
        self.assertEqual(f.hex(" "), REAL_LOGIN)

    def test_build_set_temperature_frame(self):
        """Setpoint frame matches the hardware capture byte for byte"""
        f = BoschProtocol.build(0x21, 0x00, 2, 0xD3, bytes([0x02, 0x55, 0x00, 0x26]))
        self.assertEqual(f.hex(" "), REAL_SET)

    def test_checksum(self):
        """Checksum = low 8 bits of the sum from sender to data"""
        f = BoschProtocol.build(0x21, 0x00, 2, 0xD3, bytes([0x02, 0x55, 0x00, 0x26]))
        body = bytes.fromhex("210002d302550026")
        self.assertEqual(f[-3], sum(body) & 0xFF)

    def test_stuffing_roundtrip(self):
        """7B/7D in the data area: stuff -> encrypt -> decrypt -> unstuff round-trips"""
        data = bytes([0x7B, 0x00, 0x7D, 0x7B, 0x11])
        frame = BoschProtocol.build(0x21, 0x00, 5, 0xD2, data)
        wire = BoschProtocol.stuff(frame)
        self.assertGreater(len(wire), len(frame), "stuffing bytes should have been inserted")

        enc = b"".join(BoschProtocol.encode_chunks(wire))
        frames, rest = BoschProtocol.decode_stream(
            BoschProtocol.decrypt_chunk(enc))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)
        self.assertEqual(rest, b"")

    def test_a0_chunked_roundtrip(self):
        """A 43-byte state frame splits into 20+20+3 at MTU 23 and must reassemble

        This is where it is easiest to go wrong: each fragment restarts the XOR at KEY[0].
        """
        frame = BoschProtocol.build(0x00, 0xFF, 0x6D, 0xA0, A0_DATA)
        self.assertEqual(len(frame), 43)
        wire = BoschProtocol.stuff(frame)
        self.assertEqual(len(wire), 43, "no delimiter inside, so nothing should be stuffed")

        enc = b"".join(BoschProtocol.encode_chunks(wire))
        self.assertEqual(len(enc), 43)

        buf, got = b"", []
        for i in range(0, len(enc), 20):          # simulate the stack splitting it
            buf += BoschProtocol.decrypt_chunk(enc[i:i + 20])
            frames, buf = BoschProtocol.decode_stream(buf)
            got.extend(frames)

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], frame)

    def test_parse_state(self):
        """Field parsing on the real Type1 frame (2026-11-01 13:28)"""
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
        self.assertEqual(st.mode_text, "idle")

    def test_parse_state_burning(self):
        """Burning state, measured offsets: power [13], fan [15]*100+[16], flow [21]/10"""
        d = bytearray(A0_DATA)
        d[5] = 48               # setpoint 48 C
        d[13] = 44              # burner power 44% (measured offset)
        d[10] = 0x10            # ionisation, bit 4
        d[21] = 111             # flow 11.1 L/min (measured offset)
        d[15], d[16] = 15, 11   # fan 1511 rpm
        d[28], d[29] = 0x2A, 0x5A   # Type1 outlet 42.90
        st = BoschProtocol.parse_state(bytes(d))
        self.assertEqual(st.setpoint, 48)
        self.assertEqual(st.burner_power, 44)
        self.assertTrue(st.burning)
        self.assertTrue(st.ionized)
        self.assertAlmostEqual(st.water_flow, 11.1)
        self.assertEqual(st.fan_speed, 1511)
        self.assertAlmostEqual(st.temp_out, 42.90)

    def test_parse_state_type0_offsets(self):
        """Type0 uses its own offsets: outlet [21], power [9], fan [13][14], mode [16]"""
        d = bytearray(A0_DATA)
        d[0] = 0x00                       # Type0
        d[9] = 0x28                       # power 40%
        d[13], d[14] = 0x08, 0x70         # fan power 8 / speed 0x70
        d[16] = 0x01                      # mode [16] = burning
        d[21], d[22] = 0x30, 0x39         # outlet [21]+[22]/100 = 48.57
        st = BoschProtocol.parse_state(bytes(d))
        self.assertEqual(st.frame_type, 0)
        self.assertEqual(st.burner_power, 40)
        self.assertEqual(st.fan_speed, 0x70)
        self.assertEqual(st.mode_text, "burning")
        self.assertAlmostEqual(st.temp_out, 48.57)

    def test_xor_symmetric(self):
        """XOR is its own inverse"""
        raw = bytes(range(30))
        self.assertEqual(BoschProtocol.xor(BoschProtocol.xor(raw)), raw)

    def test_parse_history(self):
        """B0/02 reply: type plus two big-endian 32-bit counters (4711 / 1462 on hardware)"""
        p = bytes.fromhex("02 00 00 12 67 00 00 05 b6")
        info = BoschProtocol.parse_history(p)
        self.assertEqual(info["type"], 2)
        self.assertEqual(info["a"], 0x1267)      # 4711 minutes, about 78.5 hours
        self.assertEqual(info["b"], 0x05B6)      # 1462 ignitions
        self.assertAlmostEqual(info["a"] / 60.0, 78.5, places=1)

    def test_parse_errors(self):
        """D7 reply: [0xEA status byte][fault codes...]; EA is not a fault code"""
        # no stored faults, matching what the app shows
        self.assertEqual(
            BoschProtocol.parse_errors(bytes.fromhex("ea 00 00 00 00 00 00 00 00 00")), [])
        self.assertEqual(BoschProtocol.parse_errors(bytes.fromhex("ea")), [])
        # with faults, the codes start after the status byte
        self.assertEqual(
            BoschProtocol.parse_errors(bytes.fromhex("ea 2d 00 1f 00 00")), [0x2D, 0x1F])

    def test_decode_partial_frame(self):
        """A partial frame stays in the buffer; no data is lost"""
        frame = BoschProtocol.build(0x00, 0xFF, 1, 0xA0, A0_DATA)
        enc = b"".join(BoschProtocol.encode_chunks(BoschProtocol.stuff(frame)))
        part = BoschProtocol.decrypt_chunk(enc[:20])
        frames, rest = BoschProtocol.decode_stream(part)
        self.assertEqual(frames, [])
        self.assertEqual(rest, part)


if __name__ == "__main__":
    unittest.main(verbosity=2)
