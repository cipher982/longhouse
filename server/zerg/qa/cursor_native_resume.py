#!/usr/bin/env python3
"""Direct stock-Cursor Helm Resume producer."""

from zerg.qa.provider_native_resume import main_for
from zerg.qa.provider_native_resume import producer_for

PRODUCER = producer_for("cursor")
REGISTRATION = PRODUCER.registration


def main(argv: list[str] | None = None) -> int:
    return main_for("cursor", argv)


if __name__ == "__main__":
    raise SystemExit(main())
