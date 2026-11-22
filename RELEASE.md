# Releasing Longhouse

A release is how installed users and self-hosters get new code: installed CLIs,
the macOS app, and self-hosted Runtime Hosts only change when a `vX.Y.Z` release
ships. A green push to `main` does not do that. The Release workflow
(`.github/workflows/release.yml`) builds the runtime image, qualifies it on the
hosted canary and deploys personal dogfood by itself (no tag or release
needed). The `zerg-ship` skill has those commands and gates. The iOS app ships
separately to TestFlight with `make testflight`.

## Tags

| Tag | Workflow | Ships |
|---|---|---|
| `vX.Y.Z` | `publish.yml` and `local-runtime-release.yml`, on the GitHub release | PyPI wheel, `longhouse` and `longhouse-engine` binaries, signed macOS DMG |
| `runner-v*` | `runner-release.yml` | signed runner binaries and manifest |

`vX.Y.Z` is what ships to users. `runner-v*` is ops-only, cut when a runner release is wanted outside the
normal cadence.

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
4. **Gates.** `launch-gate.yml` is dispatched at the candidate SHA if GitHub's
   path filters skipped it. `scripts/ops/launch-readiness.py` then waits on that
   SHA for `CI`, `Release` and `Launch Gate`, plus a matching build SHA on the
   canary (`--skip-demo`: the public demo is not part of a release). `Release`
   starts when CI completes green; a candidate that changes no runtime input
   passes with its Plan job alone.
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
(`server/`, `web/`, `engine/`, `config/`, the runtime Dockerfile; the
`runtime_image` list in `.github/path-filters.yml`) once CI is green, and tagged
with the commit SHA. The Release workflow's archive job copies the image to the
immutable object store and moves `:latest` to it if it is still main's head.
Hosted tenants always get the digest-pinned image through a deployment, never
`:latest`.

The runner has its own release cadence and signing manifest, independent of
`vX.Y.Z` (`.github/workflows/runner-release.yml`).

## Rollback

- **PyPI:** wheels are immutable. Publish a new `vX.Y.Z+1` with the previous commit's content.
- **Desktop app:** replace the DMG on the old release, or cut a new release at the previous commit.
- **Hosted runtime (canary and dogfood):** there is no automatic rollback. A red
  Release leaves dogfood on the last good image (dogfood is deployed last, after
  the canary and, for sensitive ranges, QA). To go back on purpose, run
  `make rollback SHA=<previous full sha>`: it runs Release for that commit with
  `rollback=true`, reusing its image, through the canary and QA gate. Each
  image also has an immutable OCI copy in the object store
  (`scripts/ops/release-artifacts.py fetch`) if GHCR no longer has it. **Check
  schema first:** `scripts/ops/runtime-schema.py` and the image's
  `org.longhouse.catalog-schema.*` labels say what each image reads. Rolling
  back across a forward migration can leave a tenant on an image that cannot
  read its own data; david010 holds irreplaceable history, so do not roll back
  across a schema change without a backup and a decision from the owner.
- **Production (longhouse.ai):** dark, no deploy target. One is added to
  `release.yml` when it launches.
