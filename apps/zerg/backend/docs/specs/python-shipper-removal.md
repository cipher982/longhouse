# Python Shipper Removal + Rust Engine Migration

**Status:** Ready for implementation
**Owner:** david010@gmail.com
**Last Updated:** 2026-02-19
**Spec version:** 2 (audit-validated)

---

## Context

The Rust engine (`longhouse-engine connect`) fully replaced the Python watcher daemon. It is built, installed at `~/.local/bin/longhouse-engine`, and running on the user's machine. The Python CLI (`longhouse connect`, `longhouse ship`) still calls the Python `SessionShipper` and `SessionWatcher` classes directly — these are dead code that must be cut.

**Why this matters (VISION alignment):**
- VISION target: "Shipper watches ~/.claude/, ~/.codex/, ~/.gemini/..." — the Rust engine already does this
- VISION target: "Hook-based push (preferred for Claude Code)" — the Stop hook calls `longhouse ship --file`; that must keep working after Python shipper is deleted
- VISION target: 27 MB RSS idle (Rust) vs 835 MB (Python) — we are already running Rust; Python is just lingering dead code

**Current migration status (as of 2026-02-19): 0% complete.** None of the three phases below have been started.

---

## Goals
- Remove the Python shipper daemon, watcher, spool, and state logic.
- Keep `longhouse auth`, hook installation, and MCP registration fully functional.
- Make `longhouse connect --install/--uninstall/--status` manage `longhouse-engine connect`.
- Preserve token and URL handoff via `~/.claude/longhouse-device-token` and `~/.claude/longhouse-url`.
- Keep `longhouse ship --file` working for the Claude Stop hook (routes to engine).

## Non-Goals
- Change ingest API contracts or server-side parsing.
- Rebuild hook behavior or MCP server setup.
- Introduce new background schedulers or polling-only modes.

---

## Critical Blocker

**Rust engine needs `--file` support on `longhouse-engine ship` before Phase 2 can complete.**

The Claude Stop hook calls `longhouse ship --file <path>`. After the Python shipper is deleted, `longhouse ship` must exec `longhouse-engine ship --file <path>`. That `--file` flag does not yet exist in the Rust engine.

This is the only Rust change required. Everything else is Python-side.

---

## Exact File Disposition Table

| File | Disposition | Why |
| --- | --- | --- |
| `apps/zerg/backend/zerg/services/shipper/__init__.py` | Migrate | Remove exports for deleted Python shipper classes; keep hooks/token/parser/service exports. |
| `apps/zerg/backend/zerg/services/shipper/hooks.py` | Keep | Still installs Claude hooks and MCP registrations. Stop hook continues to call `longhouse ship --file`. |
| `apps/zerg/backend/zerg/services/shipper/token.py` | Keep | Source of truth for token + URL storage used by the Rust engine. |
| `apps/zerg/backend/zerg/services/shipper/parser.py` | Keep | Still used by `commis_job_processor.py`. |
| `apps/zerg/backend/zerg/services/shipper/service.py` | Migrate | Must manage `longhouse-engine connect` instead of Python shipper. |
| `apps/zerg/backend/zerg/services/shipper/shipper.py` | Delete | Python shipper core replaced by Rust engine. |
| `apps/zerg/backend/zerg/services/shipper/watcher.py` | Delete | Python file watcher replaced by Rust engine. |
| `apps/zerg/backend/zerg/services/shipper/spool.py` | Delete | Python spool replaced by Rust engine. |
| `apps/zerg/backend/zerg/services/shipper/state.py` | Delete | Python state tracker replaced by Rust engine. |
| `apps/zerg/backend/zerg/services/shipper/providers/__init__.py` | Delete | Only used by Python shipper/tests. |
| `apps/zerg/backend/zerg/services/shipper/providers/claude.py` | Delete | Only used by Python shipper/tests. |
| `apps/zerg/backend/zerg/services/shipper/providers/codex.py` | Delete | Only used by Python shipper/tests. |
| `apps/zerg/backend/zerg/services/shipper/providers/gemini.py` | Delete | Only used by Python shipper/tests. |
| `apps/zerg/backend/zerg/cli/connect.py` | Migrate | Remove Python shipper usage. Exec `longhouse-engine` for `connect` and `ship`. |
| `apps/zerg/backend/zerg/cli/doctor.py` | Migrate | Update log checks and service naming to reflect Rust engine. |
| `apps/zerg/backend/zerg/cli/config_file.py` | Migrate | Remove unused `mode`/`interval` shipper config fields (become orphaned after CLI migration). |
| `apps/zerg/backend/scripts/profile_shipper.py` | Delete | No Python shipper to profile. |
| `apps/zerg/backend/tests/cli/test_connect.py` | Migrate | Mocks `_ship_once`/`_ship_file` and checks ShipResult output — must be rewritten for subprocess/exec assertions. |
| `apps/zerg/backend/tests/services/shipper/test_service.py` | Migrate | Asserts `shipper.log` and old plist shape — must be rewritten for engine log pattern and new plist args. |
| `apps/zerg/backend/tests/integration/test_shipper_watcher_e2e.py` | Delete | Imports `SessionShipper`/`SessionWatcher` directly; no longer relevant after Phase 3. |
| `apps/zerg/backend/tests/services/shipper/test_shipper.py` | Delete | Tests Python shipper; gone in Phase 3. |
| `apps/zerg/backend/tests/services/shipper/test_watcher.py` | Delete | Tests Python watcher; gone in Phase 3. |
| `apps/zerg/backend/tests/services/shipper/test_spool.py` | Delete | Tests Python spool; gone in Phase 3. |
| `apps/zerg/backend/tests/services/shipper/test_state.py` | Delete | Tests Python state tracker; gone in Phase 3. |

---

## Service Migration Spec (service.py)

### Binary Resolution
Add `get_engine_executable()` with this resolution order. Return an absolute path and raise a clear error if not found.

1. `shutil.which("longhouse-engine")`
2. `~/.local/bin/longhouse-engine`
3. `~/.claude/bin/longhouse-engine`
4. Repo dev builds
   - `<repo>/apps/engine/target/release/longhouse-engine`
   - `<repo>/apps/engine/target/debug/longhouse-engine`

If none are found, error message should say:
`"longhouse-engine not found. Install it or build apps/engine (cargo build --release)."`

### Service Identity
Keep existing identifiers for compatibility so `--status` and `--uninstall` still control old installs.
- `LAUNCHD_LABEL = "com.longhouse.shipper"`
- `SYSTEMD_UNIT = "longhouse-shipper"`

### ServiceConfig Fields
Replace `poll_mode` and `interval` with engine-relevant settings:
- `url: str` (for persistence only, not in plist/unit args)
- `token: str | None` (for persistence only)
- `claude_dir: str | None`
- `flush_ms: int = 500`
- `fallback_scan_secs: int = 300`
- `spool_replay_secs: int = 30`
- `log_dir: str | None` (default: `<claude_dir>/logs`)

### Argument Mapping
Engine arguments to emit in the plist/unit:
- `connect`
- `--flush-ms <flush_ms>`
- `--fallback-scan-secs <fallback_scan_secs>`
- `--spool-replay-secs <spool_replay_secs>`
- `--log-dir <log_dir>` (if set)

Do not pass `--url` or `--token` in arguments. Token + URL are read from `~/.claude` files.

### Environment Variables
Add environment variables only for filesystem location and logs:
- `CLAUDE_CONFIG_DIR` if `claude_dir` is provided.
- `LONGHOUSE_LOG_DIR` if `log_dir` is provided.

Do not set `AGENTS_API_TOKEN` in the service definition.

### macOS launchd plist (new shape)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.longhouse.shipper</string>
    <key>ProgramArguments</key>
    <array>
        <string>/absolute/path/to/longhouse-engine</string>
        <string>connect</string>
        <string>--flush-ms</string>
        <string>500</string>
        <string>--fallback-scan-secs</string>
        <string>300</string>
        <string>--spool-replay-secs</string>
        <string>30</string>
        <string>--log-dir</string>
        <string>/Users/<you>/.claude/logs</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_CONFIG_DIR</key>
        <string>/Users/<you>/.claude</string>
        <key>LONGHOUSE_LOG_DIR</key>
        <string>/Users/<you>/.claude/logs</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/<you>/.claude/logs/engine.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/<you>/.claude/logs/engine.stdout.log</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>Nice</key>
    <integer>10</integer>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
```

### Linux systemd unit (new shape)

```ini
[Unit]
Description=Longhouse Engine - Session Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/absolute/path/to/longhouse-engine connect --flush-ms 500 --fallback-scan-secs 300 --spool-replay-secs 30 --log-dir /home/<you>/.claude/logs
Restart=on-failure
RestartSec=10
Environment="CLAUDE_CONFIG_DIR=/home/<you>/.claude"
Environment="LONGHOUSE_LOG_DIR=/home/<you>/.claude/logs"

[Install]
WantedBy=default.target
```

### Status + Info
- `get_service_status()` remains unchanged but reflects the engine-backed service.
- `get_service_info()` should report log location under `~/.claude/logs/` (engine rolling logs: `engine.log.YYYY-MM-DD`).

---

## connect.py Surgery Spec

### Remove
- All Python shipper imports (`SessionShipper`, `SessionWatcher`, `ShipperConfig`, `ShipResult`).
- `_ship_file`, `_ship_once`, `_watch_loop`, `_spool_replay_loop`, `_polling_loop` and any direct `asyncio` shipping logic.

### Add
- `from zerg.services.shipper.service import get_engine_executable` (new helper in service.py).
- A small runner to exec or spawn `longhouse-engine` with env overrides.

### New `connect` Behavior
- `--status` and `--uninstall` stay the same (call migrated service.py).
- When `--install` is set, always persist URL and token before calling `install_service()`.
  - `save_zerg_url(url, config_dir)`
  - `save_token(token, config_dir)`
- Foreground mode: exec `longhouse-engine connect` instead of running a Python loop.
  - Map `--debounce` to `--flush-ms`.
  - Map `--interval` to `--fallback-scan-secs` only when the user explicitly sets `--interval` or `--poll`.
  - Print a warning if `--poll` is provided: polling is removed; watch mode is always on.
  - If `--verbose` is set, export `RUST_LOG=longhouse_engine=debug` before exec.
  - If `--claude-dir` is set, export `CLAUDE_CONFIG_DIR` and set `LONGHOUSE_LOG_DIR=<claude_dir>/logs`.

### New `ship` Behavior
- `longhouse ship` wraps `longhouse-engine ship`.
- Keep CLI flags for backwards compatibility and map them:
  - `--url` → `longhouse-engine ship --url <url>`
  - `--token` → `longhouse-engine ship --token <token>`
  - `--claude-dir` → `CLAUDE_CONFIG_DIR` env
  - `--file` → `longhouse-engine ship --file <path>` (**requires Rust engine change; see Critical Blocker**)
- `--quiet` suppresses both stdout AND stderr by redirecting to `DEVNULL`. The Stop hook in `hooks.py:51` already redirects stderr (`2>/dev/null`); the Python wrapper must add stdout suppression. Also: propagate the engine's exit code explicitly — even in quiet mode, non-zero exit from the engine should propagate as non-zero from the wrapper.

### Hooks
- Keep `hooks.py` scripts unchanged. `longhouse ship --file` still exists and now routes to the Rust engine.

---

## Rust Engine Change: `--file` Support for Ship

**This is the only required Rust change.** All other work is Python-side.

### CLI changes (`apps/engine/src/main.rs`)
Extend `Commands::Ship` with:
- `--file <path>` — optional path to a single session file to ship
- `--provider <name>` — optional provider override (claude|codex|gemini), default auto-detect from path

### Implementation outline
Add a `cmd_ship_file(...)` that:
- Calls `discovery::get_providers()` (`discovery.rs:20-46`) and `provider_for_path()` (`discovery.rs:85-94`) to infer provider from the file path. Note: `provider_for_path()` checks path prefix only — add an extension-based fallback in `cmd_ship_file` (`.jsonl` → claude/codex, `.json` → gemini) for cases where the path doesn't match a known root.
- Uses `shipper::prepare_file()` and `shipper::ship_and_record()` (`shipper.rs:31-115`) for the single path.
- Uses `ShipperConfig::from_env().with_overrides(url, token, db_path, None)`.
- Outputs concise stdout: `"Shipped X events"` / `"No new events"` unless `--json` is set.
- Returns exit code 0 on success, non-zero on failure.

The existing `longhouse-engine ship` (full scan) is unaffected — this adds a fast single-file path alongside it.

---

## doctor.py Changes

Minimal: update log detection to look for rolling engine log files:
- Current: checks for `~/.claude/shipper.log`
- Target: checks for `~/.claude/logs/engine.log.*` (tracing-appender rolling pattern)
- Report "Rust engine" in service section, not "Python shipper"

---

## Test Strategy

### Phase 1 tests (service.py)
- Rewrite plist generation test: assert `ProgramArguments` contains `longhouse-engine` and `connect`, contains `--flush-ms`, `--fallback-scan-secs`, `--spool-replay-secs`. Assert env vars include `CLAUDE_CONFIG_DIR` and `LONGHOUSE_LOG_DIR`. Assert `AGENTS_API_TOKEN` NOT present.
- Rewrite systemd unit test: assert `ExecStart` calls engine with correct flags.
- Test `get_engine_executable()`: assert resolution order (mock `shutil.which` to return None, verify fallback to `~/.local/bin/`).

### Phase 2 tests (connect.py)
- The foreground `connect` path uses `os.execvp` which replaces the process — tests must monkeypatch `os.execvp` to prevent process replacement and assert the argument list passed to it.
- The `ship --file` path uses `subprocess.run` — mock it and assert args + that non-zero exit code propagates.
- Test flag mapping: `--debounce 200` → `--flush-ms 200` in engine args.
- Test `--poll` warning output.
- Test `--quiet`: assert both stdout and stderr are DEVNULL.

### Phase 3 tests (cleanup)
- Remove test files for deleted modules: `test_shipper.py`, `test_watcher.py`, `test_spool.py`, `test_state.py`, `test_smoke.py`, provider tests.
- Keep and update `test_service.py`.
- Verify: `rg "SessionShipper|SessionWatcher|ShipperConfig|ShipResult" apps/zerg/backend -g"*.py"` returns no matches.

---

## Phased Implementation Plan

Each phase ends with `make test` green.

### Phase 1: Service Migration + Install Path

**Goal:** `longhouse connect --install` writes a plist that launches `longhouse-engine`, not Python.

1. Migrate `service.py`:
   - Add `get_engine_executable()` with fallback resolution chain.
   - Replace `ServiceConfig` fields: drop `poll_mode`, `interval`; add `flush_ms`, `fallback_scan_secs`, `spool_replay_secs`, `log_dir`.
   - Rewrite `_generate_launchd_plist()` to call engine with new args + env vars.
   - Rewrite `_generate_systemd_unit()` same.
   - Remove `AGENTS_API_TOKEN` from generated service defs.
   - Update `get_service_info()` log path to `engine.log.*`.
2. Update `connect.py` install handler: persist token + URL BEFORE calling `install_service()`.
3. Update `doctor.py`: check for engine logs, update service description.
4. Rewrite service tests for new plist/unit shape.

**Done conditions:**
- `longhouse-engine --version` prints version.
- `longhouse connect --install --url http://localhost:8080` exits clean.
- `plutil -p ~/Library/LaunchAgents/com.longhouse.shipper.plist | grep longhouse-engine` shows the engine path.
- `longhouse connect --status` prints `Status: running`.
- `make test` passes.

---

### Phase 2: CLI Exec to Rust Engine (requires Rust `--file` first)

**Goal:** `longhouse connect` and `longhouse ship` exec the Rust engine; zero Python shipping logic remains active.

1. Add `--file` to `Commands::Ship` in `apps/engine/src/main.rs` + implement `cmd_ship_file`.
2. Migrate `connect.py`:
   - Remove `_ship_file`, `_ship_once`, `_watch_loop`, `_spool_replay_loop`, `_polling_loop`.
   - Remove Python shipper imports.
   - Foreground `connect`: `os.execvp("longhouse-engine", ["longhouse-engine", "connect", ...])`.
   - `ship --file <path>`: `subprocess.run([engine, "ship", "--file", path, ...])`.
   - Flag mappings: `--debounce` → `--flush-ms`, `--interval` → `--fallback-scan-secs`, `--verbose` → `RUST_LOG=longhouse_engine=debug`.
   - `--poll` warning.
   - `--quiet` → redirect engine stdout/stderr to DEVNULL.
3. Rewrite CLI tests for subprocess/exec assertions.

**Done conditions:**
- `FILE=$(ls ~/.claude/projects/*/*.jsonl | head -n 1); longhouse ship --file "$FILE" --quiet` exits 0.
- `longhouse connect` (foreground) execs engine — confirmed by `ps aux | grep longhouse-engine`.
- `longhouse connect --poll` prints warning.
- `make test` passes.

---

### Phase 3: Delete Python Shipper + Cleanup

**Goal:** Dead code gone. Codebase is clean.

1. Delete files: `shipper.py`, `watcher.py`, `spool.py`, `state.py`, `providers/`.
2. Update `services/shipper/__init__.py`: remove deleted class exports.
3. Delete `scripts/profile_shipper.py`.
4. Remove Python shipper test suites:
   - Delete `tests/services/shipper/test_shipper.py`, `test_watcher.py`, `test_spool.py`, `test_state.py`, `test_smoke.py`, provider tests, integration watcher tests.
   - Keep and update `test_service.py`.

**Done conditions:**
- `rg "SessionShipper|SessionWatcher|ShipperConfig|ShipResult" apps/zerg/backend -g"*.py"` → no matches.
- `rg "services/shipper/providers" apps/zerg/backend -g"*.py"` → no matches.
- `make test` passes.

---

## Rollback Plan

If any phase breaks:
1. `longhouse connect --uninstall` to stop the service.
2. `git revert` to the last known-good commit.
3. `longhouse connect --install --url <url>` to reinstall with Python-backed service.
4. Verify: `longhouse connect --status` reports running.
5. If Rust engine `--file` changes were made: revert those commits AND reinstall the previous `longhouse-engine` binary (git revert does not touch `~/.local/bin/longhouse-engine`). Either rebuild from the reverted commit (`cargo build --release`) or install the previous release.

---

## Invariants (Do Not Break)

- Token and URL stay in `~/.claude/longhouse-device-token` and `~/.claude/longhouse-url`.
- `parser.py` is kept — used by `commis_job_processor.py`.
- `hooks.py` is kept — installs Claude Stop hook and MCP registration.
- zstd decompression stays in `routers/agents.py` — Rust engine uses zstd.
- Plist label `com.longhouse.shipper` and systemd unit `longhouse-shipper` are unchanged — keeps `--uninstall` working on existing installs.
