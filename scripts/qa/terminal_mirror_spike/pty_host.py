#!/usr/bin/env python3
"""Stands in for the owner's terminal: hosts a command in a PTY of a given size,
records every byte it receives with a timestamp, and takes control lines on
stdin (`resize COLS ROWS`, `stop`). The record is JSON lines:
{"t": epoch_us, "d": base64} for output, {"t":..., "resize": [cols, rows]},
{"t":..., "exit": code}.
"""

import argparse
import base64
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

parser = argparse.ArgumentParser()
parser.add_argument("--cols", type=int, required=True)
parser.add_argument("--rows", type=int, required=True)
parser.add_argument("--record", required=True)
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command[1:] if args.command and args.command[0] == "--" else args.command


def now_us() -> int:
    return time.time_ns() // 1000


def set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


pid, master = pty.fork()
if pid == 0:
    os.execvp(command[0], command)
set_size(master, args.cols, args.rows)
os.kill(pid, signal.SIGWINCH)
record = open(args.record, "a", buffering=1)
record.write(json.dumps({"t": now_us(), "resize": [args.cols, args.rows]}) + "\n")
control = b""
code = 0
while True:
    readable, _, _ = select.select([master, 0], [], [], 0.2)
    if master in readable:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        if not data:
            break
        record.write(json.dumps({"t": now_us(), "d": base64.b64encode(data).decode()}) + "\n")
    if 0 in readable:
        chunk = os.read(0, 4096)
        if not chunk:
            os.kill(pid, signal.SIGTERM)
        control += chunk
        while b"\n" in control:
            line, control = control.split(b"\n", 1)
            words = line.decode().split()
            if words[:1] == ["resize"]:
                cols, rows = int(words[1]), int(words[2])
                record.write(json.dumps({"t": now_us(), "resize": [cols, rows]}) + "\n")
                set_size(master, cols, rows)  # the kernel sends SIGWINCH
            elif words[:1] == ["stop"]:
                os.kill(pid, signal.SIGTERM)
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
record.write(json.dumps({"t": now_us(), "exit": code}) + "\n")
