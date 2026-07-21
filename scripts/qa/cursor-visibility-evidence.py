#!/usr/bin/env python3
"""Replay Cursor visibility evidence into an observation report."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from zerg.qa.cursor_visibility_evidence import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
