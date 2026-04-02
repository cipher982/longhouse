from __future__ import annotations

import os
from importlib import metadata

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli.main import app


def test_longhouse_version_flag():
    runner = CliRunner()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"longhouse {metadata.version('longhouse')}"
