#!/usr/bin/env python3
"""Fast import smoke test — catches dead module references in <5 seconds.

Imports the FastAPI app (which mounts all routers), the MCP server factory,
and the CLI entrypoints. Any ImportError from a deleted module, missing
dependency, or broken circular import will fail this check immediately.

Also scans CSS files for @import paths that reference non-existent files.

Usage:
    python scripts/ci/import-smoke.py          # run all checks
    python scripts/ci/import-smoke.py --quick   # Python imports only
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FAIL = False


def check(label: str, fn):
    global FAIL
    try:
        fn()
        print(f"  OK  {label}")
    except Exception as exc:
        print(f"  FAIL {label}: {exc}")
        FAIL = True


def import_check():
    """Import all critical Python modules."""
    # Minimal env so imports don't crash on missing config
    os.environ.setdefault("DATABASE_URL", "sqlite:///")
    os.environ.setdefault("AUTH_DISABLED", "1")
    os.environ.setdefault("SINGLE_TENANT", "1")
    os.environ.setdefault("FERNET_SECRET", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    os.environ.setdefault("TESTING", "1")

    modules = [
        ("FastAPI app + all routers", "zerg.main"),
        ("MCP server factory", "zerg.mcp_server"),
        ("MCP API client", "zerg.mcp_server.api_client"),
        ("CLI serve", "zerg.cli.serve"),
        ("CLI connect", "zerg.cli.connect"),
        ("CLI onboard", "zerg.cli.onboard"),
        ("CLI doctor", "zerg.cli.doctor"),
        ("CLI mcp-server", "zerg.cli.mcp_serve"),
        ("CLI claude-channel", "zerg.cli.claude_channel"),
        ("Demo sessions", "zerg.services.demo_sessions"),
        ("Local health", "zerg.services.local_health"),
    ]

    for label, module_name in modules:
        check(label, lambda m=module_name: importlib.import_module(m))


def css_import_check():
    """Check CSS @import paths resolve to existing files."""
    web_src = REPO_ROOT / "web" / "src"
    if not web_src.exists():
        print("  SKIP CSS check (web/src not found)")
        return

    import_re = re.compile(r'@import\s+["\']([^"\']+)["\']')
    global FAIL

    for css_file in web_src.rglob("*.css"):
        content = css_file.read_text()
        for match in import_re.finditer(content):
            import_path = match.group(1)
            # Resolve relative to the CSS file's directory
            resolved = (css_file.parent / import_path).resolve()
            if not resolved.exists():
                print(f"  FAIL CSS dead import: {css_file.relative_to(REPO_ROOT)} -> {import_path}")
                FAIL = True


def main():
    parser = argparse.ArgumentParser(description="Import smoke test")
    parser.add_argument("--quick", action="store_true", help="Python imports only")
    args = parser.parse_args()

    # Run from server/ so zerg package is importable.
    # When invoked via `uv run` from server/, the venv is active.
    # When invoked from repo root, add server/ to sys.path.
    os.chdir(REPO_ROOT / "server")
    if str(REPO_ROOT / "server") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "server"))

    print("Import smoke test")
    print("=" * 40)

    print("\nPython imports:")
    import_check()

    if not args.quick:
        print("\nCSS imports:")
        css_import_check()

    print()
    if FAIL:
        print("FAILED — dead references found")
        sys.exit(1)
    else:
        print("All clear")


if __name__ == "__main__":
    main()
