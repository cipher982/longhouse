#!/usr/bin/env python3
"""Migrate Cursor rules (~/.cursor/rules/) to Longhouse skill format (~/.longhouse/skills/)."""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_SRC = Path.home() / ".cursor" / "rules"
DEFAULT_DST = Path.home() / ".longhouse" / "skills"
RULE_EXTS = {".md", ".mdc", ".txt", ""}


def migrate_rule(rule_file: Path, dst_root: Path, dry_run: bool) -> bool:
    """Migrate a single Cursor rule file to a Longhouse SKILL.md."""
    if rule_file.suffix not in RULE_EXTS or not rule_file.is_file():
        return False

    content = rule_file.read_text(encoding="utf-8").strip()
    if not content:
        return False

    skill_name = re.sub(r"[^a-zA-Z0-9_-]", "-", rule_file.stem).strip("-")
    if not skill_name:
        return False

    first_line = content.split("\n")[0].lstrip("# ").strip()
    desc = first_line[:80] if first_line else ""

    safe_desc = desc.replace('"', '\\"')
    skill_content = f'---\nname: {skill_name}\ndescription: "{safe_desc}"\n---\n\n{content}\n'

    dst_dir = dst_root / skill_name
    dst_file = dst_dir / "SKILL.md"

    if dry_run:
        print(f"  [dry-run] {rule_file} -> {dst_file}")
        return True

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file.write_text(skill_content, encoding="utf-8")
    print(f"  {rule_file} -> {dst_file}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate Cursor rules to Longhouse skill format.")
    parser.add_argument("-s", "--source", type=Path, default=DEFAULT_SRC, help=f"Source dir (default: {DEFAULT_SRC})")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_DST, help=f"Output dir (default: {DEFAULT_DST})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files")
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"Source directory not found: {args.source}")
        sys.exit(1)

    rule_files = [f for f in args.source.iterdir() if f.is_file() and f.suffix in RULE_EXTS]
    if not rule_files:
        print(f"No rule files found in {args.source}")
        sys.exit(0)

    print(f"Migrating Cursor rules -> Longhouse skill format")
    print(f"  Source: {args.source}")
    print(f"  Output: {args.output}")
    if args.dry_run:
        print(f"  Mode:   DRY RUN\n")
    else:
        print()

    migrated = 0
    for f in sorted(rule_files):
        if migrate_rule(f, args.output, args.dry_run):
            migrated += 1

    print(f"\n{migrated} rule(s) {'would be ' if args.dry_run else ''}migrated.")


if __name__ == "__main__":
    main()
