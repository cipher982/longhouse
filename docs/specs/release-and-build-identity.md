# Release and Build Identity

**Status:** draft
**Owner:** David
**Started:** 2026-04-21

## Why

Today we can't answer "what commit am I running?" anywhere. `longhouse --version` says `0.1.15-local`. The app bundle says `0.1.15-local`. `/api/health` has no version field. Two different commits present themselves identically after `make dogfood-refresh`.

Separately, we have four independent semvers that drift (`server 0.1.15`, `engine 0.1.0`, `runner 0.1.7`, `control-plane 0.1.0`, `ios 0.1.0 / 1`), so even the release number can't stand in for "product version."

This spec fixes both.

## Model

Every build now has two orthogonal identities:

1. **Release version** — a single semver that advances in lockstep across the whole repo (server, engine, runner, control-plane, ios). Only moves when someone runs `./dev release vX.Y.Z`. This is the user-facing "Longhouse version."
2. **Build identity** — a per-build record: `{version, commit, commit_short, dirty, built_at, channel}`. Stamped at build time, embedded in every artifact. This is the "which commit specifically."

Release version without build identity is a lie for any non-tagged build. Build identity without release version is hard for users to compare. We want both, everywhere.

## Canonical file

One JSON file, one source of truth, every component reads it:

```json
{
  "version": "0.2.0",
  "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
  "commit_short": "b672fcca",
  "dirty": false,
  "built_at": "2026-04-21T18:03:12Z",
  "channel": "release"
}
```

- `version` — release semver from the bumped manifests.
- `commit` / `commit_short` — `GITHUB_SHA` in CI, `git rev-parse HEAD` locally.
- `dirty` — true if the working tree has uncommitted changes when the build ran.
- `built_at` — UTC ISO 8601.
- `channel` — `release` for tagged builds (CI on tag), `dev` for everything else (local, push-to-main hosted builds that aren't tagged).

Generator lives at `scripts/build/generate_build_identity.py`. Outputs to `.build/build-identity.json` (gitignored). Every build step calls this first.

### Display format

- Plain release build: `0.2.0 (b672fcca)`
- Dev build from clean tree: `0.2.0-dev+b672fcca`
- Dev build with local edits: `0.2.0-dev+b672fcca.dirty`

Never show bare semver. If a surface only has room for one string, show the dev/dirty-qualified form — matching plain semver with a non-release build was the bug that started this work.

## Lockstep versioning

One release number advances all four component manifests plus iOS in one shot.

**Tool:** `bump-my-version` (maintained successor to `bump2version`). Config in `.bumpversion.toml` at repo root. Files it edits:

- `server/pyproject.toml`
- `engine/Cargo.toml`
- `runner/package.json`
- `control-plane/pyproject.toml`
- `ios/XcodeHarness/Configs/Version.xcconfig` — **new**; `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` move out of `project.yml` `settings.base` into this xcconfig so a key=value editor can touch them cleanly. `project.yml` gets `Version.xcconfig` added to `configFiles` so XcodeGen picks it up.

`./dev release vX.Y.Z` (replaces `make release VERSION=vX.Y.Z`):
1. Preflight: clean tree, on main, local == origin/main, tag doesn't exist.
2. Run `bump-my-version bump --new-version X.Y.Z`. This edits all five files, commits, tags `vX.Y.Z`.
3. Push the bump commit and tag.
4. Create GitHub release, wait for `publish.yml` + `local-runtime-release.yml`, verify assets + notarization.

Steps 3–4 reuse the existing `scripts/ops/release.sh` machinery; we only replace step 2.

### Why lockstep

Split semvers exist to serve independent release cadence or external consumers. Longhouse has neither. Engine, runner, control-plane, and iOS ship from one monorepo with one dev. Lockstep is the less-complex option — one number to reason about, one release to cut. iOS "wastes" a rebuild when iOS code didn't change; that's ~30s of Xcode for product coherence.

If we ever hire someone who owns `runner` as a product with its own cadence, split then. Unsplitting a drifted repo is the hard direction.

## Runtime surfaces

Every surface that shows a version reads `build-identity.json`. Never re-infer, never hardcode, never compile a semver literal.

| Surface | Where it reads from | What it shows |
|---|---|---|
| `longhouse --version` | wheel resource, else `~/.longhouse/build-identity.json` | `longhouse 0.2.0 (b672fcca)` |
| `longhouse --version --json` | same | full JSON |
| `/api/health` | wheel resource | adds `build: {...}` field |
| `~/.claude/engine-status.json` | file next to engine binary, else `~/.longhouse/build-identity.json` | new `build` field |
| Menu bar footer | reads engine-status.json | `0.2.0 (b672fcca)` |
| iOS About screen | bundled `build-identity.json` resource | `0.2.0 (b672fcca)` |
| Docker image | `/app/build-identity.json` + OCI `org.opencontainers.image.revision` label | same file + registry metadata |

We collapse `/api/version` and `/api/system/info` version-adjacent fields into `/api/health`. One endpoint.

### Mismatch detection

`longhouse connect --status` and the menu bar both compare CLI / engine / app short SHAs. If they diverge, the surface shows `build drift` and names which components disagree. This is the operator-facing answer to "something's wrong after an update."

## Build-time wiring

Per research, the primary mechanism is **runtime file lookup**, not compile-time embedding. Compile-time embedding in Rust/Swift causes build-script cascades and fights caching. File-first keeps builds fast.

### Python (server, control-plane)

- `generate_build_identity.py` runs before `uv build`.
- Hatch's `[tool.hatch.build.targets.wheel.force-include]` bundles `.build/build-identity.json` as package data at `zerg/build-identity.json`.
- Runtime reader: `zerg.build_info.load()` checks the bundled resource first; falls back to `~/.longhouse/build-identity.json` for editable/dogfood installs; falls back to a stub `{version: "0.0.0-dev+unknown", ...}` if both miss.

### Rust (engine)

- No `build.rs` changes required for primary path.
- Engine reads `build-identity.json` from: file next to binary, else `~/.longhouse/build-identity.json`, else `../../.build/build-identity.json` (dev convenience).
- `longhouse-engine --version` prints the qualified string.
- On startup, engine copies the loaded identity into `~/.claude/engine-status.json` under a `build` key.

### Swift (iOS)

- XcodeGen run-script phase: copies `.build/build-identity.json` into `Resources/build-identity.json` during build. Declare `inputFiles` / `outputFiles` so Xcode caches correctly.
- Swift reader: `Bundle.main.url(forResource: "build-identity", withExtension: "json")`. About screen displays the qualified string.

### Docker (hosted runtime image)

- `docker/runtime.dockerfile` last stage: `COPY .build/build-identity.json /app/build-identity.json` as final layer (preserves cache).
- Workflow sets `org.opencontainers.image.revision=${GITHUB_SHA}` label.

## Dev loop

`./dev refresh` (new; replaces `make dogfood-refresh` over time) runs:
1. `scripts/build/generate_build_identity.py` → writes `.build/build-identity.json` with `channel=dev`.
2. Copies a sibling to `~/.longhouse/build-identity.json` so installed-but-not-rebuilt binaries can still find it.
3. Then the existing dogfood runtime rebuild.

Net: every local build has a traceable identity. Two different commits never both present as `0.2.0`.

## Entrypoint consolidation

`./dev` at repo root, Python + typer. One front door for the common verbs:

| Command | Calls |
|---|---|
| `./dev up` | existing `scripts/dev.sh` |
| `./dev down` | existing kill logic |
| `./dev test [tier]` | routes to the right test tier |
| `./dev ship` | existing `scripts/ops/ship.sh` |
| `./dev release vX.Y.Z` | new bump-my-version flow + existing release.sh tail |
| `./dev refresh` | existing dogfood + build-identity write |
| `./dev doctor` | new: tool versions, env vars, build-identity drift check |
| `./dev version` | print current repo build identity |

Makefile keeps existing targets as thin forwarders (`make dev` → `./dev up`) so muscle memory survives. No new Make targets added after this.

Scripts in `scripts/` are implementation detail called by `./dev`; they stop being first-class. We don't delete them wholesale — we stop growing the top-level surface.

## Out of scope for v1

- Delta / differential updates for the CLI. PyPI `uv tool upgrade` stays the upgrade path.
- Auto-rollback on build drift. Menu bar shows the warning; user acts.
- Per-commit build identity for hosted runtime beyond SHA. If someone wants "which push landed on demo at 3:14pm" that's in `/api/health` already.
- Signing/attestation of `build-identity.json`. Trust model is "same repo, same developer" for now.

## What we delete when this lands

- Hardcoded `"0.1.15-local"` fallback strings wherever they appear.
- `/api/version` and version-duplicating fields on `/api/system/info` (collapsed into `/api/health`).
- Any Makefile target that was only a one-liner around a script — subsumed by `./dev`.
- The `0.1.0` placeholder versions in `engine/Cargo.toml`, `runner/package.json`, `control-plane/pyproject.toml`, iOS xcconfig — all become lockstep.
- `release.sh`'s custom Python in-line semver rewriter (replaced by `bump-my-version`).

## Phases

1. **P0** — this spec + docket item (done when committed).
2. **P1** — `generate_build_identity.py` + tests.
3. **P2** — Python CLI / `/api/health` expose build identity; runtime reader with both bundled and dogfood paths.
4. **P3** — Engine stamps build identity into `engine-status.json`; menu bar reads and displays it; mismatch detection.
5. **P4** — iOS xcconfig + XcodeGen build phase + Swift reader + About screen.
6. **P5** — `.bumpversion.toml` + lockstep wrapper replacing `release.sh` step 1.
7. **P6** — `./dev` Python entrypoint.
8. **P7** — `dogfood-refresh` writes `~/.longhouse/build-identity.json`.
9. **P8** — cleanup + AGENTS.md learning.

After each phase I'll commit and have a Codex hatch review the change before moving on, per the pattern from the Stage 4/5 runtime-truth work.
