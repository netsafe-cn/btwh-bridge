# -*- coding: utf-8 -*-
"""singlelock.py —— 进程级单实例锁（本地端口占位）

为什么需要：两个进程同时连同一个热水器会互相把对方踢下线，同时连同一个
巴法云账号还会把对方上报的值当指令收回来，表现是"一直连接中/扫描不到"，
极难排查。界面和后台桥接共用同一把锁，保证同一时刻只有一个进程在跑。

用法：
    from singlelock import acquire, release
    if not acquire():
        sys.exit("已有实例在运行")
"""
import socket

PORT = 47653
_sock = None


def acquire(port: int = PORT) -> bool:
    """占用本地端口作为锁。True=拿到锁，False=已有实例。"""
    global _sock
    if _sock is not None:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _sock = s          # 保持引用，进程退出时自动释放
        return True
    except OSError:
        s.close()
        return False


def release():
    """显式释放（一般不需要，进程退出即释放）。"""
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
        _sock = None
