"""Delegate Cursor hook installation to the paired native engine."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def install_cursor_hooks(
    cursor_dir: Path | None = None,
    *,
    engine_path: Path | None = None,
) -> list[str]:
    cursor_dir = cursor_dir or (Path.home() / ".cursor")
    if not cursor_dir.exists():
        return []
    engine = str(engine_path or os.environ.get("LONGHOUSE_ENGINE_BIN", "longhouse-engine"))
    subprocess.run(
        [engine, "cursor-helm", "configure-hooks", "--cursor-dir", str(cursor_dir)],
        check=True,
    )
    return [f"Configured native Cursor hooks in {cursor_dir}"]
