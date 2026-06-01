#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
Usage: release.sh VERSION

  VERSION is the tag to cut (e.g. v0.1.13).

Cuts a stable Longhouse release:
  1. Bumps every public component manifest (server, engine, runner,
     iOS xcconfig) to the same shared release version via bump-my-version.
     Note: this is the release version, not the per-commit build identity.
     Build identity advances on every commit; release version only moves
     when you run this script.
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

# Shared-worktree guard: another agent may have committed to local main without
# pushing. Refuse to release until local main == origin/main so we only release
# commits that exist on origin and that the user can see in GitHub.
git -C "$ROOT" fetch --quiet origin main
LOCAL_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
REMOTE_HEAD="$(git -C "$ROOT" rev-parse origin/main)"
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  echo "Local main ($LOCAL_HEAD) does not match origin/main ($REMOTE_HEAD)." >&2
  echo "Push (or discard) local work before releasing — this guards against sweeping another agent's WIP into the release." >&2
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

if ! command -v bump-my-version >/dev/null 2>&1; then
  echo "bump-my-version not found on PATH. Install with: uv tool install bump-my-version" >&2
  exit 1
fi

CURRENT_VERSION="$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/version *= *"([^"]+)".*/\1/')"
if [[ "$CURRENT_VERSION" == "$PYVER" ]]; then
  echo "pyproject.toml is already at $PYVER. Bump to a new version first." >&2
  exit 1
fi

echo "Bumping all manifests from $CURRENT_VERSION to $PYVER (shared release version)..."
# bump-my-version edits every file listed in .bumpversion.toml and bails
# if any of them don't contain the expected old version — that's the
# shared-version guarantee. If you see a mismatch error here, another
# agent likely hand-edited one of the manifests.
(cd "$ROOT" && bump-my-version bump --new-version "$PYVER")

echo "Refreshing package lockfiles for $PYVER..."
(cd "$ROOT/server" && uv lock)
(cd "$ROOT/engine" && cargo metadata --format-version 1 >/dev/null)

git -C "$ROOT" add \
  server/pyproject.toml \
  server/uv.lock \
  engine/Cargo.toml \
  engine/Cargo.lock \
  runner/package.json \
  ios/XcodeHarness/Configs/Version.xcconfig \
  .bumpversion.toml
git -C "$ROOT" commit -m "Bump version to $PYVER"
BUMP_SHA="$(git -C "$ROOT" rev-parse HEAD)"
echo "Bump commit: ${BUMP_SHA:0:10}"

echo "Pushing bump commit to main..."
# Race-safe: only push if origin/main hasn't moved since the clean check above.
# If another agent pushed in between, bail out so they can land and we retry.
if ! git -C "$ROOT" push origin "$BUMP_SHA:refs/heads/main"; then
  echo "Push failed — another commit likely landed on origin/main. Rewind and retry:" >&2
  echo "  git reset --hard origin/main && make release VERSION=$VERSION" >&2
  exit 1
fi

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
echo "Verifying launch readiness for $BUMP_SHA..."
"$ROOT/scripts/ops/launch-readiness.py" --sha "$BUMP_SHA" --wait --timeout 7200 --poll 30

echo ""
echo "Release $VERSION shipped and verified."
echo "  gh release view $VERSION"
echo "  Users can upgrade: uv tool upgrade longhouse && longhouse connect --install"
