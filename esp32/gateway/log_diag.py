# -*- coding: utf-8 -*-
"""log_diag.py —— ESP32 网关串口日志诊断器

把 esp32/README.md 的「失败对照表」变成可执行规则。实测连不上时跑一下，
直接给出卡在哪一层、最可能的原因、下一步动作。

用法：
    python log_diag.py                     # 自动找 ../logs/ 下最新的 panel_*.log
    python log_diag.py <日志文件路径>       # 指定文件

判据与 README 第四节一致：
    [SMP] 认证完成：链路已加密
    [AUTH] A2/07 应答 = 01/02/00 且 A2/08 应答 = 01
    [A0] 有帧且字段合理
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
LOGDIR = HERE.parent / "logs"


def find_latest() -> pathlib.Path | None:
    files = sorted(LOGDIR.glob("panel_*.log"))
    files += sorted(LOGDIR.glob("esp32_*.log"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def diagnose(text: str) -> list[tuple[str, str, str]]:
    """返回 [(阶段, 结论, 建议)]，按链路顺序逐层判定。"""
    out = []

    # ---- 第 1 层：BLE 链路 + SMP
    smp_ok = bool(re.search(r"认证完成[：:].*链路已加密", text))
    pin_asked = "设备请求 PIN" in text or "PROVIDE_PIN" in text
    smp_fail = bool(re.search(r"认证完成[：:].*未加密|认证失败|SMP.*失败", text))
    connected = bool(re.search(r"\[CONN\]|已连接|Connected", text, re.I))

    if smp_ok:
        out.append(("SMP", "✓ 链路已加密/Bond 建立", "无需处理"))
    elif pin_asked or smp_fail:
        out.append(("SMP", "✗ PIN 阶段失败",
                    "码错 / 配对窗口已关 / 手机 App 占着蓝牙；"
                    "面板重新『清除配对』取新码，再输一次"))
    else:
        out.append(("SMP", "✗ 未到 PIN 阶段%s" %
                    ("（链路都没建起来）" if not connected else ""),
                    "检查面板是否休眠、手机蓝牙是否关闭、距离；用 scan 看设备在不在"))

    # ---- 第 2 层：应用层登录
    a2_07 = re.findall(r"A2/07\s*应答\s*=\s*([0-9A-Fa-f]{2})", text)
    a2_08 = re.findall(r"A2/08\s*应答\s*=\s*([0-9A-Fa-f]{2})", text)
    if a2_07:
        last07 = a2_07[-1].upper()
        if last07 == "00":
            out.append(("AUTH/A2-07", "✗ 应答 00：配对窗口未打开或已超时",
                        "这就是「配对超时」最常见的样子：面板『清除配对』→ 立刻"
                        "『连接/配对』取码 → 30 秒内输入，中途别退出配对界面。"
                        "不要改 login_mac，那没用"))
        elif last07 in ("3C", "5A"):
            out.append(("AUTH/A2-07", "✗ 应答 %s：设备连接频率保护" % last07,
                        "等 30~90 秒再试，不要连续重连"))
        elif last07 == "02":
            out.append(("AUTH/A2-07", "✓ 应答 02：窗口开着，要求提交码", "无需处理"))
        elif last07 == "01":
            # 实测 2026-11-07：01 ≠ 已授权。它只表示设备已登记本机 MAC，
            # 仍需 A2/08 校验通过才算登录成功（实测出现过 01 → A2/08=00 失败）。
            out.append(("AUTH/A2-07", "· 应答 01：MAC 已登记（仍需配对码验证）",
                        "这不是终态，以下面的 A2/08 为准"))
        else:
            out.append(("AUTH/A2-07", "? 应答 %s：未定义" % last07,
                        "记录该值并反馈，可能是新固件行为"))
    else:
        out.append(("AUTH/A2-07", "— 未收到应答",
                    "若 SMP 已过，多为服务/特征枚举失败；检查订阅 FIFO/CREDITS"))

    if a2_08:
        last08 = a2_08[-1].upper()
        if last08 == "01":
            out.append(("AUTH/A2-08", "✓ 应答 01：应用层登录通过", "无需处理"))
        else:
            hint = "码过期或输错；重取码再输，不用重新烧录"
            if a2_07 and a2_07[-1].upper() == "00":
                hint = ("根因不在码：A2/07 已是 00，说明配对窗口根本没开。"
                        "先回面板『清除配对』重新取码，别在码上找原因")
            out.append(("AUTH/A2-08", "✗ 应答 %s：码被拒" % last08, hint))
    else:
        out.append(("AUTH/A2-08", "— 未收到应答",
                    "确认 6 位码已正确送入串口（控制面板输入框）"))

    # ---- 第 3 层：状态广播
    a0_hits = len(re.findall(r"\[A0\]", text))
    if a0_hits:
        out.append(("A0", "✓ 收到 %d 帧状态广播" % a0_hits,
                    "核对水流 0~11 L/min、功率 0~50%、风机 700~1500 rpm；"
                    "数值离谱用 raw on 看 A0 是否 34 字节"))
    elif a2_08 and a2_08[-1].upper() == "01":
        out.append(("A0", "✗ 登录通过但无广播",
                    "确认 FIFO/CREDITS 两个 CCCD 都已订阅；raw on 看是否有残帧"))
    else:
        out.append(("A0", "— 尚未到这一步", "先解决上面的问题"))

    # ---- 连接层：连续失败多半是频率保护
    # 2026-11-07 实测：19:17:53 之后连续 3 次连不上，直到 19:21:55（约 3 分钟）才恢复。
    link_fail = len(re.findall(r"✗ 连不上", text))
    if link_fail >= 2:
        out.append(("LINK", "! 连续 %d 次连不上：多半是设备连接频率保护" % link_fail,
                    "停止重试，等 2~3 分钟再试；同时确认没退出配对界面、手机 App 没占着"))

    # ---- 附加信号
    if re.search(r"找不到服务|GATT 未就绪", text):
        out.append(("GATT", "✗ 服务枚举失败",
                    "通常是配对失效：pair 命令清本机绑定后重新 SMP 配对"))
    if re.search(r"无应答|超时|Timeout", text, re.I):
        out.append(("RX", "! 出现超时/无应答",
                    "看是不是手机 App 还占着设备；或面板已退出配对菜单"))
    return out


def main() -> int:
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
    else:
        path = find_latest()
    if not path or not path.exists():
        print("找不到日志。先双击『启动控制面板.bat』跑一次，日志会写到 logs/panel_*.log")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    print("日志：%s（%d 行）" % (path, len(text.splitlines())))
    print("=" * 68)
    stages = diagnose(text)
    for stage, verdict, advice in stages:
        print("[%-12s] %s" % (stage, verdict))
        if advice != "无需处理":
            print("               → %s" % advice)
    print("=" * 68)
    # 判据必须落在 A2/08=01（应用层授权通过）上，绝不能看 A2/07——
    # 实测 A2/07=01 只表示 MAC 已登记，随后 A2/08 仍可能被拒。
    ok = (bool(re.search(r"A2/08\s*应答\s*=\s*01", text))
          and "[A0]" in text)
    print("总体：%s" % ("✓ 三段判据全部满足，连接实测通过" if ok
                       else "✗ 尚未跑通，按上面的第一条 ✗ 处理"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
