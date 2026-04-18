#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
Usage: release.sh VERSION

  VERSION is the tag to cut (e.g. v0.1.13).

Cuts a stable Longhouse release:
  1. Bumps server/pyproject.toml version to match VERSION.
  2. Commits + pushes the bump to main.
  3. Creates the GitHub release with tag VERSION (fires publish.yml + local-runtime-release.yml).
  4. Waits for both workflows to finish. Notarization can take up to ~330m in the worst case.
  5. Verifies the release has the expected artifacts and that macOS notarization is notarized.

Does not push to PyPI directly — publish.yml does that from the release event.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

VERSION="$1"
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must match vX.Y.Z (e.g. v0.1.13). Got: $VERSION" >&2
  exit 2
fi

PYVER="${VERSION#v}"
PYPROJECT="$ROOT/server/pyproject.toml"

if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "Working tree has uncommitted changes. Commit or stash before releasing." >&2
  exit 1
fi

BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD)"
if [[ "$BRANCH" != "main" ]]; then
  echo "Refusing to release from branch '$BRANCH'. Release from main." >&2
  exit 1
fi

if git -C "$ROOT" rev-parse --verify --quiet "refs/tags/$VERSION" >/dev/null; then
  echo "Tag $VERSION already exists locally. Pick a new version." >&2
  exit 1
fi

if git -C "$ROOT" ls-remote --tags origin "refs/tags/$VERSION" | grep -q "$VERSION"; then
  echo "Tag $VERSION already exists on origin. Pick a new version." >&2
  exit 1
fi

CURRENT_VERSION="$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/version *= *"([^"]+)".*/\1/')"
if [[ "$CURRENT_VERSION" == "$PYVER" ]]; then
  echo "pyproject.toml is already at $PYVER. Bump to a new version first." >&2
  exit 1
fi

echo "Bumping pyproject.toml from $CURRENT_VERSION to $PYVER..."
python3 -c "
import sys
from pathlib import Path
path = Path('$PYPROJECT')
text = path.read_text()
new = []
found = False
for line in text.splitlines(keepends=True):
    if not found and line.startswith('version'):
        new.append(f'version = \"$PYVER\"\n')
        found = True
    else:
        new.append(line)
if not found:
    print('Could not find version line', file=sys.stderr)
    sys.exit(1)
path.write_text(''.join(new))
"

git -C "$ROOT" add server/pyproject.toml
git -C "$ROOT" commit -m "Bump version to $PYVER"
BUMP_SHA="$(git -C "$ROOT" rev-parse HEAD)"
echo "Bump commit: ${BUMP_SHA:0:10}"

echo "Pushing bump commit to main..."
git -C "$ROOT" push origin "$BUMP_SHA:refs/heads/main"

echo "Creating GitHub release $VERSION (this triggers publish.yml + local-runtime-release.yml)..."
PREV_TAG="$(git -C "$ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -1 || true)"
NOTES=""
if [[ -n "$PREV_TAG" ]]; then
  NOTES="**Full Changelog**: https://github.com/cipher982/longhouse/compare/$PREV_TAG...$VERSION"
fi

gh release create "$VERSION" \
  --target "$BUMP_SHA" \
  --title "$VERSION" \
  --notes "$NOTES"

echo "Release $VERSION created. Waiting for publish.yml and local-runtime-release.yml to finish..."
echo "(macOS notarization can take a while. Default Apple wait: 330 minutes.)"

wait_run() {
  local workflow="$1"
  local deadline=$(( $(date +%s) + 60*60*6 ))
  while true; do
    local run_info
    run_info="$(gh run list \
      --workflow "$workflow" \
      --event release \
      --json databaseId,status,conclusion,headBranch,displayTitle,createdAt \
      --limit 5 \
      --jq "[.[] | select(.displayTitle | contains(\"$VERSION\"))][0]" || true)"

    if [[ -n "$run_info" && "$run_info" != "null" ]]; then
      local status conclusion id
      status="$(echo "$run_info" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
      conclusion="$(echo "$run_info" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("conclusion") or "")')"
      id="$(echo "$run_info" | python3 -c 'import json,sys; print(json.load(sys.stdin)["databaseId"])')"

      if [[ "$status" == "completed" ]]; then
        if [[ "$conclusion" == "success" ]]; then
          echo "  [OK] $workflow run $id succeeded"
          return 0
        fi
        echo "  [FAIL] $workflow run $id conclusion=$conclusion"
        echo "  View: gh run view $id --log-failed"
        return 1
      fi
      echo "  $workflow run $id status=$status (polling...)"
    else
      echo "  $workflow: no release-event run found yet for $VERSION (polling...)"
    fi

    if (( $(date +%s) > deadline )); then
      echo "Timed out waiting for $workflow" >&2
      return 1
    fi
    sleep 30
  done
}

wait_run publish.yml
wait_run local-runtime-release.yml

echo ""
echo "Verifying release artifacts..."
ASSETS="$(gh release view "$VERSION" --json assets --jq '.assets[].name' | sort)"
echo "$ASSETS"

for required in \
  "longhouse-$PYVER-py3-none-any.whl" \
  "longhouse-engine-darwin-arm64" \
  "longhouse-engine-linux-x64" \
  "longhouse-codex-darwin-arm64" \
  "Longhouse-macos-arm64.dmg" \
  "local-runtime-macos-packaging.json"; do
  if ! grep -q "^$required$" <<<"$ASSETS"; then
    echo "  [FAIL] Missing expected asset: $required" >&2
    exit 1
  fi
done

echo ""
echo "Verifying macOS notarization..."
MANIFEST="$(gh release download "$VERSION" --pattern local-runtime-macos-packaging.json --output - 2>/dev/null)"
APP_STATUS="$(echo "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("notarization_status"))')"
DMG_STATUS="$(echo "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("public_download_notarization_status"))')"

if [[ "$APP_STATUS" != "notarized" ]] || [[ "$DMG_STATUS" != "notarized" ]]; then
  echo "  [FAIL] Notarization incomplete: app=$APP_STATUS dmg=$DMG_STATUS" >&2
  exit 1
fi
echo "  [OK] app and DMG are notarized"

echo ""
echo "Release $VERSION shipped and verified."
echo "  gh release view $VERSION"
echo "  Users can upgrade: uv tool upgrade longhouse && longhouse connect --install"
