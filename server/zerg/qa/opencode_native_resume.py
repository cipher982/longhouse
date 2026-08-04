#!/usr/bin/env python3
"""Direct stock-OpenCode Helm Resume producer."""

from zerg.qa.provider_native_resume import main_for
from zerg.qa.provider_native_resume import registration_for

REGISTRATION = registration_for("opencode")

if __name__ == "__main__":
    raise SystemExit(main_for("opencode"))
