#!/usr/bin/env python3
"""Repo wrapper for the packaged provider live-proof publisher."""

from __future__ import annotations

import sys
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_PATH))

from zerg.qa.provider_live_proof_publish import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
