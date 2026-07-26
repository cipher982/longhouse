from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from zerg.services.cursor_hooks import install_cursor_hooks


def test_cursor_hook_install_delegates_to_paired_engine(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    engine = tmp_path / "bin" / "longhouse-engine"
    engine.parent.mkdir()
    engine.write_text("native engine")
    with patch("zerg.services.cursor_hooks.subprocess.run") as run:
        actions = install_cursor_hooks(cursor, engine_path=engine)

    run.assert_called_once_with(
        [str(engine), "cursor-helm", "configure-hooks", "--cursor-dir", str(cursor)],
        check=True,
    )
    assert actions == [f"Configured native Cursor hooks in {cursor}"]
