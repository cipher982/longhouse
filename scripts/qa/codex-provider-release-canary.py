#!/usr/bin/env python3
"""Repo wrapper for the packaged Codex provider release canary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from zerg.qa.codex_provider_release_canary import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
