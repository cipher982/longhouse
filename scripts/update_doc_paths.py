import os
import glob
import re

replacements = [
    (r"apps/jarvis/apps/web/lib/", "src/jarvis/lib/"),
    (r"apps/jarvis/apps/web/", "src/jarvis/"),
    (r"apps/jarvis/packages/core/", "src/jarvis/core/"),
    (r"apps/jarvis/packages/data/local/", "src/jarvis/data/"),
    (r"apps/jarvis/", "apps/zerg/frontend-web/src/jarvis/"),
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
