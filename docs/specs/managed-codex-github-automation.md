# Managed Codex GitHub Automation

This is the recommended shape if you want GitHub Actions to watch upstream `openai/codex` and drive Longhouse managed-Codex upkeep.

## Goal

On a schedule:

1. Detect whether upstream shipped a newer Codex tag than the one currently pinned in `scripts/release/build-managed-codex.sh`.
2. Decide whether the carried Longhouse patch for `openai/codex#18203` still matters.
3. Produce an agent-ready advisory prompt for a one-off Claude/Codex review.
4. Dispatch the existing runtime release workflow with explicit managed-Codex upstream overrides when the deterministic policy says it is safe.

## What Already Exists In Zerg

- `scripts/release/build-managed-codex.sh`
  Builds the forked managed Codex binary from an upstream ref plus `managed-codex.patch`.
- `scripts/release/check-managed-codex-upstream.py`
  Compares the pinned upstream version/ref to upstream tags/releases, checks whether the carried patch still applies, and emits an agent-ready prompt.
- `scripts/qa/repro-codex-remote-backpressure.sh`
  Existing live probe for the remote-TUI websocket/backpressure failure path.
- `.github/workflows/local-runtime-release.yml`
  Existing notarized/package/sign/release path. It now accepts optional workflow-dispatch overrides for:
  - `managed_codex_upstream_ref`
  - `managed_codex_upstream_version`
  - `managed_codex_build_version`
- `.github/workflows/managed-codex-upstream-watch.yml`
  Scheduled/manual watcher for upstream Codex releases/tags that emits a report and can dispatch the runtime release workflow.

## Important Constraint

Today the Longhouse installer resolves managed-Codex artifacts from the active Longhouse release tag in `server/zerg/services/runtime_artifacts.py`.

Implication:

- GitHub Actions can automate detection, reporting, and candidate packaging now.
- A clean unattended rollout to all managed users still needs either:
  - a new Longhouse release tag, or
  - the separate managed-runtime manifest/update channel described in the session note.

Do not confuse "candidate build exists" with "all managed users will pick it up automatically."

## Workflow Shape

The intended split is:

1. `managed-codex-upstream-watch.yml`
   Runs on a schedule or manually. Produces:
   - `managed-codex-report.json`
   - `managed-codex-agent-prompt.txt`
   - a GitHub Actions step summary with the candidate tag/ref, patch status, desired managed build version, and current published managed-Codex version on the target Longhouse release
2. Optional advisory step
   Run the emitted prompt through Claude/Codex on a protected self-hosted runner, or manually from Longhouse/hatch.
3. Policy-gated package dispatch
   The watch workflow can dispatch `local-runtime-release.yml` with:
   - the target Longhouse release tag
   - the upstream Codex ref/version to build
   - the managed build version suffix

## Dispatch Policy

The deterministic mirror lane should be boring. The workflow now defaults to `latest_published_release`, not `latest_tag`, on unattended runs.

Schedule auto-dispatch should only happen when all of these are true:

- upstream published release is newer than the currently pinned upstream Codex version
- `patch_status == applies_cleanly`
- target Longhouse release tag is configured
- the target Longhouse release does not already publish the same managed Codex build version

Repository variables:

- `LONGHOUSE_RUNTIME_RELEASE_TAG`
  Existing Longhouse release tag whose runtime assets should be refreshed
- `MANAGED_CODEX_AUTO_DISPATCH`
  Set to `true` to let the scheduled watcher dispatch the runtime release workflow automatically
- `MANAGED_CODEX_BUILD_SUFFIX`
  Optional suffix override for the managed build version; defaults to `longhouse.1`

## Why The Advisory Step Is Separate

The update detector is deterministic and safe on a standard GitHub-hosted runner.

The "have Claude or Codex inspect this release/bug path" step is different:

- it needs model credentials or a local wrapper such as `hatch`
- it may need a protected self-hosted runner if you want private prompts/secrets
- if you want a live bug probe, it should run the existing remote-backpressure harness on a runner where that runtime setup is intentional

So the first cut should automate detection and packaging dispatch, and treat the agent review as a protected follow-up step instead of pretending every public GitHub runner should be allowed to do it.

## Practical Decision Logic

- `patch_status == already_upstream_or_equivalent`
  Candidate likely contains your fix already. Run the live probe against stock upstream before keeping the fork patch.
- `patch_status == applies_cleanly`
  Upstream still looks patchable. Run the live probe; if the bug still reproduces, rebuild the fork and ship the patched binary.
- `patch_status == conflicts`
  Stop and inspect manually. The release changed the transport path enough that blind automation is not safe.

## Recommended First Cut

Keep the first version narrow:

1. Scheduled upstream watch in GitHub Actions.
2. Deterministic auto-dispatch only when the patch applies cleanly and the desired managed Codex build is not already published on the target Longhouse release.
3. Agent prompt artifact for the one-off Claude/Codex review.

That gets you the release-awareness loop now, without pretending the Longhouse runtime installer already has a separate managed-Codex update channel.
