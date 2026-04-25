#!/usr/bin/env bash
# Stage .build/build-identity.json as a bundled iOS resource.
#
# Invoked as an XcodeGen pre-build script phase so `xcodebuild` gets the
# latest identity copied into Resources/ before the Copy Bundle Resources
# phase runs. The script regenerates identity first so direct Xcode builds
# stay honest after a new commit, not just Makefile/CI builds.
#
# If generation fails, fail loud — an iOS build without a real identity
# would lie about its version.
#
# Inputs/outputs are declared in project.yml so Xcode caches this phase.
set -euo pipefail

# Repo root — three levels up from this script (scripts/build/*.sh).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="${REPO_ROOT}/.build/build-identity.json"
DEST="${REPO_ROOT}/ios/Resources/build-identity.json"
GENERATOR="${REPO_ROOT}/scripts/build/generate_build_identity.py"

python3 "${GENERATOR}"

if [[ ! -f "${SOURCE}" ]]; then
    echo "error: build identity missing at ${SOURCE}" >&2
    echo "       generator did not produce expected output" >&2
    exit 1
fi

# Freshness guard: if git is available, demand the staged identity's commit
# matches HEAD. Catches the same class of bug as engine/build.rs — a stale
# file staged in a prior invocation silently shipping to the iOS bundle.
if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse HEAD >/dev/null 2>&1; then
    HEAD_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
    STAGED_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "${SOURCE}")"
    if [[ "${HEAD_SHA}" != "${STAGED_SHA}" ]]; then
        echo "error: build identity at ${SOURCE} is stale: commit=${STAGED_SHA} but git HEAD=${HEAD_SHA}" >&2
        echo "       run scripts/build/generate_build_identity.py and retry" >&2
        exit 1
    fi
fi

mkdir -p "$(dirname "${DEST}")"
cp "${SOURCE}" "${DEST}"
