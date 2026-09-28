#!/usr/bin/env python3
"""Print the source stamp the generated iOS Xcode project was built from.

The generated project is gitignored, so a pull that adds, moves or deletes a
Swift file leaves a local project pointing at the old paths. The stamp covers
everything xcodegen reads to lay out the project: `project.yml` itself, the
sorted repo-relative path of every `.swift` file under `ios/Sources` and
`ios/Tests`, and the bundled resource files. `make ios-project` writes it; the
pre-build check recomputes it, so any add, move or delete fails before
compilation with "run make ios-project".

Paths, not contents: editing a file never needs a regenerated project.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios"
# Generated at build time by stage_ios_build_identity.sh, never by xcodegen's view.
GENERATED_RESOURCES = {"build-identity.json"}


def stamp_inputs(root: Path = ROOT) -> list[str]:
    ios = root / "ios"
    swift = sorted(
        path.relative_to(root).as_posix()
        for source_root in (ios / "Sources", ios / "Tests")
        for path in source_root.rglob("*.swift")
    )
    # An asset catalog is one folder reference to Xcode; its contents never
    # change the project file, so only files outside catalogs count.
    resources = sorted(
        path.relative_to(root).as_posix()
        for path in (ios / "Resources").rglob("*")
        if path.is_file()
        and path.name not in GENERATED_RESOURCES
        and path.name != ".DS_Store"
        and not any(part.endswith(".xcassets") for part in path.relative_to(ios / "Resources").parts)
    )
    return swift + resources


def project_stamp(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    digest.update((root / "ios" / "XcodeHarness" / "project.yml").read_bytes())
    for relative in stamp_inputs(root):
        digest.update(b"\0" + relative.encode())
    return digest.hexdigest()


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    print(project_stamp(root))
