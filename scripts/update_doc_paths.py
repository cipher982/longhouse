#!/usr/bin/env python3
"""Update file paths in archived docs after directory restructuring."""
import os
import glob
import re

replacements = [
    (r"apps/oikos/apps/web/lib/", "src/oikos/lib/"),
    (r"apps/oikos/apps/web/", "src/oikos/"),
    (r"apps/oikos/packages/core/", "src/oikos/core/"),
    (r"apps/oikos/packages/data/local/", "src/oikos/data/"),
    (r"apps/oikos/", "apps/zerg/frontend-web/src/oikos/"),
    (r"zerg-backend", "backend"),
    (r"zerg-frontend", "frontend-web"),
]

def update_paths(directory):
    for filepath in glob.glob(os.path.join(directory, "*.md")):
        with open(filepath, "r") as f:
            content = f.read()

        new_content = content
        for old, new in replacements:
            new_content = re.sub(old, new, new_content)

        if new_content != content:
            print(f"Updated paths in {filepath}")
            with open(filepath, "w") as f:
                f.write(new_content)

update_paths("docs/completed")
update_paths("docs/archive")
