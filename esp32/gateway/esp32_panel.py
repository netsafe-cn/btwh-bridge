#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 博世热水器 ESP32 网关 —— 桌面控制面板（置顶，直接输 6 位码）
from __future__ import annotations
import datetime, pathlib, queue, re, sys, threading, time
import tkinter as tk
from tkinter import ttk, scrolledtext

HERE = pathlib.Path(__file__).resolve().parent
LOGDIR = HERE.parent / "logs"
LOGDIR.mkdir(parents=True, exist_ok=True)
LOGFILE = LOGDIR / ("panel_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")


def find_port():
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None
    ports = list(lp.comports())
    for p in ports:
        if (p.vid, p.pid) in ((0x1A86, 0x55D3), (0x1A86, 0x7523)) or "CH343" in (p.description or ""):
            return p.device
    for p in ports:
        if p.device.upper().startswith("COM"):
            return p.device
    return None


class Panel:
    def __init__(self, root, port):
        self.root, self.port = root, port
        self.q = queue.Queue()
        self.ser = None
        self.stop = threading.Event()
        self.logfile = open(LOGFILE, "a", encoding="utf-8", errors="replace", buffering=1)
        root.title("博世热水器 ESP32 网关  ·  " + port)
        root.geometry("920x630")
        root.attributes("-topmost", True)
        root.configure(bg="#f5f6f8")

        top = tk.Frame(root, bg="#f5f6f8")
        top.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(top, text="① 面板『清除配对』 → ② 取 6 位码 → ③ 在下面输入并发送",
                 font=("Microsoft YaHei UI", 12, "bold"), fg="#1a3d6d", bg="#f5f6f8").pack(anchor="w")
        tk.Label(top, text="码过期就重取再输一次，不用重新烧录；电脑上的 Python 界面/桥接请先关掉。",
                 font=("Microsoft YaHei UI", 9), fg="#666", bg="#f5f6f8").pack(anchor="w", pady=(2, 0))

        frm = tk.Frame(root, bg="#f5f6f8")
        frm.pack(fill="x", padx=12, pady=6)
        tk.Label(frm, text="面板配对码", font=("Microsoft YaHei UI", 10), bg="#f5f6f8").pack(side="left")
        self.code = tk.StringVar()
        self.entry = ttk.Entry(frm, textvariable=self.code, font=("Consolas", 20), width=10, justify="center")
        self.entry.pack(side="left", padx=8)
        self.entry.bind("<Return>", lambda _e: self.send_code())
        ttk.Button(frm, text="发送并开始连接", command=self.send_code).pack(side="left", padx=4)
        self.status = tk.Label(frm, text="等待输入…", font=("Microsoft YaHei UI", 10), fg="#c0392b", bg="#f5f6f8")
        self.status.pack(side="left", padx=12)

        bar = tk.Frame(root, bg="#f5f6f8")
        bar.pack(fill="x", padx=12, pady=(2, 6))
        for label, cmd in [("scan 扫描", "scan"), ("pair 强制重配对", "pair"), ("go 重连", "go"),
                           ("status 状态", "status"), ("raw on", "raw on"), ("raw off", "raw off"),
                           ("reboot 重启", "reboot")]:
            ttk.Button(bar, text=label, command=lambda c=cmd: self.send(c)).pack(side="left", padx=3)
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="right", padx=3)

        self.txt = scrolledtext.ScrolledText(root, font=("Consolas", 10), bg="#fff", fg="#222", wrap="none")
        self.txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        for tag, color in [("ok", "#137333"), ("warn", "#b06000"), ("err", "#c0392b"),
                           ("info", "#1a56a0"), ("dim", "#888")]:
            self.txt.tag_config(tag, foreground=color)
        self.txt.tag_config("ok", font=("Consolas", 10, "bold"))
        self.open_serial()
        threading.Thread(target=self.reader, daemon=True).start()
        root.after(80, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def open_serial(self):
        import serial
        try:
            self.ser = serial.Serial(self.port, 115200, timeout=0.1)
            self.q.put(("sys", "串口已连接：" + self.port + " @ 115200"))
            self.ser.write(b"reboot\n")
            self.ser.flush()
        except Exception as e:
            self.ser = None
            self.q.put(("err", "打不开串口 %s：%s" % (self.port, e)))

    def reader(self):
        while not self.stop.is_set():
            if self.ser is None:
                time.sleep(1)
                continue
            try:
                raw = self.ser.readline()
            except Exception as e:
                self.q.put(("err", "串口断开：%s" % e))
                self.ser = None
                continue
            if raw:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self.q.put(("line", "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), line)))

    def pump(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "line": self.append(text)
                elif kind == "sys": self.append("[--] " + text, "dim")
                else: self.append("[!!] " + text, "err")
        except queue.Empty:
            pass
        self.root.after(80, self.pump)

    def append(self, line, forced=None):
        tag = forced
        if tag is None:
            if re.search(r"授权成功|认证完成|已订阅|应答 = 01", line): tag = "ok"
            elif re.search(r"失败|被拒绝|丢弃|错误|超时|无应答|WARN", line, re.I): tag = "err"
            elif re.search(r"\[SMP\]|\[AUTH\]|\[CONN\]|\[TX\]", line): tag = "info"
            elif re.search(r"等待输入|请先输入", line): tag = "warn"
        self.txt.insert("end", line + "\n", tag or "dim")
        self.txt.see("end")
        self.logfile.write(line + "\n")
        if "授权成功" in line: self.status.config(text="已授权，等待 A0 数据", fg="#137333")
        elif "被拒绝" in line or "未通过" in line: self.status.config(text="失败，请重取码再输一次", fg="#c0392b")
        elif "[A0]" in line: self.status.config(text="运行中，正在收 A0 数据", fg="#137333")

    def clear_log(self):
        self.txt.delete("1.0", "end")

    def send(self, s):
        if not s: return
        self.append("[TX] " + s, "info")
        self.logfile.write("[TX] " + s + "\n")
        if self.ser:
            try:
                self.ser.write((s + "\n").encode("ascii", "replace"))
                self.ser.flush()
            except Exception as e: self.append("发送失败：%s" % e, "err")

    def send_code(self):
        code = self.code.get().strip()
        if not re.fullmatch(r"\d{6}", code):
            self.status.config(text="请输入 6 位数字", fg="#c0392b")
            self.entry.focus_set()
            return
        self.status.config(text="已发送 %s，连接中…" % code, fg="#1a56a0")
        self.send(code)
        self.code.set("")
        self.entry.focus_set()

    def on_close(self):
        self.stop.set()
        try:
            if self.ser: self.ser.close()
        except Exception: pass
        self.logfile.close()
        self.root.destroy()


if __name__ == "__main__":
    port = find_port()
    if not port:
        print("找不到串口（CH343），请确认板子已插好。")
        sys.exit(1)
    r = tk.Tk()
    Panel(r, port)
    r.mainloop()
