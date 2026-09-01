# Releasing Longhouse

Cutting a release is how installed users and self-hosters get new code. Push to `main` only reaches hosted surfaces. If a fix needs to reach installed CLIs, the desktop app, or self-hosted runtime hosts, it must ship in a `vX.Y.Z` release.

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

That is the whole procedure. `scripts/ops/release.sh` runs end to end and fails the release rather than leaving you to notice a gap afterwards, so there are no manual verification steps to follow it. Expect it to take hours, mostly waiting on workflows.

Before it changes anything it refuses to proceed unless the working tree is clean, you are on `main`, local `main` matches `origin/main`, and the tag does not already exist locally or on `origin`. The `origin/main` check is a shared-checkout guard: it stops the release from sweeping another agent's unpushed work into the tag.

Then, in order:

1. `bump-my-version` sets every manifest in `.bumpversion.toml` to the shared release version — `server/pyproject.toml`, `engine/Cargo.toml`, `runner/package.json`, `ios/XcodeHarness/Configs/Version.xcconfig` — and the lockfiles are refreshed. This is the release version, not the per-commit build identity, which advances on every commit. If the manifests already sit at that version the script reuses the existing candidate and revalidates it, so a failed release is safe to rerun with the same `VERSION`.
2. The bumped manifests are committed as `Bump version to X.Y.Z`, and `make test-ci` runs against that exact commit. If validation rewrites a tracked file, the script stops and asks you to commit the generated updates and rerun.
3. The candidate is pushed straight to `main` by SHA. A push failure means someone else landed first — reconcile and rerun.
4. `scripts/ops/launch-readiness.py` waits on that SHA for `CI`, `Deploy and Verify` (which carries hosted QA), and `Launch Gate`, plus matching build SHAs on the live demo and canary surfaces. Timeout is two hours.
5. `gh release create` cuts the release against the candidate SHA with a changelog link to the previous tag. That fires `publish.yml` (wheel to PyPI and to the release) and `local-runtime-release.yml` (engine and facade binaries for macOS/Linux, signed and notarized DMG).
6. The script waits up to six hours for both workflows. Release-event runs sometimes appear ~20 minutes late, so it only falls back to dispatching a workflow itself after 30 minutes of silence (`DISPATCH_GRACE_SECONDS`) — dispatching earlier produces a duplicate run that collides with the real one on asset upload.
7. It then verifies the release carries `longhouse-<version>-py3-none-any.whl`, `longhouse-engine-darwin-arm64`, `longhouse-engine-linux-x64`, `Longhouse-macos-arm64.dmg`, and `local-runtime-macos-packaging.json`; that both `notarization_status` and `public_download_notarization_status` in that manifest read `notarized`; and finally re-runs launch readiness with the release, package, and runtime-artifact checks enabled.

The release is shipped when the script prints `Release vX.Y.Z shipped and verified.` Anything short of that is a failed release, not a partial one.

## Verify install

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse verify-pair
longhouse local-health --fast --json
```

For the desktop app, download the DMG from the release and drag-install.

## Signing and notarization

Stable releases (tags matching `^v[0-9]+\.[0-9]+\.[0-9]+$`) **require** signing and notarization. Non-matching tags get adhoc signing and no notarization (smoke/test only).

Required GitHub secrets (already set):
- `MACOS_SIGNING_CERT_P12_BASE64`, `MACOS_SIGNING_CERT_PASSWORD`, `MACOS_SIGNING_IDENTITY`
- `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_APP_PASSWORD`, `MACOS_NOTARY_TEAM_ID`

If any of these are missing, a stable-tier release will fail fast with a clear error at the signing step. Do not fall back to adhoc for a stable tag.

## Runtime image (`runtime-v*`)

The runtime image is built on every main push (tagged with the commit SHA + `:latest`) and separately on `runtime-v*` tags (adds the semantic tag). Hosted tenants always receive the SHA-pinned image through the deploy pipeline; the `:latest` tag only exists as a safety fallback for workflow-only pushes.

You normally do not cut `runtime-v*` tags. Cut one only when you want a pinned runtime image outside the normal main push cadence.

## Runner (`runner-v*`)

The runner has its own release cadence and signing manifest. See `.github/workflows/runner-release.yml`. Independent of `vX.Y.Z`.

## Rollback

- PyPI: `longhouse` wheels are immutable. To roll back, publish a new `vX.Y.Z+1` with the previous commit's content.
- Desktop app: replace the DMG on the old release or cut a new release pointing at the previous commit.
- Runtime image: re-deploy the previous SHA via `workflow_dispatch` on `deploy-and-verify.yml` with `runtime_image_tag` set to the good SHA.
