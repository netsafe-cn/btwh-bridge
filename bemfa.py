# -*- coding: utf-8 -*-
"""bemfa.py —— 巴法云 TCP 客户端（不依赖 BLE，可单独复用）

作用：把本地数据同步到米家 / 小爱，并接收下发指令。

主题名规则（决定米家里的设备类型，必须注意）：
    001 插座 / 002 灯泡 / 003 风扇 / 004 传感器 / 005 空调 / 006 开关 / 009 窗帘
    010 温控器 / 011 热水器 / 012 电视 / 013 空气净化器
其中**只有 001~009 会同步到米家**，010 及之后只是普通主题节点。

传感器(004) 报文格式： `#温度#湿度#开关#`（只报温度就 `#25.4`）

用法:
    cli = BemfaClient(uid, ["boschset004", "boschup006"])
    cli.on_command = lambda topic, msg: print(topic, msg)
    cli.start()
    cli.push("boschset004", "#38")
    cli.stop()
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Callable, Optional, Sequence

ECHO_WINDOW = 8.0       # 自己发出的消息在这么多秒内视为回环，忽略
PING_INTERVAL = 25.0


class BemfaClient:
    def __init__(self, uid: str, topics: Sequence[str],
                 host: str = "bemfa.com", port: int = 8344):
        self.uid = (uid or "").strip()
        self.topics = [t for t in topics if t]
        self.host, self.port = host, port

        self.on_command: Optional[Callable[[str, str], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.status = "未启动"

        self._sock: Optional[socket.socket] = None
        self._send_q: "queue.Queue[tuple]" = queue.Queue()
        self._echo = {}
        self._buf = ""
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期
    def start(self):
        if not self.uid:
            self.status = "未配置"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="bemfa")
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop = True
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 对外
    def push(self, topic: str, msg):
        """异步发送一条消息（进队列，由后台线程写出）"""
        if self.uid and topic:
            self._send_q.put((topic, str(msg)))

    # ---- 内部
    def _log(self, s):
        if self.on_log:
            self.on_log(s)

    def _run(self):
        while not self._stop:
            try:
                self._connect()
                self._loop()
            except Exception as e:
                self._log("巴法云断开: %s" % type(e).__name__)
            self.status = "断开"
            self._sock = None
            for _ in range(50):
                if self._stop:
                    return
                time.sleep(0.1)

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(1.0)
        for t in self.topics:
            sock.sendall(("cmd=1&uid=%s&topic=%s\r\n" % (self.uid, t)).encode())
        self._sock = sock
        self._buf = ""
        self.status = "已连接"
        self._log("巴法云已连接，订阅 %s" % ", ".join(self.topics))

    def _loop(self):
        last_ping = time.time()
        while not self._stop and self._sock:
            # 1) 发送队列
            try:
                topic, msg = self._send_q.get(timeout=0.2)
                self._write(topic, msg)
            except queue.Empty:
                pass

            # 2) 接收
            try:
                data = self._sock.recv(4096)
                if not data:
                    raise RuntimeError("closed")
                self._buf += data.decode("utf-8", "ignore")
                while "\r\n" in self._buf:
                    line, self._buf = self._buf.split("\r\n", 1)
                    self._on_line(line.strip())
            except socket.timeout:
                pass

            # 3) 心跳
            if time.time() - last_ping > PING_INTERVAL:
                try:
                    self._sock.sendall(b"ping\r\n")
                except Exception:
                    return
                last_ping = time.time()

    def _write(self, topic: str, msg: str):
        if not self._sock:
            return
        try:
            self._sock.sendall(("cmd=2&uid=%s&topic=%s&msg=%s\r\n"
                                % (self.uid, topic, msg)).encode())
            self._echo[(topic, msg)] = time.time()
            self._log("→ %s = %s" % (topic, msg))
        except Exception:
            self._sock = None

    def _on_line(self, line: str):
        if not line or line.startswith("pong"):
            return
        if "topic=" not in line or "msg=" not in line:
            return
        kv = dict(p.split("=", 1) for p in line.split("&") if "=" in p)
        topic, msg = kv.get("topic"), kv.get("msg", "")
        key = (topic, msg)
        t = self._echo.get(key)
        if t is not None and time.time() - t < ECHO_WINDOW:
            self._echo.pop(key, None)      # 自己刚发的回环，忽略
            return
        self._log("← %s = %s" % (topic, msg))
        if self.on_command:
            try:
                self.on_command(topic, msg)
            except Exception:
                pass


__all__ = ["BemfaClient"]
