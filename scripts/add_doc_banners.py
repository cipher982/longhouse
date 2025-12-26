import os
import glob

def add_banner(directory, banner_text, check_string):
    for filepath in glob.glob(os.path.join(directory, "*.md")):
        if "README.md" in filepath:
            continue
        with open(filepath, "r") as f:
            content = f.read()

        if check_string in content:
            print(f"Skipping {filepath} (already has banner)")
            continue

        print(f"Adding banner to {filepath}")
        new_content = banner_text + "\n\n" + content
        with open(filepath, "w") as f:
            f.write(new_content)

archive_banner = """# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---"""

completed_banner = """# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---"""

add_banner("docs/archive", archive_banner, "ARCHIVED")
add_banner("docs/completed", completed_banner, "COMPLETED")
