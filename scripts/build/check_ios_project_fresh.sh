#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_FILE="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj/project.pbxproj"
PROJECT_SPEC="$ROOT_DIR/ios/XcodeHarness/project.yml"
PROJECT_STAMP="$ROOT_DIR/ios/XcodeHarness/.project-source-sha256"

if [[ ! -f "$PROJECT_FILE" || ! -f "$PROJECT_STAMP" ]]; then
    echo "iOS Xcode project is not generated. Run 'make ios-project', then close and reopen Xcode." >&2
    exit 1
fi

python3 - "$ROOT_DIR" "$PROJECT_FILE" "$PROJECT_SPEC" "$PROJECT_STAMP" <<'PY'
from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

root, project_file, project_spec, project_stamp = map(Path, sys.argv[1:])
project = project_file.read_text()

swift_files = [
    path
    for source_root in (root / "ios" / "Sources", root / "ios" / "Tests")
    for path in source_root.rglob("*.swift")
]
actual_files = Counter(path.name for path in swift_files)
project_files = Counter(
    match.group(1)
    for match in re.finditer(
        r"/\* ([^*]+\.swift) \*/ = \{isa = PBXFileReference;",
        project,
    )
)
source_build_files = Counter(
    match.group(1)
    for match in re.finditer(
        r"/\* ([^*]+\.swift) in Sources \*/ = \{isa = PBXBuildFile;",
        project,
    )
)

missing_references = sorted(name for name in actual_files if project_files[name] < actual_files[name])
extra_references = sorted(name for name in project_files if project_files[name] > actual_files[name])
missing_source_builds = sorted(
    name
    for path in root.joinpath("ios", "Sources").rglob("*.swift")
    for name in [path.name]
    if source_build_files[name] < (2 if "Sources/Shared" in str(path) else 1)
)
expected_hash = hashlib.sha256(project_spec.read_bytes()).hexdigest()
actual_hash = project_stamp.read_text().strip()

problems = []
if expected_hash != actual_hash:
    problems.append("project.yml changed since the last project generation")
if missing_references:
    problems.append("missing project file references: " + ", ".join(missing_references))
if extra_references:
    problems.append("stale project file references: " + ", ".join(extra_references))
if missing_source_builds:
    problems.append("source files missing from a target Sources phase: " + ", ".join(missing_source_builds))

if problems:
    print("iOS Xcode project is stale:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("Run 'make ios-project', then close and reopen Xcode.", file=sys.stderr)
    raise SystemExit(1)

print("iOS Xcode project is current.")
PY
