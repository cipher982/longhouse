#!/usr/bin/env bash
# Install a pinned, hash-checked XcodeGen release for CI. `brew install
# xcodegen` floats to whatever Homebrew ships that day, and the generated
# project (and therefore the build) changes with it.
set -euo pipefail

XCODEGEN_VERSION="2.46.0"
XCODEGEN_SHA256="4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"

dest="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/xcodegen-${XCODEGEN_VERSION}"
rm -rf "$dest"
mkdir -p "$dest"
curl -fsSL -o "$dest/xcodegen.zip" \
  "https://github.com/yonaskolb/XcodeGen/releases/download/${XCODEGEN_VERSION}/xcodegen.zip"
echo "${XCODEGEN_SHA256}  $dest/xcodegen.zip" | shasum -a 256 -c -
unzip -q "$dest/xcodegen.zip" -d "$dest"
"$dest/xcodegen/bin/xcodegen" --version

if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$dest/xcodegen/bin" >> "$GITHUB_PATH"
else
  echo "Add $dest/xcodegen/bin to PATH"
fi
