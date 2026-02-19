# Python Shipper Removal + Rust Engine Migration

**Status:** Draft
**Owner:** david010@gmail.com
**Last Updated:** 2026-02-19

## Goals
- Remove the Python shipper daemon, watcher, spool, and state logic.
- Keep `longhouse auth`, hook installation, and MCP registration fully functional.
- Make `longhouse connect --install/--uninstall/--status` manage `longhouse-engine connect`.
- Preserve token and URL handoff via `~/.claude/longhouse-device-token` and `~/.claude/longhouse-url`.
- Keep `longhouse ship --file` working for the Claude Stop hook.

## Non-Goals
- Change ingest API contracts or server-side parsing.
- Rebuild hook behavior or MCP server setup.
- Introduce new background schedulers or polling-only modes.

## Exact File Disposition Table

| File | Disposition | Why |
| --- | --- | --- |
| `apps/zerg/backend/zerg/services/shipper/__init__.py` | Migrate | Remove exports for deleted Python shipper classes; keep hooks/token/parser/service exports.
| `apps/zerg/backend/zerg/services/shipper/hooks.py` | Keep | Still installs Claude hooks and MCP registrations. The Stop hook continues to call `longhouse ship --file`.
| `apps/zerg/backend/zerg/services/shipper/token.py` | Keep | Source of truth for token + URL storage used by the Rust engine.
| `apps/zerg/backend/zerg/services/shipper/parser.py` | Keep | Still used by `commis_job_processor.py`.
| `apps/zerg/backend/zerg/services/shipper/service.py` | Migrate | Must manage `longhouse-engine connect` instead of Python shipper.
| `apps/zerg/backend/zerg/services/shipper/shipper.py` | Delete | Python shipper core replaced by Rust engine.
| `apps/zerg/backend/zerg/services/shipper/watcher.py` | Delete | Python file watcher replaced by Rust engine.
| `apps/zerg/backend/zerg/services/shipper/spool.py` | Delete | Python spool replaced by Rust engine.
| `apps/zerg/backend/zerg/services/shipper/state.py` | Delete | Python state tracker replaced by Rust engine.
| `apps/zerg/backend/zerg/services/shipper/providers/__init__.py` | Delete | Only used by Python shipper/tests.
| `apps/zerg/backend/zerg/services/shipper/providers/claude.py` | Delete | Only used by Python shipper/tests.
| `apps/zerg/backend/zerg/services/shipper/providers/codex.py` | Delete | Only used by Python shipper/tests.
| `apps/zerg/backend/zerg/services/shipper/providers/gemini.py` | Delete | Only used by Python shipper/tests.
| `apps/zerg/backend/zerg/cli/connect.py` | Migrate | Remove Python shipper usage. Exec `longhouse-engine` for `connect` and `ship`.
| `apps/zerg/backend/zerg/cli/doctor.py` | Migrate | Update log checks and service naming to reflect Rust engine.

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
"longhouse-engine not found. Install it or build `apps/engine` (cargo build --release)."

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
Key changes: `ProgramArguments` should call `longhouse-engine connect`; remove `AGENTS_API_TOKEN` env; log paths moved to log dir.

```
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
</dict>
</plist>
```

### Linux systemd unit (new shape)
Key changes: ExecStart uses `longhouse-engine connect`; remove AGENTS_API_TOKEN.

```
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
- `get_service_info()` should report log location under `~/.claude/logs/` (engine logs: `engine.log.YYYY-MM-DD`).

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
- `longhouse ship` should become a wrapper around `longhouse-engine ship`.
- Keep CLI flags for backwards compatibility and map them:
  - `--url` -> `longhouse-engine ship --url <url>`
  - `--token` -> `longhouse-engine ship --token <token>`
  - `--claude-dir` -> `CLAUDE_CONFIG_DIR` env
  - `--file` -> `longhouse-engine ship --file <path>` (requires Rust engine change below)
- `--quiet` should suppress stdout/stderr by redirecting to `DEVNULL` when invoking the engine.

### Hooks
- Keep `hooks.py` scripts unchanged. `longhouse ship --file` still exists and now routes to the Rust engine.

## Rust Engine Change Needed for Hooks

Add a single-file path to the engine so `longhouse ship --file` is cheap and suitable for hooks.

### CLI changes
- Extend `Commands::Ship` in `apps/engine/src/main.rs` with:
  - `--file <path>` optional
  - Optional `--provider <name>` override (claude|codex|gemini), default to auto-detect.

### Implementation outline
- Add a `cmd_ship_file(...)` that:
  - Uses `discovery::get_providers()` and `provider_for_path()` to infer provider.
  - Falls back to file extension if provider detection fails.
  - Uses `shipper::prepare_file()` and `shipper::ship_and_record()` for the single path.
  - Uses `ShipperConfig::from_env().with_overrides(url, token, db_path, None)`.
  - Produces concise stdout ("Shipped X events" / "No new events") unless `--json` is set.

## ShipperConfig Dead Field Removal
- `scan_interval_seconds`, `batch_size`, and `max_batch_bytes` in Python `ShipperConfig` are unused.
- Deleting `shipper.py` removes them. If any transitional stub remains, drop the fields and update tests.

## Phased Implementation Plan (Atomic, Tests Passing Each Phase)

### Phase 1: Service Migration + Install Path
1. Update `service.py` to resolve `longhouse-engine` and emit new launchd/systemd definitions.
2. Update `connect.py` install path to persist token + URL before service installation.
3. Update `doctor.py` to check engine log files and show engine service info.
4. Update or adjust service-related tests to reflect the new ProgramArguments and log paths.

**Done conditions (macOS):**
- `longhouse-engine --version` prints `longhouse-engine <version>`.
- `longhouse connect --install --url http://localhost:8080` prints `[OK] Service installed and started`.
- `plutil -p ~/Library/LaunchAgents/com.longhouse.shipper.plist | grep longhouse-engine` shows the engine path.
- `longhouse connect --status` prints `Status: running`.
- `make test` passes.

### Phase 2: CLI Exec to Rust + Engine Single-File Ship
1. Add `--file` support to `longhouse-engine ship` as described.
2. Replace Python ship/conn loops with `longhouse-engine` exec wrappers in `connect.py`.
3. Map CLI flags (`--debounce`, `--poll`, `--interval`, `--verbose`, `--claude-dir`) to engine args/env.
4. Update CLI tests (`tests/cli/test_connect.py`) to assert subprocess/exec calls rather than ShipResult handling.

**Done conditions (macOS):**
- `FILE=$(ls ~/.claude/projects/*/*.jsonl | head -n 1)`
- `longhouse ship --file "$FILE" --quiet` exits with code 0.
- `longhouse connect --status` still works.
- `make test` passes.

### Phase 3: Delete Python Shipper + Cleanup Tests
1. Delete `shipper.py`, `watcher.py`, `spool.py`, `state.py`, and `providers/`.
2. Remove Python shipper exports from `services/shipper/__init__.py`.
3. Remove or rewrite Python shipper test suites:
   - Remove `tests/services/shipper/test_shipper.py`, `test_watcher.py`, `test_spool.py`, `test_state.py`, `test_smoke.py`, provider tests, and integration watcher tests.
   - Keep and update `test_service.py` if still meaningful.
4. Remove or update `apps/zerg/backend/scripts/profile_shipper.py` (no Python shipper to profile).

**Done conditions (macOS):**
- `rg -n "SessionShipper|SessionWatcher|ShipperConfig|ShipResult" apps/zerg/backend -g"*.py"` returns no matches.
- `rg -n "services/shipper/providers" apps/zerg/backend -g"*.py"` returns no matches.
- `make test` passes.

## Rollback Plan

If any phase fails in production or dev:
1. Stop the engine service: `longhouse connect --uninstall`.
2. Revert to the last known-good commit (pre-migration) using `git revert` or `git checkout`.
3. Reinstall the Python-backed service: `longhouse connect --install --url <url>`.
4. Verify status: `longhouse connect --status` should report running.
5. If Rust engine changes were made (ship --file), revert those commits as well to restore parity.

## Notes and Constraints
- Token and URL must remain in `~/.claude/longhouse-device-token` and `~/.claude/longhouse-url` to keep Rust config resolution intact.
- Keep parser usage in `commis_job_processor.py` unchanged.
- Do not remove zstd support in API ingest; Rust engine uses zstd by default.
