#!/usr/bin/env python3
"""Migrate Claude Code skills (~/.claude/skills/) to Longhouse format (~/.longhouse/skills/)."""

import argparse
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
DEFAULT_SRC = Path.home() / ".claude" / "skills"
DEFAULT_DST = Path.home() / ".longhouse" / "skills"


def migrate_skill(src_dir: Path, dst_root: Path, dry_run: bool) -> bool:
    """Migrate a single Claude Code skill directory to Longhouse format."""
    md_files = list(src_dir.glob("*.md"))
    if not md_files:
        return False

    md_file = md_files[0]
    content = md_file.read_text(encoding="utf-8")
    skill_name = re.sub(r"[^a-zA-Z0-9_-]", "-", src_dir.name).strip("-")

    # Check if already has frontmatter
    if FRONTMATTER_RE.match(content):
        # Ensure name field exists in frontmatter
        if f"name:" not in content.split("---")[1]:
            content = content.replace("---\n", f"---\nname: {skill_name}\n", 1)
    else:
        # Wrap with minimal frontmatter
        first_line = content.strip().split("\n")[0].lstrip("# ").strip()
        desc = first_line[:80].replace('"', '\\"') if first_line else ""
        header = f"---\nname: {skill_name}\ndescription: \"{desc}\"\n---\n\n"
        content = header + content

    dst_dir = dst_root / skill_name
    dst_file = dst_dir / "SKILL.md"

    if dry_run:
        print(f"  [dry-run] {md_file} -> {dst_file}")
        return True

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file.write_text(content, encoding="utf-8")
    print(f"  {md_file} -> {dst_file}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate Claude Code skills to Longhouse format.")
    parser.add_argument("-s", "--source", type=Path, default=DEFAULT_SRC, help=f"Source dir (default: {DEFAULT_SRC})")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_DST, help=f"Output dir (default: {DEFAULT_DST})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files")
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"Source directory not found: {args.source}")
        sys.exit(1)

    skill_dirs = [d for d in args.source.iterdir() if d.is_dir()]
    if not skill_dirs:
        print(f"No skill directories found in {args.source}")
        sys.exit(0)

    print(f"Migrating Claude Code skills -> Longhouse format")
    print(f"  Source: {args.source}")
    print(f"  Output: {args.output}")
    if args.dry_run:
        print(f"  Mode:   DRY RUN\n")
    else:
        print()

    migrated = 0
    for d in sorted(skill_dirs):
        if migrate_skill(d, args.output, args.dry_run):
            migrated += 1

    print(f"\n{migrated} skill(s) {'would be ' if args.dry_run else ''}migrated.")


if __name__ == "__main__":
    main()
