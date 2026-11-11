# Releasing Longhouse

A release is how installed users and self-hosters get new code: installed CLIs,
the macOS app, and self-hosted Runtime Hosts only change when a `vX.Y.Z` release
ships. A push to `main` does not do that. It builds the runtime image and
qualifies the hosted canary, and the dogfood and production rings then follow
by themselves (Promote Rings, `.github/workflows/promote-rings.yml`; no tag or
release needed). The `zerg-ship` skill has those commands and gates. The iOS app
ships separately to TestFlight with `make testflight`.

## Tags

| Tag | Workflow | Ships |
|---|---|---|
| `vX.Y.Z` | `publish.yml` and `local-runtime-release.yml`, on the GitHub release | PyPI wheel, `longhouse` and `longhouse-engine` binaries, signed macOS DMG |
| `runtime-v*` | `runtime-image.yml` | `ghcr.io/cipher982/longhouse-runtime:runtime-v*` |
| `runner-v*` | `runner-release.yml` | signed runner binaries and manifest |

`vX.Y.Z` is what ships to users. The other two are ops-only, cut when a pinned
runtime image or runner release is wanted outside the normal cadence.

## Cutting a `vX.Y.Z` release

```bash
make release VERSION=vX.Y.Z
```

That is the whole procedure. `scripts/ops/release.sh` runs end to end and fails
the release instead of leaving a gap to find later, so there are no manual
verification steps after it. It is shipped when the script prints
`Release vX.Y.Z shipped and verified.`; anything short of that is a failed
release, not a partial one.

Run it from any checkout, on any branch, clean or not. It takes the `release`
ring lock (`scripts/ops/ring_lock.py`; a second release is refused and told who
holds it, and the lock frees itself if the holder dies). It then makes a
disposable worktree of the exact `origin/main` commit under `/tmp/agents`, runs
every step there, and removes it on exit. A release therefore never sweeps
another checkout's unpushed work into the tag.

Two cases start elsewhere. A resume starts from the bump commit an earlier run
made but did not push, kept at `refs/longhouse/release-candidates/<version>` while
it still sits on top of `origin/main`. And the script refuses when the tag
already exists locally or on `origin`.

Steps, in order:

1. **Bump.** `bump-my-version` sets the shared release version in every manifest
   listed in `.bumpversion.toml` (`server/pyproject.toml`, `engine/Cargo.toml`,
   `runner/package.json`, `ios/XcodeHarness/Configs/Version.xcconfig`) and
   refreshes lockfiles. This is the release version, not the per-commit build
   identity. If the manifests already sit at that version the script reuses the
   candidate, so a failed release is safe to rerun with the same `VERSION`.
2. **Validate.** The bump is committed as `Bump version to X.Y.Z` and
   `make test-ci` runs on that exact commit under the machine-wide heavy-build
   lock (`/tmp/agents/longhouse-heavy-build.lock`; do not wrap `make release` in
   your own `lockf`). It runs in the isolated guest at up to 8 CPU and 8 GiB
   (`LONGHOUSE_TEST_CPUS`, `LONGHOUSE_TEST_MEMORY`) with the engine built in the
   `ci-test` profile. If validation rewrites a tracked file, the script stops;
   commit the generated updates and rerun. A candidate that passed with a clean
   tree is stamped under `release-validated/<sha>` in the git common dir, and a
   resume of the same commit skips validation. `RELEASE_REVALIDATE=1` forces it
   and any new commit revalidates. The version-only bump skips the iOS lane and
   the fixture-image rebuild in CI.
3. **Push.** The candidate goes to `main` by exact SHA (nothing is pushed if it
   is already there). If the push loses a race and the bump is the only local
   commit, the script replays it onto the new `origin/main` and pushes again.
   Otherwise it stops and you rerun.
4. **Gates.** Any exact-SHA gate workflow that GitHub's path filters skipped
   (`runtime-image.yml`, `deploy-and-verify.yml`, `launch-gate.yml`) is
   dispatched at the candidate SHA. `scripts/ops/launch-readiness.py` then waits
   on that SHA for `CI`, `Deploy and Verify` and `Launch Gate`, plus a matching
   build SHA on the canary (`--skip-demo`: the public demo is not part of a
   release). `Deploy and Verify` dispatches Hosted Live QA asynchronously; its
   verdict gates production promotion, not the release.
5. **Release.** `gh release create` cuts the release against the candidate SHA,
   with a changelog link to the previous tag. That fires `publish.yml` (wheel to
   PyPI and the release) and `local-runtime-release.yml` (engine and facade
   binaries for macOS and Linux, signed and notarized DMG).
6. **Wait for the workflows.** Release-event runs can appear late, so the script
   dispatches a workflow itself only after `DISPATCH_GRACE_SECONDS` (default 30
   minutes) of silence. Dispatching earlier makes a duplicate run that collides
   with the real one on asset upload.
7. **Verify.** The release must carry `longhouse-<version>-py3-none-any.whl`,
   `longhouse-engine-darwin-arm64`, `longhouse-engine-linux-x64`,
   `Longhouse-macos-arm64.dmg` and `local-runtime-macos-packaging.json`. In that
   manifest `notarization_status` and `public_download_notarization_status` must
   both read `notarized`. Launch readiness then runs again (still `--skip-demo`)
   with the release, package and runtime-artifact checks on.

## Verify an install

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse verify-pair
longhouse local-health --json
```

For the desktop app, download the DMG from the release and drag-install.

## Signing and notarization

Stable tags (`^v[0-9]+\.[0-9]+\.[0-9]+$`) require signing and notarization.
Other tags get ad-hoc signing and no notarization (smoke tests only). A stable
tag never falls back to ad-hoc: if a required GitHub secret is missing the
signing step fails fast. The secrets are `MACOS_SIGNING_CERT_P12_BASE64`,
`MACOS_SIGNING_CERT_PASSWORD`, `MACOS_SIGNING_IDENTITY`, `MACOS_NOTARY_APPLE_ID`,
`MACOS_NOTARY_APP_PASSWORD` and `MACOS_NOTARY_TEAM_ID`.

## Runtime image and runner

The runtime image is built on a `main` push that touches runtime paths
(`server/`, `web/`, `engine/`, `config/`, the runtime Dockerfile; see
`.github/workflows/runtime-image.yml`) and tagged with the commit SHA. Archive
Runtime Image then moves `:latest` to it if it is still main's head. A
`runtime-v*` tag adds the semantic tag. Hosted tenants always get the
digest-pinned image through a deployment, never `:latest`.

The runner has its own release cadence and signing manifest, independent of
`vX.Y.Z` (`.github/workflows/runner-release.yml`).

## Rollback

- **PyPI:** wheels are immutable. Publish a new `vX.Y.Z+1` with the previous commit's content.
- **Desktop app:** replace the DMG on the old release, or cut a new release at the previous commit.
- **Canary runtime image:** re-deploy the previous SHA with `workflow_dispatch` on `deploy-and-verify.yml`, setting `runtime_image_tag` to the good SHA.
- **Production:** a halted `make promote-production` prints its own recovery commands (control-plane rollback, or a rerun with `PROMOTION_ATTEMPT=<n+1>`).
