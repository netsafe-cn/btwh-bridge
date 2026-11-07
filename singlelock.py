# -*- coding: utf-8 -*-
"""singlelock.py - process-level single-instance lock (a local port reservation)

Why this exists: two processes talking to the same heater evict each other, and two
processes sharing one Bemfa account feed each other's reports back as commands. The
symptom is "connecting forever" or "the device is not advertising", which is close to
impossible to diagnose. The desktop console and the headless bridge share this lock, so
only one of them runs at a time.

Usage:
    from singlelock import acquire, release
    if not acquire():
        sys.exit("another instance is already running")
"""
import socket

PORT = 47653
_sock = None


def acquire(port: int = PORT) -> bool:
    """Bind a local port as the lock. True = acquired, False = another instance exists."""
    global _sock
    if _sock is not None:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _sock = s          # hold the reference; the OS releases the port on exit
        return True
    except OSError:
        s.close()
        return False


def release():
    """Release explicitly (usually unnecessary - process exit is enough)."""
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
        _sock = None
