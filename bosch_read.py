#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bosch_read.py —— 博世 Therm 4800i 壁挂炉 BLE 读取器（已验证可用）

关键流程（缺一不可）：
  1. 订阅 FIFO(b2) / CREDITS(b3)，CCCD = notify(01 00)
  2. A2/07 登录（携带 6 字节 MAC）
  3. A2/08 提交面板临时码（实测必需，缺此步设备不推送）
  4. (可选) A1 配置广播周期 [type, 秒] —— 实测非必需
  → 设备开始周期性推送 A0 数据帧（43 字节，MTU=23 下被切成 20+20+3 三片）

用法:
  python bosch_read.py                        # 读一次（默认 30 秒）
  python bosch_read.py --loop                 # 持续读取，断线自动重连
  python bosch_read.py --code 123456          # 指定面板临时码
  python bosch_read.py --mac AABBCCDDEEFF     # 指定登录用 MAC
  python bosch_read.py --json                 # JSON 输出（便于接入 MQTT/巴法云）
  python bosch_read.py --no-pass              # 跳过密码（用于验证密码是否必需）
  python bosch_read.py --no-bcast             # 跳过配置广播
"""
import argparse, asyncio, json, sys, time
from bleak import BleakClient

# 参数一律来自 config.json / 环境变量，代码里不写死任何地址或配对码
try:
    from bosch_api import Config as _Config
    _CFG = _Config.load()
    DEV_ADDR = _CFG.device_address
    try:
        DEFAULT_MAC = _CFG.login_mac_bytes
    except ValueError:
        DEFAULT_MAC = b"\x00" * 6
    DEFAULT_CODE = _CFG.pair_code
except Exception:                      # 允许脱离 config 单独使用
    DEV_ADDR = ""
    DEFAULT_MAC = b"\x00" * 6
    DEFAULT_CODE = ""

U = "ff4396a6-50a8-4fc0-bcf6-228e82dfed"
FIFO, CREDITS, MODE = U + "b2", U + "b3", U + "b1"
KEY = b"1ag3.-fs7"
T0 = time.time()


def log(s):
    print("[%6.2fs] %s" % (time.time() - T0, s), flush=True)


# ---------- 帧编解码 ----------
def xor(b: bytes) -> bytes:
    return bytes(v ^ KEY[i % 9] for i, v in enumerate(b))


def unstuff(d: bytes) -> bytes:
    """移除 0x7B/0x7D 后插入的 0x00 填充"""
    if len(d) <= 2:
        return d
    head, body, tail = d[:2], d[2:-2], d[-2:]
    out = bytearray(head)
    skip = False
    for b in body:
        if skip and b == 0x00:
            skip = False
            continue
        out.append(b)
        skip = b in (0x7B, 0x7D)
    return bytes(out) + tail


def enc_pass(code: str) -> bytes:
    """面板临时码 → A2/08 数据，每位数字转 1 字节，如 "123456" -> 01 02 03 04 05 06"""
    return bytes(int(c) for c in code)


def build(sender, receiver, mid, cmd, data=b""):
    body = bytes([sender, receiver, mid, cmd]) + data
    return b"\x7b\x7b" + body + bytes([sum(body) & 0xFF]) + b"\x7d\x7d"


# 加密态帧头（0x7B 0x7B 各自异或密钥后的字节）
FRAME_HEAD_ENC = bytes([0x7B ^ KEY[0], 0x7B ^ KEY[1]])


def decode_stream(buf: bytes):
    """把「已按分片解密」的明文字节流切成完整帧

    重要：设备对每个 notify 分片（≤20 字节）独立从 KEY[0] 开始异或，
    不是整帧连续异或。因此调用方需先对每片单独 xor()，再拼接后送入本函数。
    返回 (完整帧列表, 剩余未完成字节)
    """
    frames = []
    while True:
        if not buf.startswith(b"\x7b\x7b"):
            i = buf.find(b"\x7b\x7b")
            if i < 0:
                return frames, b""
            buf = buf[i:]
        i = buf.find(b"\x7d\x7d", 2)
        if i < 0:
            return frames, buf
        frames.append(unstuff(buf[:i + 2]))
        buf = buf[i + 2:]
    return frames, buf


def parse_a0(data: bytes) -> dict:
    """解码 A0 广播数据帧（字段索引依据 2026-11-01 实机采样）"""
    if len(data) < 7:
        return {}
    t = data[0]
    d = {
        "type": t,
        "t_in": round(data[1] + data[2] / 100.0, 2),        # 进水温度
        "t_alt": round(data[3] + data[4] / 100.0, 2),        # 第二路温度（待确认语义）
        "setpoint": data[5],                                  # 设定温度
    }
    # 出水温度：type 决定索引偏移
    if t == 0 and len(data) > 22:
        d["t_out"] = round(data[21] + data[22] / 100.0, 2)
    elif t == 1 and len(data) > 29:
        d["t_out"] = round(data[28] + data[29] / 100.0, 2)
    d["raw"] = data.hex(" ")
    return d


# ---------- 会话 ----------
class BoschReader:
    def __init__(self, mac=DEFAULT_MAC, code=DEFAULT_CODE, addr=DEV_ADDR,
                 use_pass=True, use_bcast=True, btype=1, bsec=1,
                 verbose=True):
        self.mac, self.code, self.addr = mac, code, addr
        self.use_pass, self.use_bcast = use_pass, use_bcast
        self.btype, self.bsec = btype, bsec
        self.verbose = verbose
        self.buf = b""
        self.frames = []
        self.latest = None
        self.mid = 0

    def say(self, s):
        if self.verbose:
            log(s)

    def on_notify(self, _s, data):
        """notify 分片重组

        MTU=23 下每片最多 20 字节；设备对每片独立从 KEY[0] 开始异或，
        所以先逐片 xor，再拼接成明文帧流后切帧。
        """
        self.buf += xor(bytes(data))
        frames, self.buf = decode_stream(self.buf)
        for f in frames:
            self.handle(f)
        if len(self.buf) > 400:          # 异常保护
            self.buf = b""

    def handle(self, f: bytes):
        if len(f) < 9:
            return
        sender, receiver, mid, cmd = f[2], f[3], f[4], f[5]
        data, chk = f[6:-3], f[-3]
        ok = (sum(f[2:-3]) & 0xFF) == chk
        self.frames.append(f)
        if cmd == 0xA0:
            self.latest = parse_a0(data)
            self.latest["mid"] = mid
            if self.verbose:
                log("A0  %s" % json.dumps(self.latest, ensure_ascii=False))
            else:
                print(json.dumps(self.latest, ensure_ascii=False), flush=True)
        else:
            self.say("RX cmd=%02X mid=%d data=%s chk=%s"
                     % (cmd, mid, data.hex(" "), "OK" if ok else "BAD"))

    async def send(self, c, cmd, data, tag=""):
        f = build(0x21, 0xAB, self.mid, cmd, data)
        self.mid += 1
        await c.write_gatt_char(FIFO, xor(f), response=True)
        self.say(">> %-6s %s" % (tag, f.hex(" ")))
        await asyncio.sleep(1.0)

    async def run(self, dwell=30.0):
        c = BleakClient(self.addr, timeout=20.0)
        await c.connect()
        self.say("已连接 MTU=%s" % getattr(c, "mtu_size", "?"))
        await asyncio.sleep(0.5)                 # 等服务发现完成
        try:
            m = await c.read_gatt_char(MODE)
            self.say("MODE = %s" % bytes(m).hex(" "))
        except Exception as e:
            self.say("MODE 读取跳过 (%s)" % type(e).__name__)
        await c.start_notify(FIFO, self.on_notify, force_indicate=False)
        await asyncio.sleep(0.25)
        await c.start_notify(CREDITS, self.on_notify, force_indicate=False)
        self.say("已订阅 FIFO/CREDITS")

        await self.send(c, 0xA2, bytes([0x07]) + self.mac, "LOGIN")
        if self.use_pass:
            await self.send(c, 0xA2, bytes([0x08]) + enc_pass(self.code), "PASS")
        if self.use_bcast:
            await self.send(c, 0xA1, bytes([self.btype, self.bsec]), "BCAST")

        self.say("等待数据 %.0fs ..." % dwell)
        t_end = time.time() + dwell
        while time.time() < t_end:
            if not c.is_connected:
                self.say("设备断开")
                break
            await asyncio.sleep(1)
        try:
            await c.disconnect()
        except Exception:
            pass
        return self.latest


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", default=DEFAULT_MAC.hex().upper())
    ap.add_argument("--code", default=DEFAULT_CODE)
    ap.add_argument("--addr", default=DEV_ADDR)
    ap.add_argument("--dwell", type=float, default=30.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-pass", action="store_true")
    ap.add_argument("--bcast", action="store_true", help="额外发 A1 配置广播（实测非必需）")
    ap.add_argument("--btype", type=int, default=1)
    ap.add_argument("--bsec", type=int, default=1)
    a = ap.parse_args()

    while True:
        r = BoschReader(bytes.fromhex(a.mac.replace(":", "")), a.code, a.addr,
                        use_pass=not a.no_pass, use_bcast=a.bcast,
                        btype=a.btype, bsec=a.bsec, verbose=not a.json)
        try:
            await r.run(a.dwell)
        except Exception as e:
            log("异常: %s %s" % (type(e).__name__, e))
        if not a.loop:
            break
        log("10 秒后重连 ...")
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
