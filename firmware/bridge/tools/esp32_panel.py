#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Bosch heater ESP32 gateway - desktop panel (always on top, type the six digits)
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
        root.title("Bosch heater ESP32 gateway  -  " + port)
        root.geometry("920x630")
        root.attributes("-topmost", True)
        root.configure(bg="#f5f6f8")

        top = tk.Frame(root, bg="#f5f6f8")
        top.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(top, text="1) panel clear pairing  ->  2) read the 6 digits  ->  3) type them below",
                 font=("Microsoft YaHei UI", 12, "bold"), fg="#1a3d6d", bg="#f5f6f8").pack(anchor="w")
        tk.Label(top, text="A new code is fine, no reflash. Close the PC console/bridge first.",
                 font=("Microsoft YaHei UI", 9), fg="#666", bg="#f5f6f8").pack(anchor="w", pady=(2, 0))

        frm = tk.Frame(root, bg="#f5f6f8")
        frm.pack(fill="x", padx=12, pady=6)
        tk.Label(frm, text="Panel code", font=("Microsoft YaHei UI", 10), bg="#f5f6f8").pack(side="left")
        self.code = tk.StringVar()
        self.entry = ttk.Entry(frm, textvariable=self.code, font=("Consolas", 20), width=10, justify="center")
        self.entry.pack(side="left", padx=8)
        self.entry.bind("<Return>", lambda _e: self.send_code())
        ttk.Button(frm, text="Send and connect", command=self.send_code).pack(side="left", padx=4)
        self.status = tk.Label(frm, text="waiting for input...", font=("Microsoft YaHei UI", 10), fg="#c0392b", bg="#f5f6f8")
        self.status.pack(side="left", padx=12)

        bar = tk.Frame(root, bg="#f5f6f8")
        bar.pack(fill="x", padx=12, pady=(2, 6))
        for label, cmd in [("scan", "scan"), ("pair (force re-pair)", "pair"),
                           ("go (reconnect)", "go"), ("status", "status"),
                           ("raw on", "raw on"), ("raw off", "raw off"), ("reboot", "reboot")]:
            ttk.Button(bar, text=label, command=lambda c=cmd: self.send(c)).pack(side="left", padx=3)
        ttk.Button(bar, text="Clear log", command=self.clear_log).pack(side="right", padx=3)

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
            self.q.put(("sys", "serial connected: " + self.port + " @ 115200"))
            self.ser.write(b"reboot\n")
            self.ser.flush()
        except Exception as e:
            self.ser = None
            self.q.put(("err", "cannot open %s: %s" % (self.port, e)))

    def reader(self):
        while not self.stop.is_set():
            if self.ser is None:
                time.sleep(1)
                continue
            try:
                raw = self.ser.readline()
            except Exception as e:
                self.q.put(("err", "serial closed: %s" % e))
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
            if re.search(r"authorised|authentication complete|subscribed|reply = 01", line): tag = "ok"
            elif re.search(r"cannot connect|rejected|dropping|error|timeout|no reply|WARN", line, re.I): tag = "err"
            elif re.search(r"\[SMP\]|\[AUTH\]|\[CONN\]|\[TX\]", line): tag = "info"
            elif re.search(r"waiting for input|Enter the six", line): tag = "warn"
        self.txt.insert("end", line + "\n", tag or "dim")
        self.txt.see("end")
        self.logfile.write(line + "\n")
        if "authorised" in line: self.status.config(text="authorised, waiting for A0", fg="#137333")
        elif "[result] x" in line:
            # The firmware ends every failed attempt with the reason code, e.g. (window_closed).
            m = re.search(r"\(([a-z_]+)\)", line)
            self.status.config(text="failed: %s" % (m.group(1) if m else "see log"), fg="#c0392b")
        elif "rejected" in line: self.status.config(text="failed; read a new code and retry", fg="#c0392b")
        elif "[A0]" in line: self.status.config(text="running, receiving A0", fg="#137333")

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
            except Exception as e: self.append("send failed: %s" % e, "err")

    def send_code(self):
        code = self.code.get().strip()
        if not re.fullmatch(r"\d{6}", code):
            self.status.config(text="enter six digits", fg="#c0392b")
            self.entry.focus_set()
            return
        self.status.config(text="sent %s, connecting..." % code, fg="#1a56a0")
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
        print("no serial port (CH343); check the board is plugged in.")
        sys.exit(1)
    r = tk.Tk()
    Panel(r, port)
    r.mainloop()
