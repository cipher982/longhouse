#!/usr/bin/env python3
"""Thin public wrapper for the packaged Codex qualification bridge."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from zerg.qa.provider_qualification import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
