# -*- coding: utf-8 -*-

# tools/ sits one level below the library modules; make the repo root importable.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

"""bosch_ui.py —— 博世热水器桌面控制台（界面层）

只负责显示与交互，所有蓝牙逻辑都在 bosch_api.BoschService 里。
换界面（Qt / Web / 命令行）只需重写这一个文件，api 不用动。

启动:  双击 start_ui.bat   或   python bosch_ui.py
"""
import socket
import sys
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox

from bemfa import BemfaClient
from bosch_api import Config, BoschService, local_bt_mac, TEMP_MIN, TEMP_MAX

APP_NAME = "博世热水器控制台"
VERSION = "1.2"
LOCK_PORT = 47653          # 单实例锁：占用本地端口，重复启动会被拦下
_lock_sock = None


def acquire_single_instance(port: int = LOCK_PORT) -> bool:
    """确保只有一个实例在跑（界面与后台桥接共用同一把锁）。

    这是刚需，不是洁癖：两个实例会同时连同一个热水器，互相把对方踢下线，
    同时连同一个巴法云账号，把对方上报的值当指令收回来——表现就是
    “一直显示连接中、设备扫描不到”，很难排查。
    """
    global _lock_sock
    from singlelock import acquire
    ok = acquire(port)
    if ok:
        _lock_sock = True
    return ok

BG = "#f2f4f7"
CARD = "#ffffff"
FG = "#1b1f24"
MUTED = "#6b7280"
ACCENT = "#1f6feb"
OK = "#16a34a"
WARN = "#d97706"
ERR = "#dc2626"


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = Config.load()
        self.svc = BoschService(self.cfg)
        self.svc.start()

        # 巴法云桥接（可选，uid 为空则不启用）
        self.bemfa = BemfaClient(
            self.cfg.bemfa_uid,
            [self.cfg.topic_set, self.cfg.topic_up, self.cfg.topic_down])
        self.bemfa.on_command = self._on_cloud_cmd
        self.bemfa.on_log = self._cloud_log
        self.bemfa.start()
        self._last_report = 0.0
        self._cloud_logs = []
        self.hist = deque(maxlen=180)      # 出水温度历史点（约 10 分钟）
        self._last_curve_ts = 0.0

        root.title("%s v%s" % (APP_NAME, VERSION))
        root.geometry("500x800")
        root.minsize(470, 700)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._tick()

    # ---------------------------------------------------------------- 布局
    def _card(self, parent, pady=(0, 12)):
        f = tk.Frame(parent, bg=CARD, highlightbackground="#e3e6eb",
                     highlightthickness=1)
        f.pack(fill="x", padx=16, pady=pady)
        return f

    def _build(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(top, text="博世热水器", bg=BG, fg=FG,
                 font=("Microsoft YaHei", 15, "bold")).pack(side="left")
        self.dot = tk.Label(top, text="●", bg=BG, fg=MUTED,
                            font=("Microsoft YaHei", 13))
        self.dot.pack(side="right")
        self.conn_lbl = tk.Label(top, text="启动中", bg=BG, fg=MUTED,
                                 font=("Microsoft YaHei", 9))
        self.conn_lbl.pack(side="right", padx=(0, 6))
        tk.Button(top, text="设置", font=("Microsoft YaHei", 9), bd=0,
                  bg=BG, fg=ACCENT, activebackground=BG, cursor="hand2",
                  command=self._open_settings).pack(side="right", padx=(0, 14))

        bar2 = tk.Frame(self.root, bg=BG)
        bar2.pack(fill="x", padx=18, pady=(0, 4))
        self.bemfa_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar2, text="同步到米家", variable=self.bemfa_var,
                       bg=BG, fg=MUTED, activebackground=BG, selectcolor=CARD,
                       font=("Microsoft YaHei", 9), bd=0, highlightthickness=0,
                       command=self._toggle_bemfa).pack(side="left")
        self.bemfa_lbl = tk.Label(bar2, text="巴法云 未开始", bg=BG, fg=MUTED,
                                  font=("Microsoft YaHei", 9))
        self.bemfa_lbl.pack(side="right")

        self.login_bar = tk.Label(self.root, text="登录：未开始", bg=BG,
                                  fg=MUTED, font=("Microsoft YaHei", 9),
                                  anchor="w")
        self.login_bar.pack(fill="x", padx=18, pady=(0, 4))

        # 动态配对码：面板进配对菜单生成，超时很短，所以要在这里快速输入
        prow = tk.Frame(self.root, bg=BG)
        prow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(prow, text="配对码", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.pair_var = tk.StringVar()
        self.pair_entry = tk.Entry(prow, textvariable=self.pair_var, width=8,
                                   font=("Microsoft YaHei", 11, "bold"),
                                   relief="solid", bd=1, justify="center")
        self.pair_entry.pack(side="left", padx=(6, 6))
        tk.Button(prow, text="立即连接", font=("Microsoft YaHei", 9),
                  bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                  activebackground="#1a5fd0", bd=0, padx=10,
                  command=self._quick_pair).pack(side="left")
        tk.Label(prow, text="面板配对菜单生成，超时很短", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=(10, 0))
        self.pair_entry.bind("<Return>", lambda e: self._quick_pair())

        # 当前水温
        c1 = self._card(self.root)
        tk.Label(c1, text="出水温度", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei", 10)).pack(anchor="w", padx=18,
                                                    pady=(14, 0))
        row = tk.Frame(c1, bg=CARD)
        row.pack(anchor="w", padx=18)
        self.temp_big = tk.Label(row, text="--", bg=CARD, fg=FG,
                                 font=("Microsoft YaHei Light", 52))
        self.temp_big.pack(side="left")
        tk.Label(row, text="℃", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei", 16)).pack(side="left", pady=(18, 0),
                                                    padx=(2, 0))
        self.mode_lbl = tk.Label(row, text="", bg=CARD, fg=ACCENT,
                                 font=("Microsoft YaHei", 11))
        self.mode_lbl.pack(side="left", padx=(14, 0), pady=(26, 0))

        # 第一行：温度族
        sub = tk.Frame(c1, bg=CARD)
        sub.pack(fill="x", padx=18, pady=(0, 4))
        self.t_in = tk.Label(sub, text="进水 --", bg=CARD, fg=MUTED,
                             font=("Microsoft YaHei", 10))
        self.t_in.pack(side="left")
        self.t_ch = tk.Label(sub, text="炉腔 --", bg=CARD, fg=MUTED,
                             font=("Microsoft YaHei", 10))
        self.t_ch.pack(side="left", padx=14)
        self.t_box = tk.Label(sub, text="机身 --", bg=CARD, fg=MUTED,
                              font=("Microsoft YaHei", 10))
        self.t_box.pack(side="left")
        self.flow = tk.Label(sub, text="水流 --", bg=CARD, fg=MUTED,
                             font=("Microsoft YaHei", 10))
        self.flow.pack(side="left", padx=14)
        self.burn = tk.Label(sub, text="待机", bg=CARD, fg=MUTED,
                             font=("Microsoft YaHei", 10))
        self.burn.pack(side="right")

        # 第二行：运行细节（风机/电离/故障/最大设定）
        sub2 = tk.Frame(c1, bg=CARD)
        sub2.pack(fill="x", padx=18, pady=(0, 14))
        self.fan = tk.Label(sub2, text="风机 --", bg=CARD, fg=MUTED,
                            font=("Microsoft YaHei", 9))
        self.fan.pack(side="left")
        self.ion = tk.Label(sub2, text="", bg=CARD, fg=MUTED,
                            font=("Microsoft YaHei", 9))
        self.ion.pack(side="left", padx=12)
        self.maxsp = tk.Label(sub2, text="", bg=CARD, fg=MUTED,
                              font=("Microsoft YaHei", 9))
        self.maxsp.pack(side="left")
        self.fault = tk.Label(sub2, text="故障 无", bg=CARD, fg=MUTED,
                              font=("Microsoft YaHei", 9))
        self.fault.pack(side="right")

        # 出水温度曲线（最近约 10 分钟）
        self.curve = tk.Canvas(c1, height=64, bg=CARD, highlightthickness=0)
        self.curve.pack(fill="x", padx=18, pady=(2, 12))
        self.curve.bind("<Configure>", lambda e: self._draw_curve())

        # 设定温度
        c2 = self._card(self.root)
        head = tk.Frame(c2, bg=CARD)
        head.pack(fill="x", padx=18, pady=(14, 4))
        tk.Label(head, text="设定温度", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei", 10)).pack(side="left")
        self.set_lbl = tk.Label(head, text="--", bg=CARD, fg=ACCENT,
                                font=("Microsoft YaHei", 10, "bold"))
        self.set_lbl.pack(side="right")

        btns = tk.Frame(c2, bg=CARD)
        btns.pack(fill="x", padx=18, pady=(4, 6))
        for txt, delta in (("-5", -5), ("-1", -1), ("+1", 1), ("+5", 5)):
            tk.Button(btns, text=txt, width=6, font=("Microsoft YaHei", 11),
                      bg="#eef1f5", fg=FG, relief="flat", cursor="hand2",
                      activebackground="#dfe4ea",
                      command=lambda d=delta: self._nudge(d)).pack(
                side="left", padx=(0, 8), pady=2)

        self.scale = tk.Scale(c2, from_=TEMP_MIN, to=TEMP_MAX,
                              orient="horizontal", showvalue=False, bg=CARD,
                              fg=FG, highlightthickness=0,
                              troughcolor="#e6e9ee", activebackground=ACCENT,
                              sliderlength=26, bd=0, command=self._on_slide)
        self.scale.pack(fill="x", padx=18, pady=(2, 6))

        apply_row = tk.Frame(c2, bg=CARD)
        apply_row.pack(fill="x", padx=18, pady=(0, 14))
        tk.Label(apply_row, text="直接输入", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.entry = tk.Entry(apply_row, width=5, justify="center",
                              font=("Microsoft YaHei", 11), relief="solid",
                              bd=1)
        self.entry.pack(side="left", padx=6)
        tk.Button(apply_row, text="应用", font=("Microsoft YaHei", 10),
                  bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                  activebackground="#1a5fd0",
                  command=self._apply_entry).pack(side="left")
        self.hint = tk.Label(apply_row, text="", bg=CARD, fg=MUTED,
                             font=("Microsoft YaHei", 9))
        self.hint.pack(side="right")

        # 日志
        c3 = self._card(self.root, pady=(0, 16))
        head3 = tk.Frame(c3, bg=CARD)
        head3.pack(fill="x", padx=18, pady=(10, 2))
        tk.Label(head3, text="日志", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(head3, text="读故障", font=("Microsoft YaHei", 8), bd=0,
                  bg=CARD, fg=ACCENT, activebackground=CARD, cursor="hand2",
                  command=self._query_errors).pack(side="right")
        tk.Button(head3, text="读历史", font=("Microsoft YaHei", 8), bd=0,
                  bg=CARD, fg=ACCENT, activebackground=CARD, cursor="hand2",
                  command=self._query_history).pack(side="right", padx=(0, 12))
        self.logbox = tk.Text(c3, height=7, bg=CARD, fg="#374151", relief="flat",
                              font=("Consolas", 9), state="disabled",
                              wrap="none")
        self.logbox.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ---------------------------------------------------------------- 设置
    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        W, H = 470, 640
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - W) // 2
        y = max(16, min(self.root.winfo_rooty() + 24,
                        self.root.winfo_screenheight() - H - 70))
        win.geometry("%dx%d+%d+%d" % (W, H, x, y))

        fields = [
            ("device_address", "热水器蓝牙地址", "如 AA:BB:CC:DD:EE:FF"),
            ("login_mac", "本机蓝牙地址", "登录身份，设备白名单用"),
            ("pair_code", "配对码 / 临时码", "面板 6 位数字"),
            ("bemfa_uid", "巴法云私钥", "留空则不同步米家"),
            ("topic_set", "主题：设定温度", "传感器，以 004 结尾"),
            ("topic_up", "主题：升温", "开关，以 006 结尾"),
            ("topic_down", "主题：降温", "开关，以 006 结尾"),
        ]
        vars_ = {}

        def read_local():
            mac = local_bt_mac()
            if mac:
                vars_["login_mac"].set(mac)
            else:
                messagebox.showinfo("提示", "没读到本机蓝牙地址，请手动填写",
                                    parent=win)

        def do_save():
            for k, v in vars_.items():
                setattr(self.cfg, k, v.get().strip())
            if self.cfg.login_mac:
                self.cfg.login_mac = (self.cfg.login_mac.replace(":", "")
                                      .replace("-", "").upper())
                if len(self.cfg.login_mac) != 12:
                    messagebox.showwarning(
                        "提示", "本机蓝牙地址应为 12 位十六进制", parent=win)
                    return
            self.cfg.save()
            self.svc.update_config(self.cfg)
            self._reload_bemfa()
            win.destroy()

        foot = tk.Frame(win, bg=BG)
        foot.pack(side="bottom", fill="x", padx=18, pady=(6, 14))
        tk.Button(foot, text="读取本机蓝牙地址", font=("Microsoft YaHei", 9),
                  bg="#eef1f5", fg=FG, relief="flat", cursor="hand2",
                  command=read_local).pack(side="left")
        tk.Button(foot, text="取消", font=("Microsoft YaHei", 10), bg="#eef1f5",
                  fg=FG, relief="flat", cursor="hand2",
                  command=win.destroy).pack(side="right", padx=(8, 0))
        tk.Button(foot, text="保存并重连", font=("Microsoft YaHei", 10),
                  bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                  activebackground="#1a5fd0",
                  command=do_save).pack(side="right")

        body = tk.Frame(win, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=18, pady=(12, 0))
        body.columnconfigure(0, weight=1)
        for i, (key, label, hint) in enumerate(fields):
            r = i * 2
            tk.Label(body, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei", 9)).grid(row=r, column=0,
                                                       columnspan=2, sticky="w",
                                                       pady=(6, 0))
            v = tk.StringVar(value=str(getattr(self.cfg, key, "")))
            vars_[key] = v
            tk.Entry(body, textvariable=v, font=("Microsoft YaHei", 10),
                     relief="solid", bd=1).grid(row=r + 1, column=0,
                                                sticky="we", pady=(1, 0))
            tk.Label(body, text=hint, bg=BG, fg=MUTED,
                     font=("Microsoft YaHei", 8)).grid(row=r + 1, column=1,
                                                       sticky="w", padx=(8, 0))

    # ---------------------------------------------------------------- 操作
    def _quick_pair(self):
        """输入面板新配对码后立即重连（跳过退避等待）"""
        code = self.pair_var.get().strip()
        if not code.isdigit() or len(code) != 6:
            self.hint.config(text="配对码应为 6 位数字", fg=ERR)
            return
        try:
            self.svc.set_pair_code_and_reconnect(code)
        except Exception as e:
            self.hint.config(text="提交失败: %s" % e, fg=ERR)
            return
        self.hint.config(text="正在用新配对码连接…", fg=ACCENT)
        self.login_bar.config(text="登录：配对中…", fg=ACCENT)

    def _toggle_bemfa(self):
        on = self.bemfa_var.get()
        if on:
            self.bemfa.start()
        else:
            self.bemfa.stop()
        self._cloud_logs.append("%s  [云] 米家同步%s"
                                % (time.strftime("%H:%M:%S"),
                                   "开启" if on else "关闭"))

    def _cloud_log(self, msg):
        from boschlog import logger as _cl
        _cl.info("[CLOUD] %s" % msg)
        self._cloud_logs.append("%s  [云] %s" % (time.strftime("%H:%M:%S"), msg))

    def _reload_bemfa(self):
        """配置变了之后重建云连接"""
        try:
            self.bemfa.stop()
        except Exception:
            pass
        self.bemfa = BemfaClient(
            self.cfg.bemfa_uid,
            [self.cfg.topic_set, self.cfg.topic_up, self.cfg.topic_down])
        self.bemfa.on_command = self._on_cloud_cmd
        self.bemfa.on_log = self._cloud_log
        if self.bemfa_var.get():
            self.bemfa.start()

    def _on_cloud_cmd(self, topic, msg):
        """米家/巴法云下发（在后台线程执行，只能调用线程安全的接口）"""
        low = str(msg).strip().lower()
        st = self.svc.state
        if topic == self.cfg.topic_up and low in ("on", "1"):
            if st and st.setpoint is not None:
                self.svc.set_temperature(st.setpoint + 1)
            self.bemfa.push(topic, "off")        # 开关自动复位
        elif topic == self.cfg.topic_down and low in ("on", "1"):
            if st and st.setpoint is not None:
                self.svc.set_temperature(st.setpoint - 1)
            self.bemfa.push(topic, "off")
        elif topic == self.cfg.topic_set:
            raw = str(msg).strip()
            if "#" in raw:
                parts = [p for p in raw.split("#") if p.strip()]
                raw = parts[0] if parts else ""
            try:
                v = float(raw)
            except ValueError:
                return
            if TEMP_MIN <= v <= TEMP_MAX:
                self.svc.set_temperature(int(v))

    def _nudge(self, delta):
        sp = self.svc.state.setpoint if self.svc.state else None
        if sp is None:
            self._flash("还没收到设定值")
            return
        self._last_report = 0.0
        self.svc.set_temperature(sp + delta)

    def _on_slide(self, val):
        v = int(float(val))
        sp = self.svc.state.setpoint if self.svc.state else None
        if sp is not None and v == sp:
            if self.hint.cget("text").endswith("点应用"):
                self.hint.config(text="")
            return
        self.hint.config(text="预设 %d℃，点应用" % v, fg=MUTED)

    def _apply_entry(self):
        txt = self.entry.get().strip()
        if txt:
            try:
                v = int(txt)
            except ValueError:
                self._flash("请输入数字")
                return
        else:
            v = int(float(self.scale.get()))
        if not TEMP_MIN <= v <= TEMP_MAX:
            self._flash("范围 %d~%d" % (TEMP_MIN, TEMP_MAX))
            return
        self._last_report = 0.0
        self.svc.set_temperature(v)

    def _flash(self, msg):
        self.hint.config(text=msg, fg=WARN)
        self.root.after(2500, lambda: self.hint.config(text="", fg=MUTED))

    def _on_close(self):
        """关窗前让服务把蓝牙断开，否则连接残留在系统里，下次会连不上"""
        self.bemfa.stop()
        self.svc.stop(timeout=3)
        self.root.destroy()

    # ---------------------------------------------------------------- 曲线
    def _draw_curve(self):
        cv = getattr(self, "curve", None)
        if cv is None:
            return
        cv.delete("all")
        w = cv.winfo_width()
        h = int(cv["height"])
        if w < 60 or not self.hist:
            return

        pts = list(self.hist)
        lo, hi = min(pts), max(pts)
        if hi - lo < 8:                 # 抖动太小就撑开到 8℃ 跨度，避免一条直线假象
            mid = (hi + lo) / 2
            lo, hi = mid - 4, mid + 4
        pad = 4
        n = len(pts)

        def xy(i, v):
            x = pad + (w - 2 * pad) * (i / max(1, n - 1))
            y = h - pad - (h - 2 * pad) * ((v - lo) / (hi - lo))
            return x, y

        # 设定温度参考线（虚线）
        sp = self.svc.state.setpoint if self.svc.state else None
        if sp and lo <= sp <= hi:
            _, y = xy(0, sp)
            cv.create_line(pad, y, w - pad, y, fill="#d7dbe2", dash=(3, 3))

        if n >= 2:
            coords = []
            for i, v in enumerate(pts):
                coords.extend(xy(i, v))
            cv.create_line(*coords, fill=ACCENT, width=2, smooth=False)
            x, y = xy(n - 1, pts[-1])
            cv.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                           fill=ACCENT, outline="")
        cv.create_text(pad + 2, 8, anchor="w",
                       text="%.0f" % hi, fill=MUTED,
                       font=("Microsoft YaHei", 7))
        cv.create_text(pad + 2, h - 8, anchor="w",
                       text="%.0f" % lo, fill=MUTED,
                       font=("Microsoft YaHei", 7))

    # ---------------------------------------------------------------- 查询
    def _query_history(self):
        self.svc.query_history(2)
        self.hint.config(text="已请求历史数据…", fg=ACCENT)
        self._await_result("history", 10, self._show_history)

    def _query_errors(self):
        self.svc.query_errors(10)
        self.hint.config(text="已请求故障列表…", fg=ACCENT)
        self._await_result("errors", 10, self._show_errors)

    def _await_result(self, key, tries, cb):
        """轮询服务结果，拿到后回调（不阻塞界面）。"""
        def poll(n):
            if key in self.svc.results:
                cb(self.svc.results.pop(key))
                return
            if n <= 0:
                self.hint.config(text="查询超时（设备可能未应答）", fg=ERR)
                return
            self.root.after(1000, lambda: poll(n - 1))
        self.root.after(800, lambda: poll(tries))

    def _show_history(self, raw):
        from bosch_api import BoschProtocol
        info = BoschProtocol.parse_history(raw or b"")
        if info["a"] is None:
            messagebox.showinfo("历史数据", "应答过短：%s" % info.get("raw", ""))
            return
        hours = info["a"] / 60.0
        messagebox.showinfo(
            "历史数据（type %d）" % info["type"],
            "累计运行：%.1f 小时（%d 分钟）\n点火次数：%d 次\n\n原始应答：%s"
            % (hours, info["a"], info["b"], info["raw"]))
        self.hint.config(text="历史：%.1f 小时 / %d 次" % (hours, info["b"]),
                         fg=MUTED)

    def _show_errors(self, raw):
        from bosch_api import BoschProtocol
        codes = BoschProtocol.parse_errors(raw or b"")
        if not codes:
            messagebox.showinfo("故障记录", "没有故障记录（全部槽位为空）")
            self.hint.config(text="故障：无记录", fg=MUTED)
            return
        txt = "、".join("%02X" % c for c in codes)
        messagebox.showinfo("故障记录",
                            "共 %d 条：\n\n%s\n\n原始应答：%s"
                            % (len(codes), txt, (raw or b"").hex(" ")))
        self.hint.config(text="故障记录：%s" % txt, fg=WARN)

    # ---------------------------------------------------------------- 刷新
    def _tick(self):
        svc, st = self.svc, self.svc.state

        self.dot.config(fg=OK if svc.connected else WARN)
        self.conn_lbl.config(text=svc.status, fg=OK if svc.connected else WARN)

        ls = svc.login_state
        if ls.startswith("已授权"):
            self.login_bar.config(text="登录：%s" % ls, fg=OK)
        elif "错误" in ls or "拒绝" in ls or "无效" in ls:
            self.login_bar.config(text="登录：%s（请检查设置）" % ls, fg=ERR)
        else:
            self.login_bar.config(text="登录：%s" % ls, fg=MUTED)

        if st:
            self.temp_big.config(
                text="%.1f" % st.temp_out if st.temp_out is not None else "--")
            self.t_in.config(
                text="进水 %.1f" % st.temp_in if st.temp_in is not None
                else "进水 --")
            self.t_ch.config(
                text="炉腔 %.1f" % st.chamber if st.chamber is not None
                else "炉腔 --")
            self.t_box.config(
                text="机身 %.1f" % st.temp_box if st.temp_box is not None
                else "机身 --")
            self.flow.config(
                text="水流 %.1f" % st.water_flow if st.water_flow is not None
                else "水流 --")
            # 运行状态：data[32] 实测恒为 0，改用"流量 + 功率"判定
            flow = st.water_flow or 0
            if flow > 0.5:
                self.burn.config(text="燃烧中 %d%%" % st.burner_power, fg=ACCENT)
            elif st.burner_power > 10:
                self.burn.config(text="保温 %d%%" % st.burner_power, fg=WARN)
            else:
                self.burn.config(text="待机", fg=MUTED)
            self.mode_lbl.config(text="T%d" % st.frame_type)
            # 风机 / 电离 / 故障 / 最大设定
            if st.fan_speed is not None:
                self.fan.config(text="风机 %d rpm" % st.fan_speed)
            else:
                self.fan.config(text="风机 --")
            self.ion.config(text="电离 有" if st.ionized else "电离 无",
                            fg=ACCENT if st.ionized else MUTED)
            self.maxsp.config(
                text=("最大设定 %d℃" % st.max_setpoint)
                if st.max_setpoint else "")
            self.fault.config(
                text="故障 %s" % st.failure_text,
                fg=ERR if st.failure else MUTED)
            # 温度曲线：每个新帧一个点，最多 8 秒重绘一次
            if st.temp_out is not None and st.ts > self._last_curve_ts:
                self._last_curve_ts = st.ts
                self.hist.append(st.temp_out)
                if time.time() - getattr(self, "_last_draw", 0) > 3:
                    self._last_draw = time.time()
                    self._draw_curve()
            self.set_lbl.config(
                text="当前 %d℃" % st.setpoint if st.setpoint is not None else "--")
            if st.setpoint is not None and not self.hint.cget("text"):
                self.scale.set(st.setpoint)
        else:
            self.temp_big.config(text="--")

        # 米家同步：状态显示 + 节流上报设定温度
        if self.bemfa_var.get():
            self.bemfa_lbl.config(
                text="巴法云 %s" % self.bemfa.status,
                fg=OK if self.bemfa.status == "已连接" else MUTED)
            if (st and st.setpoint is not None
                    and time.time() - self._last_report > 8):
                self._last_report = time.time()
                self.bemfa.push(self.cfg.topic_set, "#%d" % st.setpoint)
        else:
            self.bemfa_lbl.config(text="巴法云 已关闭", fg=MUTED)

        logs = svc.take_logs()
        if self._cloud_logs:
            logs.extend(self._cloud_logs)
            self._cloud_logs = []
        if logs:
            self.logbox.config(state="normal")
            for line in logs:
                self.logbox.insert("end", line + "\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
            n = int(self.logbox.index("end-1c").split(".")[0])
            if n > 300:
                self.logbox.delete("1.0", "%d.0" % (n - 250))

        self.root.after(300, self._tick)


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 或更高版本")
        sys.exit(1)
    _root = tk.Tk()
    if not acquire_single_instance():
        _root.withdraw()
        messagebox.showwarning(
            APP_NAME,
            "程序已经在运行了。\n\n"
            "重复启动会让两个实例互抢热水器、互相踢下线，\n"
            "表现就是一直显示“连接中”且设备扫描不到。")
        _root.destroy()
        sys.exit(0)
    App(_root)
    _root.mainloop()
