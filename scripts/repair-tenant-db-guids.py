#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps" / "zerg" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from zerg.services.tenant_db_guid_repair import run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli())
