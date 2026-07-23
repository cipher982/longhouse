# Releasing Longhouse

Cutting a release is how installed users and self-hosters get new code. Push to `main` only reaches hosted surfaces. If a fix needs to reach installed CLIs, the desktop app, or self-hosted runtime hosts, it must ship in a `vX.Y.Z` release.

## Tag types

Three tag families, each independent:

| Tag pattern  | Workflow triggered                             | What it ships                                                        |
|--------------|------------------------------------------------|----------------------------------------------------------------------|
| `vX.Y.Z`     | `publish.yml` + `local-runtime-release.yml`    | PyPI wheel, engine binaries, managed Codex binaries, signed macOS DMG |
| `runtime-v*` | `runtime-image.yml`                            | `ghcr.io/cipher982/longhouse-runtime:runtime-v*` image                |
| `runner-v*`  | `runner-release.yml`                           | Signed runner binaries + manifest                                     |

`vX.Y.Z` is what ships to users. The other two are ops-only and usually only touched when those specific components need a pinned release.

## Cutting a `vX.Y.Z` release

1. Bump `version` in `server/pyproject.toml` to match the target tag (e.g. `0.1.13`).
2. Commit and push to `main`. Let CI go green.
3. Draft the GitHub release from the latest main SHA with tag `vX.Y.Z`. A short body is fine — point to the commit log or call out notable changes.
4. Publish the release. This fires two workflows automatically:
   - `Publish to PyPI` — builds the wheel, uploads it as a release asset, pushes to PyPI.
   - `Local Runtime Binary Release` — builds engine + managed Codex for Linux/macOS, signs/notarizes the macOS DMG, uploads all artifacts to the release.
5. Wait for both workflows to finish (macOS notarization can take up to ~330 minutes in the worst case but typically finishes in minutes).
6. Verify the release landed:
   ```bash
   gh release view vX.Y.Z --json assets --jq '.assets[].name'
   ```
   Expected: wheel, engine binaries, codex binaries, DMG, zip, checksums, packaging manifest.
7. Verify notarization:
   ```bash
   gh release download vX.Y.Z --pattern local-runtime-macos-packaging.json --output -
   ```
   `notarization_status` and `public_download_notarization_status` must both be `notarized` for a stable tag.

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
