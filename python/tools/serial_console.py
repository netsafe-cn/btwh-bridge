"""Scripted serial console for the bridge board.

One open of the port, a sequence of commands, everything the board says is
printed. Reopening the port resets the board (CH343 DTR), so batching beats
one-process-per-command.

Usage:
  python serial_console.py                          # listen only, 5 s
  python serial_console.py --cmd status --cmd selftest --wait 12
  python serial_console.py --cmd "broker 192.168.1.5" --wait 8 --log session.txt

The port defaults to the first CH343 device found, because the number is not
stable across machines (COM3 at the office, COM8 here).
"""
import argparse
import sys
import time

import serial
from serial.tools import list_ports


def find_board_port() -> str:
    """CH343 is the UART the firmware logs on; the S3's native USB shows up
    as 'USB Serial Device' and is not the console."""
    for p in list_ports.comports():
        if "CH343" in p.description:
            return p.device
    for p in list_ports.comports():
        if p.vid == 0x303A:  # Espressif USB CDC - the other socket on the board
            return p.device
    raise SystemExit("no candidate serial port found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cmd", action="append", default=[],
                    help="command to send; repeatable, sent in order")
    ap.add_argument("--gap", type=float, default=3.0,
                    help="seconds to wait between commands")
    ap.add_argument("--wait", type=float, default=5.0,
                    help="seconds to listen after the last command")
    ap.add_argument("--log", default=None, help="also write output here")
    ap.add_argument("--reset", action="store_true",
                    help="pulse RTS like esptool's hard reset; the default-open line states "
                         "can park the board in download mode right after a flash")
    args = ap.parse_args()
    port = args.port or find_board_port()

    out = open(args.log, "w", encoding="utf-8", newline="\n") if args.log else None
    # Line buffered even when piped: capture-to-file must not look stuck.
    sys.stdout.reconfigure(line_buffering=True)

    deadline = time.monotonic() + (args.gap * max(len(args.cmd) - 1, 0)
                                   + args.wait + 2.0)

    # dtr/rts set before open: asserting both on open is the classic way to walk an
    # auto-reset board into the ROM bootloader, and the board then says nothing at all.
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = args.baud
    ser.timeout = 0.1
    ser.dtr = False
    ser.rts = False
    ser.open()

    with ser:
        print(f"--- {port} @ {args.baud} ---")
        if args.reset:
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
            time.sleep(0.3)   # banner lands right after
        sent = 0
        next_send = time.monotonic() + (1.5 if not args.reset else 0.8)
        while time.monotonic() < deadline:
            if sent < len(args.cmd) and time.monotonic() >= next_send:
                line = args.cmd[sent]
                print(f">>> {line}")
                ser.write((line + "\n").encode())
                sent += 1
                next_send = time.monotonic() + (args.gap if sent < len(args.cmd)
                                                else 0)
            data = ser.read(4096)
            if data:
                text = data.decode("utf-8", "replace")
                sys.stdout.write(text)
                if out:
                    out.write(text)
        if out:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
