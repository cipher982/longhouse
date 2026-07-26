#!/usr/bin/env python3
"""Run a command under a real PTY and preserve its exit status."""

from __future__ import annotations

import os
import pty
import sys


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run-in-pty.py COMMAND [ARG ...]")
    status = pty.spawn(sys.argv[1:])
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    raise SystemExit(main())
