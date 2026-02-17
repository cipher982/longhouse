#!/usr/bin/env python3
"""Targeted CSS recolor tool for the Warm Gold redesign.

Parses CSS files line-by-line, skips url(...) regions and data URIs,
and replaces hardcoded color values using an exact-match mapping table.

Usage:
    python scripts/recolor.py --dry-run    # Preview changes
    python scripts/recolor.py              # Apply changes
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "apps/zerg/frontend-web/src"

# Directories to process
CSS_DIRS = [
    ROOT / "styles",
    ROOT / "oikos/styles",
    ROOT / "oikos/app/components",
    ROOT / "components",
    ROOT / "pages",
]

# Skip tokens.css — handled separately in Step 1
SKIP_FILES = {"tokens.css"}

# ── Color mapping: old → new ──────────────────────────────────────────────────
# Case-insensitive hex matching. rgb/rgba matched with normalized whitespace.
HEX_MAP = {
    # Backgrounds
    "#030305": "#120B09",
    "#09090b": "#120B09",
    "#0a0a12": "#150E0B",
    "#0a0a0f": "#150E0B",
    "#18181b": "#1A1410",
    "#1a1a2e": "#1A1410",
    "#111113": "#1A1410",
    "#111118": "#1A1410",
    "#0f0f12": "#150E0B",
    "#0d0d11": "#150E0B",
    "#0c0c10": "#150E0B",
    "#141418": "#1A1410",
    "#161616": "#1A1410",
    "#1a1a1a": "#1A1410",
    "#1c1c1c": "#1A1410",
    "#1e1e1e": "#211C15",
    # Borders
    "#27272a": "#2a2418",
    "#3f3f46": "#3d3428",
    "#2d2d35": "#2a2418",
    "#333338": "#2a2418",
    "#2a2a2e": "#2a2418",
    "#1f1f25": "#2a2418",
    "#2c2c32": "#2a2418",
    # Brand / accent — indigo → gold
    "#6366f1": "#C9A66B",
    "#4f46e5": "#B8923C",
    "#818cf8": "#9e7c5a",
    "#a855f7": "#C9A66B",
    "#8b5cf6": "#C9A66B",
    "#7c3aed": "#B8923C",
    # Neon/cyber → warm
    "#06b6d4": "#C9A66B",
    "#22d3ee": "#D4B87A",
    "#38bdf8": "#D4B87A",
    "#ec4899": "#C45040",
    "#d946ef": "#C9A66B",
    # Text
    "#fafafa": "#F3EAD9",
    "#f4f4f5": "#F3EAD9",
    "#e4e4e7": "#F3EAD9",
    "#d4d4d8": "#D5C8B4",
    "#a1a1aa": "#B5A48E",
    "#b4b4bc": "#B5A48E",
    "#9898a3": "#8A7A64",
    "#71717a": "#8A7A64",
    "#52525b": "#5C4F3D",
    # Intent
    "#22c55e": "#5D9B4A",
    "#16a34a": "#4A8A3A",
    "#f59e0b": "#D4A843",
    "#ef4444": "#C45040",
    "#dc2626": "#B04030",
    "#f87171": "#D4685A",
    "#fbbf24": "#D4B87A",
    "#4ade80": "#6DAB5A",
    "#86efac": "#7DBB6A",
    "#fca5a5": "#D4887A",
    "#fde68a": "#D4C88A",
    # Misc surfaces
    "#ffffff": "#F3EAD9",
    "#000000": "#120B09",
}

# rgba/rgb patterns: (old_r, old_g, old_b, old_a) → replacement string
# We handle these with regex-based matching
RGBA_MAP = {
    # Glass surfaces → solid warm
    (255, 255, 255, 0.03): "#1A1410",
    (255, 255, 255, 0.04): "#1C1610",
    (255, 255, 255, 0.05): "#1E1812",
    (255, 255, 255, 0.07): "#211C15",
    (255, 255, 255, 0.08): "#231E16",
    (255, 255, 255, 0.1): "#261F18",
    (255, 255, 255, 0.12): "#2a2318",
    (255, 255, 255, 0.15): "#2e271c",
    (255, 255, 255, 0.2): "#342d20",
    (255, 255, 255, 0.06): "#1F1912",
    # Dark overlays → warm dark
    (0, 0, 0, 0.3): "rgba(18, 11, 9, 0.3)",
    (0, 0, 0, 0.4): "rgba(18, 11, 9, 0.4)",
    (0, 0, 0, 0.5): "rgba(18, 11, 9, 0.5)",
    (0, 0, 0, 0.6): "rgba(18, 11, 9, 0.6)",
    (0, 0, 0, 0.7): "rgba(18, 11, 9, 0.7)",
    (0, 0, 0, 0.8): "rgba(18, 11, 9, 0.8)",
    # Indigo glows → gold glows
    (99, 102, 241, None): "rgb(201, 166, 107)",  # any alpha
    (129, 140, 248, None): "rgb(158, 124, 90)",
    (168, 85, 247, None): "rgb(201, 166, 107)",
    (6, 182, 212, None): "rgb(201, 166, 107)",
    (236, 72, 153, None): "rgb(196, 80, 64)",
    # Success/warning/error muted
    (34, 197, 94, 0.15): "rgba(93, 155, 74, 0.15)",
    (34, 197, 94, 0.1): "rgba(93, 155, 74, 0.1)",
    (34, 197, 94, 0.2): "rgba(93, 155, 74, 0.2)",
    (245, 158, 11, 0.15): "rgba(212, 168, 67, 0.15)",
    (245, 158, 11, 0.1): "rgba(212, 168, 67, 0.1)",
    (239, 68, 68, 0.15): "rgba(196, 80, 64, 0.15)",
    (239, 68, 68, 0.1): "rgba(196, 80, 64, 0.1)",
    (239, 68, 68, 0.3): "rgba(196, 80, 64, 0.3)",
    # Green variants
    (22, 163, 74, 0.15): "rgba(74, 138, 58, 0.15)",
    (22, 163, 74, 0.1): "rgba(74, 138, 58, 0.1)",
    # General dark bg
    (10, 10, 15, 0.45): "rgba(18, 11, 9, 0.45)",
    (10, 10, 15, 0.6): "rgba(18, 11, 9, 0.6)",
    (20, 20, 30, 0.6): "rgba(26, 20, 16, 0.6)",
    (20, 20, 30, 0.8): "rgba(26, 20, 16, 0.8)",
    # Purple/indigo variants
    (99, 102, 241, 0.1): "rgba(201, 166, 107, 0.1)",
    (99, 102, 241, 0.15): "rgba(201, 166, 107, 0.15)",
    (99, 102, 241, 0.2): "rgba(201, 166, 107, 0.2)",
    (99, 102, 241, 0.3): "rgba(201, 166, 107, 0.3)",
    (99, 102, 241, 0.5): "rgba(201, 166, 107, 0.5)",
    (139, 92, 246, 0.15): "rgba(201, 166, 107, 0.15)",
    (139, 92, 246, 0.2): "rgba(201, 166, 107, 0.2)",
}


def _is_inside_url(line: str, match_start: int) -> bool:
    """Check if a match position is inside a url(...) or data URI."""
    # Look backward for url(
    before = line[:match_start].lower()
    # Find last url( before this position
    url_pos = before.rfind("url(")
    if url_pos == -1:
        return False
    # Check if there's a closing ) between url( and our match
    between = line[url_pos:match_start]
    return ")" not in between


def _normalize_hex(h: str) -> str:
    """Normalize hex to lowercase 6-char."""
    h = h.lower().lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    return f"#{h[:6]}"


def _replace_hex(line: str) -> tuple[str, int]:
    """Replace hex colors in a line. Returns (new_line, count)."""
    count = 0
    hex_re = re.compile(r"#([0-9a-fA-F]{3,8})\b")

    def _sub(m):
        nonlocal count
        if _is_inside_url(line, m.start()):
            return m.group(0)
        normalized = _normalize_hex(m.group(0))
        if normalized in HEX_MAP:
            count += 1
            return HEX_MAP[normalized]
        return m.group(0)

    # We need to handle the line carefully to not break url() contexts
    new_line = hex_re.sub(_sub, line)
    return new_line, count


def _parse_rgba_values(s: str) -> tuple | None:
    """Parse rgb/rgba function call to (r, g, b, a) tuple. a=None for rgb."""
    s = s.strip()
    # Match both rgba(r, g, b, a) and rgb(r g b / a) modern syntax
    # Also rgb(r, g, b) and rgba(r, g, b)
    m = re.match(
        r"rgba?\s*\(\s*"
        r"(\d+(?:\.\d+)?)\s*[,/ ]\s*"
        r"(\d+(?:\.\d+)?)\s*[,/ ]\s*"
        r"(\d+(?:\.\d+)?)\s*"
        r"(?:[,/]\s*(\d*\.?\d+%?))?\s*\)",
        s,
    )
    if not m:
        return None
    r, g, b = int(float(m.group(1))), int(float(m.group(2))), int(float(m.group(3)))
    a_str = m.group(4)
    if a_str is None:
        return (r, g, b, None)
    if a_str.endswith("%"):
        a = float(a_str[:-1]) / 100
    else:
        a = float(a_str)
    return (r, g, b, round(a, 4))


def _replace_rgba(line: str) -> tuple[str, int]:
    """Replace rgb/rgba colors in a line."""
    count = 0
    rgba_re = re.compile(r"rgba?\s*\([^)]+\)")

    def _sub(m):
        nonlocal count
        if _is_inside_url(line, m.start()):
            return m.group(0)
        vals = _parse_rgba_values(m.group(0))
        if vals is None:
            return m.group(0)
        r, g, b, a = vals

        # Try exact match first
        if (r, g, b, a) in RGBA_MAP:
            count += 1
            return RGBA_MAP[(r, g, b, a)]

        # Try wildcard alpha match (None = any alpha)
        if (r, g, b, None) in RGBA_MAP:
            base = RGBA_MAP[(r, g, b, None)]
            if a is not None and a != 1.0:
                # Reconstruct with original alpha
                # Extract rgb values from the base
                base_m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", base)
                if base_m:
                    count += 1
                    nr, ng, nb = base_m.group(1), base_m.group(2), base_m.group(3)
                    a_str = f"{a:.0%}".replace("%", "") if a * 100 == int(a * 100) else f"{a}"
                    return f"rgba({nr}, {ng}, {nb}, {a_str})"
            count += 1
            return base

        return m.group(0)

    new_line = rgba_re.sub(_sub, line)
    return new_line, count


def process_file(path: Path, dry_run: bool) -> int:
    """Process a single CSS file. Returns number of replacements."""
    if path.name in SKIP_FILES:
        return 0

    text = path.read_text()
    lines = text.split("\n")
    new_lines = []
    total = 0

    for line in lines:
        new_line, c1 = _replace_hex(line)
        new_line, c2 = _replace_rgba(new_line)
        new_lines.append(new_line)
        total += c1 + c2

    if total > 0:
        if dry_run:
            print(f"  {path.relative_to(ROOT)}: {total} replacements (dry-run)")
        else:
            path.write_text("\n".join(new_lines))
            print(f"  {path.relative_to(ROOT)}: {total} replacements")

    return total


def main():
    parser = argparse.ArgumentParser(description="Warm Gold CSS recolor tool")
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying files")
    args = parser.parse_args()

    print(f"{'DRY RUN — ' if args.dry_run else ''}Warm Gold Recolor")
    print("=" * 60)

    grand_total = 0
    file_count = 0

    for d in CSS_DIRS:
        if not d.exists():
            continue
        for css_file in sorted(d.rglob("*.css")):
            count = process_file(css_file, args.dry_run)
            if count > 0:
                file_count += 1
                grand_total += count

    print("=" * 60)
    print(f"Total: {grand_total} replacements across {file_count} files")

    if args.dry_run:
        print("\nRun without --dry-run to apply changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
