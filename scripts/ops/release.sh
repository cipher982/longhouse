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
   2. Commits the versioned candidate locally and runs the full validation.
   3. Pushes the validated candidate to main.
   4. Waits for exact-SHA CI, deploy (including hosted QA), installer, and live-surface gates.
   5. Creates the GitHub release with tag VERSION (fires publish.yml + local-runtime-release.yml).
   6. Waits for both release workflows to finish. Notarization can take up to ~330m in the worst case.
   7. Verifies the release has the expected artifacts and that macOS notarization is notarized.

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
CURRENT_VERSION="$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/version *= *"([^"]+)".*/\1/')"

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
git -C "$ROOT" fetch --quiet --tags origin main
LOCAL_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
REMOTE_HEAD="$(git -C "$ROOT" rev-parse origin/main)"
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  if [[ "$CURRENT_VERSION" != "$PYVER" ]] || ! git -C "$ROOT" merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL_HEAD"; then
    echo "Local main ($LOCAL_HEAD) does not match origin/main ($REMOTE_HEAD)." >&2
    echo "Push (or discard) local work before releasing — this guards against sweeping another agent's WIP into the release." >&2
    exit 1
  fi
  echo "Resuming $VERSION from local main ahead of origin/main at ${LOCAL_HEAD:0:10}."
fi

if git -C "$ROOT" rev-parse --verify --quiet "refs/tags/$VERSION" >/dev/null; then
  echo "Tag $VERSION already exists locally. Pick a new version." >&2
  exit 1
fi

if git -C "$ROOT" ls-remote --tags origin "refs/tags/$VERSION" | grep -q "$VERSION"; then
  echo "Tag $VERSION already exists on origin. Pick a new version." >&2
  exit 1
fi

if [[ "$CURRENT_VERSION" == "$PYVER" ]]; then
  echo "All manifests are already at $PYVER; reusing the current candidate and validating it again."
else
  if ! command -v bump-my-version >/dev/null 2>&1; then
    echo "bump-my-version not found on PATH. Install with: uv tool install bump-my-version" >&2
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
fi

# A retry may arrive after the bump commit, so verify the shared-version
# invariant directly instead of trusting only the server's anchor manifest.
VERSION_MARKERS=(
  "server/pyproject.toml|version = \"$PYVER\""
  "engine/Cargo.toml|version = \"$PYVER\""
  "runner/package.json|\"version\": \"$PYVER\""
  "ios/XcodeHarness/Configs/Version.xcconfig|MARKETING_VERSION = $PYVER"
  ".bumpversion.toml|current_version = \"$PYVER\""
)
for entry in "${VERSION_MARKERS[@]}"; do
  file="${entry%%|*}"
  marker="${entry#*|}"
  if ! grep -Fq "$marker" "$ROOT/$file"; then
    echo "$file does not declare the shared release version $PYVER." >&2
    exit 1
  fi
done

if [[ "$CURRENT_VERSION" != "$PYVER" ]]; then
  git -C "$ROOT" add \
    server/pyproject.toml \
    server/uv.lock \
    engine/Cargo.toml \
    engine/Cargo.lock \
    runner/package.json \
    ios/XcodeHarness/Configs/Version.xcconfig \
    .bumpversion.toml
  git -C "$ROOT" commit -m "Bump version to $PYVER"
fi

BUMP_SHA="$(git -C "$ROOT" rev-parse HEAD)"
echo "Versioned candidate: ${BUMP_SHA:0:10}"

echo "Running full release validation on the exact candidate commit..."
(cd "$ROOT" && make test-ci)

if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "Release validation changed tracked files. Commit the generated updates, then rerun the same release." >&2
  exit 1
fi

echo "Pushing versioned candidate to main..."
# Race-safe: only push if origin/main hasn't moved since the clean check above.
# If another agent pushed in between, bail out so they can land and we retry.
if ! git -C "$ROOT" push origin "$BUMP_SHA:refs/heads/main"; then
  echo "Push failed — another commit likely landed on origin/main. Rewind and retry:" >&2
  echo "  reconcile local main with origin/main, then rerun make release VERSION=$VERSION" >&2
  exit 1
fi

# GitHub path filters may omit required release gates when the final candidate
# only changes another product surface. Give push-triggered runs a moment to
# register, then dispatch only the exact-SHA gates GitHub did not create.
sleep 10
for workflow in deploy-and-verify.yml launch-gate.yml; do
  run_count="$(gh run list \
    --repo cipher982/longhouse \
    --workflow "$workflow" \
    --commit "$BUMP_SHA" \
    --limit 1 \
    --json databaseId \
    --jq 'length')"
  if [[ "$run_count" == "0" ]]; then
    echo "Dispatching missing exact-SHA workflow: $workflow"
    gh workflow run "$workflow" --repo cipher982/longhouse --ref main
  fi
done

echo "Waiting for pre-release exact-SHA gates before creating $VERSION..."
"$ROOT/scripts/ops/launch-readiness.py" \
  --sha "$BUMP_SHA" \
  --required-workflow "CI" \
  --required-workflow "Deploy and Verify" \
  --required-workflow "Launch Gate" \
  --skip-release \
  --skip-public-package \
  --skip-runtime-artifacts \
  --wait --timeout 7200 --discovery-grace 1800 --poll 30

echo "Creating GitHub release $VERSION (this triggers publish.yml + local-runtime-release.yml)..."
PREV_TAG="$(git -C "$ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -1 || true)"
NOTES=""
if [[ -n "$PREV_TAG" ]]; then
  NOTES="**Full Changelog**: https://github.com/cipher982/longhouse/compare/$PREV_TAG...$VERSION"
fi

# Anything we accept as "this release's run" must have started after this.
RELEASE_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

gh release create "$VERSION" \
  --target "$BUMP_SHA" \
  --title "$VERSION" \
  --notes "$NOTES"

echo "Release $VERSION created. Waiting for publish.yml and local-runtime-release.yml to finish..."
echo "(macOS notarization can take a while. Default Apple wait: 330 minutes.)"

# Release-event runs can be badly delayed. On 2026-08-26 the release published
# at 16:11 and the runs did not appear until 16:30 -- nineteen minutes, with the
# tag resolved, the commit on main, and both workflows active the whole time.
#
# The fallback below exists for the case where they never arrive at all, and its
# fuse is deliberately long. A short one is actively harmful: dispatching at
# three minutes on that release produced a second publish run, which uploaded
# the wheel first and left the real release-event run to die on "400 File
# already exists", plus two concurrent macOS signing jobs racing to attach the
# same assets. Waiting costs nothing; dispatching early costs a failed run and a
# duplicate notarization.
DISPATCH_GRACE_SECONDS="${DISPATCH_GRACE_SECONDS:-1800}"

wait_run() {
  local workflow="$1"
  local deadline=$(( $(date +%s) + 60*60*6 ))
  local dispatch_after=$(( $(date +%s) + DISPATCH_GRACE_SECONDS ))
  local dispatched=false
  while true; do
    local run_info
    # Accept a run from either event: the release event when it fires, or our
    # own dispatch when it does not.
    run_info="$(gh run list \
      --workflow "$workflow" \
      --json databaseId,status,conclusion,headBranch,displayTitle,createdAt,event \
      --limit 10 \
      --jq "[.[] | select(.createdAt >= \"$RELEASE_STARTED_AT\") | select((.displayTitle | contains(\"$VERSION\")) or (.event == \"workflow_dispatch\"))][0]" || true)"

    if [[ -z "$run_info" || "$run_info" == "null" ]] && [[ "$dispatched" == "false" ]] && (( $(date +%s) > dispatch_after )); then
      echo "  $workflow: no run appeared from the release event; dispatching it directly"
      gh workflow run "$workflow" -f tag_name="$VERSION" >/dev/null 2>&1 || true
      dispatched=true
      sleep 15
      continue
    fi

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
"$ROOT/scripts/ops/launch-readiness.py" --sha "$BUMP_SHA" --wait --timeout 1800 --poll 30

echo ""
echo "Release $VERSION shipped and verified."
echo "  gh release view $VERSION"
echo "  Users can upgrade: curl -fsSL https://get.longhouse.ai/install.sh | bash"
