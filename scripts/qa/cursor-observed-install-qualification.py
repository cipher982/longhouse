#!/usr/bin/env python3
"""Repo wrapper for exact Cursor observed-install qualification."""

from __future__ import annotations

import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2] / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from zerg.qa.provider_observed_install_qualification import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
