# Releasing Longhouse

Cutting a release is how installed users and self-hosters get new code. A push to `main` only reaches the hosted canary; the dogfood instance and production move on their own by `make promote-dogfood` and `make promote-production`, which need no tag and no release (the `zerg-ship` skill has the commands and gates). If a fix needs to reach installed CLIs, the desktop app, or self-hosted runtime hosts, it must ship in a `vX.Y.Z` release. The iOS app ships separately, to TestFlight (`make testflight`).

## Tag types

Three tag families, each independent:

| Tag pattern  | Workflow triggered                             | What it ships                                                        |
|--------------|------------------------------------------------|----------------------------------------------------------------------|
| `vX.Y.Z`     | `publish.yml` + `local-runtime-release.yml`    | PyPI wheel, `longhouse` + `longhouse-engine` binaries, signed macOS DMG |
| `runtime-v*` | `runtime-image.yml`                            | `ghcr.io/cipher982/longhouse-runtime:runtime-v*` image                |
| `runner-v*`  | `runner-release.yml`                           | Signed runner binaries + manifest                                     |

`vX.Y.Z` is what ships to users. The other two are ops-only and usually only touched when those specific components need a pinned release.

## Cutting a `vX.Y.Z` release

```bash
make release VERSION=vX.Y.Z
```

That is the whole procedure. `scripts/ops/release.sh` runs end to end and fails the release rather than leaving you to notice a gap afterwards, so there are no manual verification steps to follow it. With green gates it takes well under an hour: validation about 7 minutes, the exact-SHA gates, then the two release workflows (about 10 minutes, notarization included). The waits are bounded: two hours for the gates, six for the release workflows.

Run it from any checkout, clean or not, on any branch. It first takes the `release` ring lock (`scripts/ops/ring_lock.py`: a second release is refused and told who holds it; the lock frees itself if the holder dies), then makes a disposable worktree of the exact `origin/main` commit under `/tmp/agents` and runs every step below from there, removing it on exit whether the release succeeds or not. So a release never sweeps another agent's unpushed work into the tag, and no checkout is tied up while it runs. The one exception to starting from `origin/main` is a resume: a bump commit an earlier run made but did not push is kept at `refs/longhouse/release-candidates/<version>`, and a rerun starts from it while it still sits on top of `origin/main`. It also refuses when the tag already exists locally or on `origin`.

Then, in order:

1. `bump-my-version` sets every manifest in `.bumpversion.toml` to the shared release version — `server/pyproject.toml`, `engine/Cargo.toml`, `runner/package.json`, `ios/XcodeHarness/Configs/Version.xcconfig` — and the lockfiles are refreshed. This is the release version, not the per-commit build identity, which advances on every commit. If the manifests already sit at that version the script reuses the existing candidate, so a failed release is safe to rerun with the same `VERSION`; validation is skipped when that exact commit already passed it (see step 2).
2. The bumped manifests are committed as `Bump version to X.Y.Z`, and `make test-ci` runs against that exact commit under the machine-wide heavy-build lock (`/tmp/agents/longhouse-heavy-build.lock`; only this step holds it, and an outer `lockf` wrapper is detected, so do not wrap `make release`). It runs in the isolated guest at 8 CPU / 8 GiB (`LONGHOUSE_TEST_CPUS` / `LONGHOUSE_TEST_MEMORY`) with the engine built in the `ci-test` profile, about 7 minutes. If validation rewrites a tracked file, the script stops and asks you to commit the generated updates and rerun. A candidate that passed with a clean tree is stamped under `release-validated/<sha>` in the clone's git common dir, and a resume of the same commit skips the validation; `RELEASE_REVALIDATE=1` forces it, and any new commit revalidates. The version-only bump commit does not run the iOS lane or rebuild the fixture image in CI.
3. The review gate (`scripts/ops/review_gate.py push`) checks the landing rule for any commit ahead of `origin/main`; the version bump itself is exempt. The candidate is then pushed straight to `main` by SHA (nothing is pushed when it is already on `origin/main`). If the push loses a race and the bump is the only local commit, the script replays it onto the new `origin/main` and pushes again; otherwise it stops and you rerun.
4. Any exact-SHA gate workflow GitHub's path filters skipped (`runtime-image.yml`, `deploy-and-verify.yml`, `launch-gate.yml`) is dispatched at the candidate SHA. `scripts/ops/launch-readiness.py` then waits on that SHA for `CI`, `Deploy and Verify`, and `Launch Gate`, plus a matching build SHA on the canary (`--skip-demo`: the public demo moves on the production ring, not with a release). `Deploy and Verify` dispatches Hosted Live QA asynchronously; its verdict gates production promotion, not the release. Timeout is two hours.
5. `gh release create` cuts the release against the candidate SHA with a changelog link to the previous tag. That fires `publish.yml` (wheel to PyPI and to the release) and `local-runtime-release.yml` (engine and facade binaries for macOS/Linux, signed and notarized DMG).
6. The script waits up to six hours for both workflows. Release-event runs sometimes appear ~20 minutes late, so it only falls back to dispatching a workflow itself after 30 minutes of silence (`DISPATCH_GRACE_SECONDS`) — dispatching earlier produces a duplicate run that collides with the real one on asset upload.
7. It then verifies the release carries `longhouse-<version>-py3-none-any.whl`, `longhouse-engine-darwin-arm64`, `longhouse-engine-linux-x64`, `Longhouse-macos-arm64.dmg`, and `local-runtime-macos-packaging.json`; that both `notarization_status` and `public_download_notarization_status` in that manifest read `notarized`; and finally re-runs launch readiness (still `--skip-demo`) with the release, package, and runtime-artifact checks enabled.

The release is shipped when the script prints `Release vX.Y.Z shipped and verified.` Anything short of that is a failed release, not a partial one.

## Verify install

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse verify-pair
longhouse local-health --json
```

For the desktop app, download the DMG from the release and drag-install.

## Signing and notarization

Stable releases (tags matching `^v[0-9]+\.[0-9]+\.[0-9]+$`) **require** signing and notarization. Non-matching tags get adhoc signing and no notarization (smoke/test only).

Required GitHub secrets (already set):
- `MACOS_SIGNING_CERT_P12_BASE64`, `MACOS_SIGNING_CERT_PASSWORD`, `MACOS_SIGNING_IDENTITY`
- `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_APP_PASSWORD`, `MACOS_NOTARY_TEAM_ID`

If any of these are missing, a stable-tier release will fail fast with a clear error at the signing step. Do not fall back to adhoc for a stable tag.

## Runtime image (`runtime-v*`)

The runtime image is built on a `main` push that touches runtime paths (`server/`, `web/`, `engine/`, `config/`, the runtime Dockerfile; see `.github/workflows/runtime-image.yml`), tagged with the commit SHA (Archive Runtime Image then moves `:latest` to it if it is still main's head), and separately on `runtime-v*` tags (adds the semantic tag). Hosted tenants always receive the digest-pinned image through a deployment, never `:latest`.

You normally do not cut `runtime-v*` tags. Cut one only when you want a pinned runtime image outside the normal main push cadence.

## Runner (`runner-v*`)

The runner has its own release cadence and signing manifest. See `.github/workflows/runner-release.yml`. Independent of `vX.Y.Z`.

## Rollback

- PyPI: `longhouse` wheels are immutable. To roll back, publish a new `vX.Y.Z+1` with the previous commit's content.
- Desktop app: replace the DMG on the old release or cut a new release pointing at the previous commit.
- Runtime image, canary: re-deploy the previous SHA via `workflow_dispatch` on `deploy-and-verify.yml` with `runtime_image_tag` set to the good SHA. Production: a halted `make promote-production` prints its own recovery commands (control-plane rollback, or rerun with `PROMOTION_ATTEMPT=<n+1>`).
