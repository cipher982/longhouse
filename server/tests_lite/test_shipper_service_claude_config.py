"""The service must not export a CLAUDE_CONFIG_DIR that equals the default.

Exporting it is not a no-op. Claude Code keys its stored credential on whether
the config dir was configured, so setting the default path makes the CLI stop
finding a credential it holds, and every Console turn answers "Not logged in"
on a machine whose own terminal reports a healthy subscription.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.shipper.service import _service_claude_config_env  # noqa: E402


def test_the_default_claude_directory_is_never_exported():
    # The bug this closes: the engine's launchd plist carried
    # CLAUDE_CONFIG_DIR=~/.claude, which is exactly what Claude would have
    # resolved on its own, and that alone broke every Console turn.
    assert _service_claude_config_env(Path.home() / ".claude") == {}


def test_a_custom_claude_directory_is_still_exported():
    # A directory the user actually chose must be passed through: the engine
    # and the CLI have to agree on a location neither one can infer.
    custom = Path("/opt/custom-claude")
    assert _service_claude_config_env(custom) == {"CLAUDE_CONFIG_DIR": str(custom)}
