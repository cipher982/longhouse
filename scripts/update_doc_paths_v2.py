import os
import glob
import re

# Order matters: more specific first
replacements = [
    (r"apps/oikos/apps/web/lib/", "apps/zerg/frontend-web/src/oikos/lib/"),
    (r"apps/oikos/apps/web/src/", "apps/zerg/frontend-web/src/oikos/app/"),
    (r"apps/oikos/apps/web/", "apps/zerg/frontend-web/src/oikos/"),
    (r"apps/oikos/packages/core/src/", "apps/zerg/frontend-web/src/oikos/core/"),
    (r"apps/oikos/packages/core/", "apps/zerg/frontend-web/src/oikos/core/"),
    (r"apps/oikos/packages/data/local/src/", "apps/zerg/frontend-web/src/oikos/data/"),
    (r"apps/oikos/packages/data/local/", "apps/zerg/frontend-web/src/oikos/data/"),
    (r"apps/oikos/", "apps/zerg/frontend-web/src/oikos/"),
    (r"src/oikos/core/src/", "apps/zerg/frontend-web/src/oikos/core/"), # Fix previous mistakes
    (r"src/oikos/data/src/", "apps/zerg/frontend-web/src/oikos/data/"), # Fix previous mistakes
    (r"src/oikos/lib/", "apps/zerg/frontend-web/src/oikos/lib/"), # Fix previous mistakes
    (r"src/oikos/", "apps/zerg/frontend-web/src/oikos/"), # Fix previous mistakes
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
