# Managed Codex GitLab Automation

This is the recommended shape if you want GitLab CI to watch upstream `openai/codex` and drive Longhouse managed-Codex upkeep.

## Goal

On a schedule:

1. Detect whether upstream shipped a newer Codex tag than the one currently pinned in `scripts/release/build-managed-codex.sh`.
2. Decide whether the carried Longhouse patch for `openai/codex#18203` still matters.
3. Optionally run an AI advisory pass over the release notes and patch status.
4. Build and publish a candidate Longhouse-managed Codex package when you decide to carry the update.

## What Already Exists In Zerg

- `scripts/release/build-managed-codex.sh`
  Builds the forked managed Codex binary from an upstream ref plus `managed-codex.patch`.
- `scripts/release/check-managed-codex-upstream.py`
  Compares the pinned upstream version/ref to upstream tags/releases, checks whether the carried patch still applies, and can emit an agent-ready prompt.
- `scripts/qa/repro-codex-remote-backpressure.sh`
  Existing live probe for the remote-TUI websocket/backpressure failure path.
- `.github/workflows/local-runtime-release.yml`
  Existing notarized/package/sign/release path. It now accepts optional workflow-dispatch overrides for:
  - `managed_codex_upstream_ref`
  - `managed_codex_upstream_version`
  - `managed_codex_build_version`

## Runner Recommendations

Use two classes of GitLab runners:

- `detect` runner:
  Plain unprivileged Docker executor is enough. This stage only needs Python, git, and outbound network access to GitHub.
- `advisory` / `probe` runner:
  Protected self-managed runner. This is where API keys, `hatch`, or a live Codex probe should run. Keep this off public/shared runners.

Do not require privileged Docker unless you actually need Docker-in-Docker. The managed-Codex build is a normal `git clone` plus `cargo build`; it does not need a privileged container.

## Important Constraint

Today the Longhouse installer resolves managed-Codex artifacts from the active Longhouse release tag in `server/zerg/services/runtime_artifacts.py`.

Implication:

- GitLab can automate detection, advisory, and candidate packaging now.
- A clean unattended rollout to all managed users still needs either:
  - a new Longhouse release tag, or
  - the separate managed-runtime manifest/update channel described in the session note.

Do not confuse "candidate build exists" with "all managed users will pick it up automatically."

## Suggested GitLab Pipeline Shape

```yaml
stages:
  - detect
  - advise
  - package

variables:
  GIT_STRATEGY: fetch

detect_managed_codex_update:
  stage: detect
  image: python:3.12-bookworm
  before_script:
    - apt-get update
    - apt-get install -y git
  script:
    - python3 scripts/release/check-managed-codex-upstream.py --json --write-agent-prompt artifacts/managed-codex-agent-prompt.txt > artifacts/managed-codex-report.json
    - python3 - <<'PY'
      import json
      from pathlib import Path
      report = json.loads(Path("artifacts/managed-codex-report.json").read_text())
      print(json.dumps({
          "update_needed_by_tag": report["update_needed_by_tag"],
          "patch_status": report["patch_status"],
          "candidate_tag": report["candidate_tag"]["name"],
          "candidate_version": report["candidate_tag"]["version"],
          "candidate_ref": report["candidate_tag"]["commit_sha"],
      }, indent=2))
      PY
  artifacts:
    when: always
    paths:
      - artifacts/managed-codex-report.json
      - artifacts/managed-codex-agent-prompt.txt
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
    - if: '$CI_PIPELINE_SOURCE == "web"'

advise_managed_codex_update:
  stage: advise
  tags: ["protected-llm"]
  needs: ["detect_managed_codex_update"]
  script:
    - test -x "$(command -v hatch)"
    - hatch codex mini "$(cat artifacts/managed-codex-agent-prompt.txt)" > artifacts/managed-codex-ai.md
  artifacts:
    when: always
    paths:
      - artifacts/managed-codex-ai.md
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'

package_managed_codex_candidate:
  stage: package
  tags: ["protected-release"]
  needs: ["detect_managed_codex_update"]
  script:
    - eval "$(python3 - <<'PY'
      import json
      from pathlib import Path
      report = json.loads(Path("artifacts/managed-codex-report.json").read_text())
      if not report["update_needed_by_tag"]:
          raise SystemExit("No new upstream tag.")
      candidate = report["candidate_tag"]
      print(f'export MANAGED_CODEX_UPSTREAM_REF="{candidate["commit_sha"]}"')
      print(f'export MANAGED_CODEX_UPSTREAM_VERSION="{candidate["version"]}"')
      print(f'export MANAGED_CODEX_BUILD_VERSION="{candidate["version"]}+longhouse.1"')
      PY
      )"
    - gh workflow run local-runtime-release.yml \
        --repo cipher982/longhouse \
        -f tag_name="$LONGHOUSE_RELEASE_TAG" \
        -f managed_codex_upstream_ref="$MANAGED_CODEX_UPSTREAM_REF" \
        -f managed_codex_upstream_version="$MANAGED_CODEX_UPSTREAM_VERSION" \
        -f managed_codex_build_version="$MANAGED_CODEX_BUILD_VERSION"
  rules:
    - if: '$CI_PIPELINE_SOURCE == "web"'
      when: manual
```

## Practical Decision Logic

- `patch_status == already_upstream_or_equivalent`
  Candidate likely contains your fix already. Run the live probe against stock upstream before keeping the fork patch.
- `patch_status == applies_cleanly`
  Upstream still looks patchable. Run the live probe; if the bug still reproduces, rebuild the fork and ship the patched binary.
- `patch_status == conflicts`
  Stop and inspect manually. The release changed the transport path enough that blind automation is not safe.

## Recommended First Cut

Keep the first version narrow:

1. Scheduled detect job.
2. Optional AI advisory job on a protected runner.
3. Manual package job that dispatches the existing GitHub release workflow with upstream override inputs.

That gets you the release-awareness loop now, without pretending the Longhouse runtime installer already has a separate managed-Codex update channel.
