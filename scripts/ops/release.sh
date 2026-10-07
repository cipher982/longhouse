#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
Usage: release.sh VERSION

  VERSION is the tag to cut (e.g. v0.1.13).

Cuts a stable Longhouse release. Start it from any checkout, clean or not, on
any branch: it takes the release lock (scripts/ops/ring_lock.py; a second
release refuses and names the first), makes a disposable worktree of the exact
origin/main commit under /tmp/agents, runs every step below from there, and
removes it on exit, success or failure. The primary checkout is not involved.
  1. Bumps every public component manifest (server, engine, runner,
     iOS xcconfig) to the same shared release version via bump-my-version.
     Note: this is the release version, not the per-commit build identity.
     Build identity advances on every commit; release version only moves
     when you run this script.
   2. Commits the versioned candidate locally and runs the full validation
      (`make test-ci`) under the shared heavy-build lock. Only this step holds
      the lock; the rest of the release waits on GitHub, so other agents' builds
      are not blocked for the few minutes that takes.
   3. Pushes the validated candidate to main.
   4. Waits for exact-SHA CI, deploy, installer, and live-surface gates (hosted QA runs
      asynchronously and gates production promotion, not the release).
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

CANDIDATE_REF="refs/longhouse/release-candidates/$VERSION"

# --- The run you start: the release lock and a disposable exact-SHA checkout --
# A release used to need the primary checkout on main for its ~30 minutes, so it
# blocked `make dogfood-refresh` and was blocked by any agent that left that
# checkout dirty or behind origin/main. This part only takes the lock, picks the
# start commit, and runs this same script from a disposable worktree of it.
if [[ -z "${LONGHOUSE_RELEASE_CHECKOUT:-}" ]]; then
  . "$ROOT/scripts/lib/ring-lock.sh"
  . "$ROOT/scripts/lib/exact-checkout.sh"
  trap 'lh_exact_checkout_cleanup; lh_ring_lock_release' EXIT
  git -C "$ROOT" fetch --quiet origin main
  REMOTE_HEAD="$(git -C "$ROOT" rev-parse origin/main)"
  # Releases took a median 28 min and up to 77 min (v0.1.68..v0.1.75, bump commit to
  # release workflows done), and single steps block much longer (the heavy-build lock
  # wait, the 2 h gate wait, up to 6 h of notarization). No fixed TTL holds a live
  # release without also holding a dead one for hours, so the keepalive renews it every
  # minute while this script lives; the 10 min TTL only bounds a stopped keepalive (a
  # hung or suspended run), and a release that dies frees it at once.
  lh_ring_lock_acquire release "$REMOTE_HEAD" 600 "make release $VERSION" || {
    echo "Refusing: could not take the release lock (above)." >&2
    exit 1
  }
  lh_ring_lock_keepalive 60 600
  START="$REMOTE_HEAD"
  if candidate="$(git -C "$ROOT" rev-parse --verify --quiet "$CANDIDATE_REF")"; then
    # A bump commit an earlier run made but did not push: resume from it while it
    # still sits on origin/main; otherwise bump again from the new origin/main.
    if git -C "$ROOT" merge-base --is-ancestor "$REMOTE_HEAD" "$candidate"; then
      START="$candidate"
      echo "Resuming $VERSION from its unpushed candidate ${candidate:0:10}."
    else
      echo "Dropping the unpushed $VERSION candidate ${candidate:0:10}: origin/main moved; bumping again."
      git -C "$ROOT" update-ref -d "$CANDIDATE_REF"
    fi
  fi
  lh_exact_checkout "$ROOT" "release-$VERSION" "$START"
  echo "Releasing from a disposable checkout of ${START:0:10}: $LH_EXACT_CHECKOUT"
  status=0
  LONGHOUSE_RELEASE_CHECKOUT=1 LH_RING_LOCK_SURFACE="$LH_RING_LOCK_SURFACE" LH_RING_LOCK_TOKEN="$LH_RING_LOCK_TOKEN" \
    bash "$LH_EXACT_CHECKOUT/scripts/ops/release.sh" "$VERSION" || status=$?
  exit "$status"
fi

# --- From here on: the disposable checkout ($ROOT), detached at the start commit --
cd "$ROOT"
. "$ROOT/scripts/lib/ring-lock.sh"  # the outer run's lock, renewed here with the candidate SHA

PYVER="${VERSION#v}"
PYPROJECT="$ROOT/server/pyproject.toml"
CURRENT_VERSION="$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/version *= *"([^"]+)".*/\1/')"

if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "Working tree has uncommitted changes. Commit or stash before releasing." >&2
  exit 1
fi

# The checkout is detached at origin/main, or at an unpushed bump candidate on top
# of it. Release only commits that exist on origin (plus that one bump), never
# another agent's unpushed work. Fetch only the branch here: local historical tags
# may intentionally differ from origin, and tag existence is checked against the
# remote below without clobbering them.
git -C "$ROOT" fetch --quiet origin main
LOCAL_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
REMOTE_HEAD="$(git -C "$ROOT" rev-parse origin/main)"
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  if [[ "$CURRENT_VERSION" != "$PYVER" ]] || ! git -C "$ROOT" merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL_HEAD"; then
    echo "The release checkout ($LOCAL_HEAD) is neither origin/main ($REMOTE_HEAD) nor a $PYVER bump on top of it; rerun make release VERSION=$VERSION." >&2
    exit 1
  fi
  echo "Resuming $VERSION from its candidate ${LOCAL_HEAD:0:10}, ahead of origin/main."
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
  # The checkout is removed on exit; this ref keeps the candidate for a rerun to resume.
  git -C "$ROOT" update-ref "$CANDIDATE_REF" HEAD
fi

BUMP_SHA="$(git -C "$ROOT" rev-parse HEAD)"
echo "Versioned candidate: ${BUMP_SHA:0:10}"
lh_ring_lock_renew 600 "$BUMP_SHA" >/dev/null || true  # the lock's status names the candidate

# The validation is the one heavy step of a release; everything after it waits on
# GitHub. Hold the machine-wide heavy-build lock for this step only, so other
# agents can build while the gates and notarization run (an outer
# `lockf <lock> make release` wrapper is detected, not deadlocked on).
. "$ROOT/scripts/lib/heavy-build-lock.sh"

# The isolated guest defaults to 2 CPU / 4 GiB, sized for cube's shared pods. A
# release holds the lock alone on a 16-core laptop; the 4 GiB ceiling is what
# OOM-killed rustc mid-validation on 2026-09-29 (v0.1.58) after ~30 minutes of
# swap thrash. Callers can still override either value.
docker_cpus="$(docker info --format '{{.NCPU}}' 2>/dev/null || true)"
[[ "$docker_cpus" =~ ^[0-9]+$ ]] || docker_cpus=8
export LONGHOUSE_TEST_CPUS="${LONGHOUSE_TEST_CPUS:-$(( docker_cpus < 8 ? docker_cpus : 8 ))}"
export LONGHOUSE_TEST_MEMORY="${LONGHOUSE_TEST_MEMORY:-8g}"

# A resume (same version, candidate already committed) must not repeat a
# validation the exact commit already passed: v0.1.58's gate failed after a green
# validation and a retry would have paid it again. The stamp is keyed by commit
# SHA in the clone's git common dir (the release checkout is disposable), written
# only once make test-ci succeeded and left the tree clean (below), so an edited
# candidate has a new SHA and revalidates, and a validation that dirtied the tree
# is never stamped.
VALIDATED_STAMP="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/release-validated/$BUMP_SHA"
if [[ -z "${RELEASE_REVALIDATE:-}" && -f "$VALIDATED_STAMP" ]]; then
  echo "Candidate ${BUMP_SHA:0:10} already passed make test-ci ($(cat "$VALIDATED_STAMP")); skipping. RELEASE_REVALIDATE=1 forces it."
else
  echo "Running full release validation on the exact candidate commit..."
  lh_run_heavy bash -c 'cd "$1" && make test-ci' _ "$ROOT"
  NEEDS_STAMP=1
fi

if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "Release validation changed tracked files. Commit the generated updates, then rerun the same release." >&2
  exit 1
fi
if [[ -n "${NEEDS_STAMP:-}" ]]; then
  mkdir -p "$(dirname "$VALIDATED_STAMP")"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$VALIDATED_STAMP"
fi

# A candidate that needed no bump commit is already on origin/main; other
# agents landing on top of it during validation must not fail the release.
lh_ring_lock_renew 600 "$BUMP_SHA" >/dev/null || {
  echo "This run no longer holds the release lock (above); not pushing. Rerun make release VERSION=$VERSION." >&2
  exit 1
}
git -C "$ROOT" fetch --quiet origin main
# Landing rule: local commits ahead of origin/main that touch the blocking list
# (scripts/ops/review-policy.toml) need a completed review before they reach main.
# A candidate already on origin/main has an empty range.
python3 "$ROOT/scripts/ops/review_gate.py" --repo "$ROOT" push --base origin/main --head "$BUMP_SHA"
if git -C "$ROOT" merge-base --is-ancestor "$BUMP_SHA" origin/main; then
  echo "Candidate ${BUMP_SHA:0:10} is already on origin/main; nothing to push."
# Race-safe: only push if origin/main hasn't moved since the clean check above.
# If another agent pushed in between, bail out so they can land and we retry.
elif echo "Pushing versioned candidate to main..." && ! git -C "$ROOT" push origin "$BUMP_SHA:refs/heads/main"; then
  # Other agents land on main during the validation. When the bump
  # commit is the only local commit, replay it onto origin/main: that only
  # adds already-pushed work, and the exact-SHA CI and deploy gates below run
  # on the rebased candidate before any release is created.
  git -C "$ROOT" fetch --quiet origin main
  if [[ "$(git -C "$ROOT" rev-list --count origin/main..HEAD)" != "1" ]] \
    || ! git -C "$ROOT" rebase --quiet origin/main \
    || ! BUMP_SHA="$(git -C "$ROOT" rev-parse HEAD)" \
    || ! git -C "$ROOT" push origin "$BUMP_SHA:refs/heads/main"; then
    echo "Push failed — another commit likely landed on origin/main. Rerun make release VERSION=$VERSION:" >&2
    echo "  it resumes from the unpushed candidate when it still sits on origin/main, else bumps again." >&2
    exit 1
  fi
  echo "Rebased versioned candidate onto origin/main: ${BUMP_SHA:0:10}"
fi
git -C "$ROOT" update-ref -d "$CANDIDATE_REF" 2>/dev/null || true  # on origin/main now: nothing left to resume
lh_ring_lock_renew 600 "$BUMP_SHA" >/dev/null || true

# GitHub path filters may omit required release gates when the final candidate
# only changes another product surface. Give push-triggered runs a moment to
# register, then dispatch only the exact-SHA gates GitHub did not create.
sleep "${RELEASE_SETTLE_SECONDS:-10}"
for workflow in runtime-image.yml deploy-and-verify.yml launch-gate.yml; do
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
# On a busy main another agent's push lands within minutes of the candidate's, and main's
# supersede logic cancels the candidate's queued runs while the canary moves past it
# (v0.1.76 died twice that way, 2026-10-07). --accept-covering takes coverage for identity
# there: a cancelled run passes when the same workflow succeeded on a main commit that
# contains the candidate, and the canary when it serves such a commit whose own Deploy and
# Verify passed. A cancelled run nothing covers is rerun for the exact SHA, at most twice.
# A genuine failure stays terminal.
# The canary ring (same source as deploy-status.sh); the public demo is production.
CANARY_SUBDOMAIN="${HOSTED_CANARY_SUBDOMAIN:-release-canary-a}"
CANARY_HEALTH_URL="https://${CANARY_SUBDOMAIN}.longhouse.ai/api/health"
"$ROOT/scripts/ops/launch-readiness.py" \
  --sha "$BUMP_SHA" \
  --canary-url "$CANARY_HEALTH_URL" \
  --required-workflow "CI" \
  --required-workflow "Deploy and Verify" \
  --required-workflow "Launch Gate" \
  --skip-release \
  --skip-public-package \
  --skip-runtime-artifacts \
  --skip-demo \
  --accept-covering --redispatch-superseded 2 \
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
"$ROOT/scripts/ops/launch-readiness.py" \
  --sha "$BUMP_SHA" \
  --canary-url "$CANARY_HEALTH_URL" \
  --skip-demo \
  --accept-covering --redispatch-superseded 2 \
  --wait --timeout 1800 --poll 30

echo ""
echo "Release $VERSION shipped and verified."
echo "  gh release view $VERSION"
echo "  Users can upgrade: curl -fsSL https://get.longhouse.ai/install.sh | bash"
