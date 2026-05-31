# Release and Build Identity

**Status:** draft
**Owner:** maintainer
**Started:** 2026-04-21

## Why

Today we can't answer "what commit am I running?" anywhere. `longhouse --version` says `0.1.15-local`. The app bundle says `0.1.15-local`. `/api/health` has no version field. Two different commits present themselves identically after `make dogfood-refresh`.

Separately, public component semvers have drifted (`server 0.1.15`, `engine 0.1.0`, `runner 0.1.7`, `ios 0.1.0 / 1`), so even the release number can't stand in for "product version."

This spec fixes both.

## Model

Every build now has two orthogonal identities:

1. **Release version** — a single shared semver that advances across the public repo (server, engine, runner, ios) in one shot. Only moves when someone runs `make release VERSION=vX.Y.Z`. This is the user-facing "Longhouse version."
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
- `dirty` — true iff tracked files differ from `HEAD` (`git diff --quiet HEAD`). Untracked files are ignored — shared-worktree reality means other agents routinely have WIP in the same directory and that noise is not our provenance.
- `built_at` — UTC ISO 8601.
- `channel` — `release` for tagged builds (CI on tag), `dev` for everything else (local, push-to-main hosted builds that aren't tagged).

Generator lives at `scripts/build/generate_build_identity.py`. Outputs to `.build/build-identity.json` (gitignored). Every build step calls this first.

### Display format

- Plain release build: `0.2.0 (b672fcca)`
- Dev build from clean tree: `0.2.0-dev+b672fcca`
- Dev build with local edits: `0.2.0-dev+b672fcca.dirty`

Never show bare semver. If a surface only has room for one string, show the dev/dirty-qualified form — matching plain semver with a non-release build was the bug that started this work.

## Shared release versioning

One release number advances all public component manifests in one shot. This is the release version, not build identity — release version moves only on explicit release ceremonies; build identity advances every commit.

**Tool:** `bump-my-version` (maintained successor to `bump2version`). Config in `.bumpversion.toml` at repo root. Files it edits:

- `server/pyproject.toml`
- `engine/Cargo.toml`
- `runner/package.json`
- `ios/XcodeHarness/Configs/Version.xcconfig` — **new**; `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` move out of `project.yml` `settings.base` into this xcconfig so a key=value editor can touch them cleanly. `project.yml` gets `Version.xcconfig` added to `configFiles` so XcodeGen picks it up.

`make release VERSION=vX.Y.Z`:
1. Preflight: clean tree, on main, local == origin/main, tag doesn't exist.
2. Run `bump-my-version bump --new-version X.Y.Z`. This edits the public version manifests.
3. Commit the bump, push to main, create GitHub release.
4. Wait for `publish.yml` + `local-runtime-release.yml`, verify assets + notarization.

### Why share one number

Split semvers exist to serve independent release cadence or external consumers. Longhouse core has neither. Engine, runner, and iOS ship from one public repo with one release ceremony. One shared number is the less-complex option — one number to reason about, one release to cut. iOS "wastes" a rebuild when iOS code didn't change; that's ~30s of Xcode for product coherence.

If we ever hire someone who owns `runner` as a product with its own cadence, split then. Unsplitting a drifted repo is the hard direction.

## Runtime surfaces

Every surface that shows a version reads `build-identity.json`. Never re-infer, never hardcode, never compile a semver literal.

| Surface | Where it reads from | What it shows |
|---|---|---|
| `longhouse --version` | bundled wheel resource (only) | `longhouse 0.2.0 (b672fcca)` |
| `longhouse --version --json` | same | full JSON |
| `/api/health` | bundled wheel resource | adds `build: {...}` field |
| `~/.claude/engine-status.json` | written by engine from its compiled-in identity | new `build` field |
| Menu bar footer | reads engine-status.json | `0.2.0 (b672fcca)` |
| iOS About screen | bundled `build-identity.json` resource | `0.2.0 (b672fcca)` |
| Docker image | `/app/build-identity.json` + OCI `org.opencontainers.image.revision` label | same file + registry metadata |

We collapse `/api/version` and `/api/system/info` version-adjacent fields into `/api/health`. One endpoint.

### Mismatch detection

`longhouse connect --status` and the menu bar compare the installed Longhouse build against the running engine daemon. If the daemon is still on the old binary after `make install-engine` or `make install-cli`, the surface shows `engine restart pending` instead of a scary broken-state warning. This is the operator-facing answer to "what still needs a restart after an update?"

## Build-time wiring

Every artifact carries its identity **inside itself**. No home-directory or other shared mutable fallback — that would let two different binaries report the same SHA because they read the same external file. If a build surface can't find its bundled identity, that's a build bug, not a runtime case to paper over — fail loudly.

### Python (server)

- `generate_build_identity.py` runs before any `uv build`. It writes `.build/build-identity.json` and simultaneously stages the file into the Python package tree (e.g. `server/zerg/build_identity.json`).
- Each package reads its own copy via `importlib.resources` — no cross-package imports.
- Runtime reader: `zerg.build_info.load()` reads the staged resource. Missing resource → raise `BuildIdentityMissing`; the CLI surfaces that as "build identity missing — rebuild."
- No editable-install fallback. `make dogfood-refresh` builds a wheel and installs it, not `uv pip install -e`. Wheel build adds ~25s per refresh; correctness is worth it.
- **`make dev` (source-run backend):** `scripts/dev.sh` runs `generate_build_identity.py` first, which stages the resource into `server/zerg/build_identity.json`. Source runs read via `importlib.resources` like installed wheels — one read path, no env-var fallback. If the resource is missing → `BuildIdentityMissing`. Always explicit, never inferred.

### Rust (engine)

- Minimal `build.rs` at `engine/build.rs`: reads `../.build/build-identity.json`, parses JSON, emits `cargo::rustc-env=LONGHOUSE_BUILD_{VERSION,COMMIT,COMMIT_SHORT,DIRTY,BUILT_AT,CHANNEL}`.
- Declares `cargo::rerun-if-changed=../.build/build-identity.json` to keep rebuilds surgical. Cargo only re-runs the build script when the identity file changes.
- Freshness guard: when `git` is available in the build environment, `build.rs` compares the staged identity's `commit` field to `git rev-parse HEAD` and fails the build on mismatch. Catches the case where cargo runs before `generate_build_identity.py` regenerates the file.
- Engine source: `const BUILD: &str = env!("LONGHOUSE_BUILD_COMMIT_SHORT");` etc., aggregated into a `BuildIdentity` struct. Missing env vars → compile error (build-identity.json absent ⇒ build fails).
- `longhouse-engine --version` prints the qualified string.
- On startup, engine writes its identity into `~/.claude/engine-status.json` under a `build` key. Menu bar reads from there.

### Swift (iOS)

- XcodeGen run-script phase in `ios/XcodeHarness/project.yml`: copies `.build/build-identity.json` into bundled `Resources/build-identity.json`. Declare `inputFiles` / `outputFiles` so Xcode caches it correctly.
- Swift reader: `Bundle.main.url(forResource: "build-identity", withExtension: "json")`. About screen displays the qualified string. Missing → the About screen shows "build identity missing," not a fake version.

### Docker (hosted runtime image)

- `docker/runtime.dockerfile` last stage: `COPY .build/build-identity.json /app/build-identity.json` as final layer (preserves cache).
- Workflow sets `org.opencontainers.image.revision=${GITHUB_SHA}` label.

## Dev loop

`make dogfood-refresh` becomes:
1. `scripts/build/generate_build_identity.py` → writes `.build/build-identity.json` with `channel=dev`.
2. Builds a wheel (`uv build`) and installs it — **not** an editable install. ~25s slower than editable; in return, every refresh produces a CLI whose identity is compiled-in and correct.
3. Rebuilds the engine (picks up the identity via `build.rs` rerun-if-changed).
4. Rebuilds the app bundle if the macOS app changed.

Net: every local build has a traceable, binary-local identity. Two different commits never both present as `0.2.0`. An old binary doesn't read a newer shared file and lie about its SHA.

## Entrypoint consolidation — out of scope for this initiative

The Makefile / `scripts/` sprawl problem is real but orthogonal. Tracked as a separate docket item. This spec stays focused on release trust and build identity.

## Out of scope for v1

- Delta / differential updates for the CLI. PyPI `uv tool upgrade` stays the upgrade path.
- Auto-rollback on engine restart pending or related build mismatch states. Menu bar shows the warning; user acts.
- Per-commit build identity for hosted runtime beyond SHA. If someone wants "which push landed on demo at 3:14pm" that's in `/api/health` already.
- Signing/attestation of `build-identity.json`. Trust model is "same repo, same developer" for now.

## What we delete when this lands

- Hardcoded `"0.1.15-local"` fallback strings wherever they appear.
- `/api/version` and version-duplicating fields on `/api/system/info` (collapsed into `/api/health`).
- The placeholder versions in `engine/Cargo.toml`, `runner/package.json`, and iOS xcconfig — all share the one release number.
- `release.sh`'s custom Python in-line semver rewriter (replaced by `bump-my-version`).

## Phases

1. **P0** — this spec + docket item (done).
2. **P1** — `generate_build_identity.py` + tests.
3. **P2** — Python build-identity module, CLI `--version`, `/api/health` `build` block. Wheel force-includes the JSON. `make dogfood-refresh` switches to wheel build+install.
4. **P3** — Engine `build.rs` + `BuildIdentity` struct. `longhouse-engine --version` prints qualified string. Engine stamps identity into `~/.claude/engine-status.json`. Menu bar reads and displays. Restart-pending detection (installed build vs running engine daemon).
5. **P4** — iOS xcconfig + XcodeGen build phase + Swift reader + About screen.
6. **P5** — `.bumpversion.toml` + shared-version wrapper replacing the in-line semver rewriter in `release.sh`.
7. **P6** — cleanup: strip hardcoded `0.1.15-local` fallback strings, kill `/api/version` if redundant, record AGENTS.md learning.

After each phase: commit, internal review, address findings, then move on.
