#!/usr/bin/env python3
"""Repo wrapper for the real Longhouse↔Cursor Helm product canary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from zerg.qa.cursor_helm_product_e2e import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
