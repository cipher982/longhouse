#!/usr/bin/env bash
# Stage .build/build-identity.json as a bundled iOS resource.
#
# Invoked as an XcodeGen pre-build script phase so `xcodebuild` gets the
# latest identity copied into Resources/ before the Copy Bundle Resources
# phase runs. Separate from the Python generator on purpose:
#
# - The generator needs a full python3 + repo layout; Xcode build phases
#   should be a thin shell shim.
# - The generator must already have run once this session (dev loop, CI,
#   or release pipeline). If it hasn't, fail loud — an iOS build without
#   a real identity would lie about its version.
#
# Inputs/outputs are declared in project.yml so Xcode caches this phase.
set -euo pipefail

# Repo root — three levels up from this script (scripts/build/*.sh).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="${LONGHOUSE_BUILD_IDENTITY_PATH:-${REPO_ROOT}/.build/build-identity.json}"
DEST="${REPO_ROOT}/ios/Resources/build-identity.json"

if [[ ! -f "${SOURCE}" ]]; then
    echo "error: build identity missing at ${SOURCE}" >&2
    echo "       run scripts/build/generate_build_identity.py first" >&2
    exit 1
fi

mkdir -p "$(dirname "${DEST}")"
cp "${SOURCE}" "${DEST}"
