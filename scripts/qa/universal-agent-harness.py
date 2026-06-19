#!/usr/bin/env python3
"""Repo wrapper for the universal agent harness MVP."""

# ruff: noqa: E402,I001

from __future__ import annotations

import sys
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_PATH))

from zerg.qa.universal_agent_harness import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
