# -*- coding: utf-8 -*-
"""boschlog.py - shared logging setup

Three destinations:

  1. Rotating file bosch_api.log   DEBUG and up, frame-level detail (the primary debugging entry)
  2. In-memory ring buffer (500)   INFO and up, read by the UI / take_logs
  3. Console                       WARN and up, so batch scripts show the important problems

Tag convention (every log call starts with one, so the file stays greppable):
  [CONN]  connect / disconnect / GATT
  [PAIR]  OS-level pairing
  [AUTH]  login, pairing code, authorisation replies
  [TX]    frames sent (DEBUG)
  [RX]    frames received (DEBUG)
  [CMD]   business commands (setpoint and friends)
  [STATE] parsed device state
  [CLOUD] Bemfa cloud
  [SVC]   service lifecycle (start / stop / reconnect plan)
"""
import logging
import logging.handlers
from collections import deque
from pathlib import Path

_buf: deque = deque(maxlen=500)
logger = logging.getLogger("bosch")


class _RingHandler(logging.Handler):
    """Append formatted records to the in-memory ring buffer for the UI."""

    def emit(self, record):
        try:
            _buf.append(self.format(record))
        except Exception:
            pass


def setup(logfile="bosch_api.log", file_level=logging.DEBUG):
    """Configure logging. Safe to call more than once (idempotent)."""
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
    """Return the last n buffered records (INFO and above)."""
    return list(_buf)[-n:]


setup()
