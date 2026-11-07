# -*- coding: utf-8 -*-
"""boschlog.py —— 统一日志模块

三级输出：
  1. 轮转文件 bosch_api.log    DEBUG 起，含全部帧级细节（调试主入口）
  2. 内存环形缓冲（500 条）    INFO 起，UI / take_logs 直接取用
  3. 控制台                    WARN 起（后台脚本跑批时看关键问题）

日志标签约定（写日志时统一前缀，便于 grep）：
  [CONN]  连接 / 断开 / GATT
  [PAIR]  系统配对
  [AUTH]  登录 / 配对码 / 授权应答
  [TX]    发出的帧（DEBUG 级）
  [RX]    收到的帧（DEBUG 级）
  [CMD]   业务命令（设温等）
  [STATE] 解析后的设备状态
  [CLOUD] 巴法云
  [SVC]   服务生命周期（启动/停止/重连计划）
"""
import logging
import logging.handlers
from collections import deque
from pathlib import Path

_buf: deque = deque(maxlen=500)
logger = logging.getLogger("bosch")


class _RingHandler(logging.Handler):
    """写入内存环形缓冲，供 UI 即时取用。"""

    def emit(self, record):
        try:
            _buf.append(self.format(record))
        except Exception:
            pass


def setup(logfile="bosch_api.log", file_level=logging.DEBUG):
    """初始化日志。重复调用安全（幂等）。"""
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(message)s",
        "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        Path(__file__).parent / logfile,
        maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(file_level)
    logger.addHandler(fh)
    ring = _RingHandler()
    ring.setFormatter(fmt)
    ring.setLevel(logging.INFO)
    logger.addHandler(ring)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.WARNING)
    logger.addHandler(console)
    return logger


def tail(n=200):
    """取最近 n 条内存日志（INFO 以上）。"""
    return list(_buf)[-n:]


setup()
