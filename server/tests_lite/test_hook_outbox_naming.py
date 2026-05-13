import subprocess

import pytest

from zerg.services.shipper.hooks import CODEX_HOOK_SCRIPT
from zerg.services.shipper.hooks import HOOK_SCRIPT


@pytest.mark.parametrize("script", [HOOK_SCRIPT, CODEX_HOOK_SCRIPT])
def test_hook_atomic_rename_produces_ready_prs_file(script: str, tmp_path):
    rename_line = next(
        (
            line.strip()
            for line in script.splitlines()
            if line.strip().startswith('mv "$TMPFILE"')
        ),
        None,
    )
    assert rename_line is not None

    probe = tmp_path / "probe.sh"
    probe.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                f'OUTBOX="{tmp_path}"',
                'TMPFILE=$(mktemp "$OUTBOX/.tmp.XXXXXX")',
                "printf '%s\\n' '{}' > \"$TMPFILE\"",
                rename_line,
                'basename "$OUTBOX"/*.json',
            ]
        )
        + "\n"
    )

    result = subprocess.run(
        ["bash", str(probe)],
        check=True,
        text=True,
        capture_output=True,
    )

    final_name = result.stdout.strip()
    assert final_name.startswith("prs.")
    assert final_name.endswith(".json")
    assert not final_name.startswith(".tmp.")
