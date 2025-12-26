import os
import glob
import re

# Order matters: more specific first
replacements = [
    (r"apps/jarvis/apps/web/lib/", "apps/zerg/frontend-web/src/jarvis/lib/"),
    (r"apps/jarvis/apps/web/src/", "apps/zerg/frontend-web/src/jarvis/app/"),
    (r"apps/jarvis/apps/web/", "apps/zerg/frontend-web/src/jarvis/"),
    (r"apps/jarvis/packages/core/src/", "apps/zerg/frontend-web/src/jarvis/core/"),
    (r"apps/jarvis/packages/core/", "apps/zerg/frontend-web/src/jarvis/core/"),
    (r"apps/jarvis/packages/data/local/src/", "apps/zerg/frontend-web/src/jarvis/data/"),
    (r"apps/jarvis/packages/data/local/", "apps/zerg/frontend-web/src/jarvis/data/"),
    (r"apps/jarvis/", "apps/zerg/frontend-web/src/jarvis/"),
    (r"src/jarvis/core/src/", "apps/zerg/frontend-web/src/jarvis/core/"), # Fix previous mistakes
    (r"src/jarvis/data/src/", "apps/zerg/frontend-web/src/jarvis/data/"), # Fix previous mistakes
    (r"src/jarvis/lib/", "apps/zerg/frontend-web/src/jarvis/lib/"), # Fix previous mistakes
    (r"src/jarvis/", "apps/zerg/frontend-web/src/jarvis/"), # Fix previous mistakes
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
