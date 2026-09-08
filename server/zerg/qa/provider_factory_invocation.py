"""Shared command-line contract for provider-factory executables.

The private factory and the public producer modules meet at a subprocess
boundary. Keeping the common envelope here prevents either side from silently
inventing a provider-specific dialect that only fails in the live image.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path


def add_factory_provider_arguments(
    parser: argparse.ArgumentParser,
    *,
    variants: Iterable[str],
    provider_bin_aliases: tuple[str, ...] = (),
) -> None:
    """Add the exact common envelope accepted from the provider factory."""

    parser.add_argument("--variant", required=True, choices=tuple(variants))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--longhouse-cli", type=Path)
    parser.add_argument(
        "--provider-bin",
        *provider_bin_aliases,
        dest="provider_bin",
        type=Path,
    )
    parser.add_argument("--provider-version")
    # The factory includes the authored cell axes in every executable command.
    # Producers may not need to use them, but must parse the same envelope so a
    # staged cell cannot silently run a different subject or evidence class.
    parser.add_argument("--provider")
    parser.add_argument("--platform")
    parser.add_argument("--architecture")
    parser.add_argument("--evidence-class")
    parser.add_argument("--credential-binding", action="append", default=[])
    parser.add_argument("--registration", action="store_true")
