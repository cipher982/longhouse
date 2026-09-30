//! Shipper configuration.
//!
//! Reads canonical machine state from `~/.longhouse/machine/state.json` and the
//! device token from `~/.longhouse/machine/device-token`.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

/// Shipper configuration (mirrors Python `ShipperConfig`).
#[derive(Debug, Clone)]
pub struct ShipperConfig {
    pub api_url: String,
    pub api_token: Option<String>,
    pub db_path: Option<PathBuf>,
    pub workers: usize,
    pub max_batch_bytes: u64,
    pub timeout_seconds: u64,
    /// Human-readable machine label (set by user during `longhouse connect --install`).
    /// Stored in `~/.longhouse/machine/state.json`. Defaults to hostname.
    pub machine_name: String,
}

#[derive(Debug, Default, Deserialize)]
struct MachineStateFile {
    runtime_url: Option<String>,
    machine_name: Option<String>,
}

/// Logical CPUs available to this process (honours cgroup quotas on Linux).
pub fn cpu_count() -> usize {
    std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get)
}

impl Default for ShipperConfig {
    fn default() -> Self {
        Self {
            api_url: "http://localhost:8080".to_string(),
            api_token: None,
            db_path: None,
            workers: cpu_count(),
            max_batch_bytes: 50 * 1024 * 1024, // 50 MB
            timeout_seconds: 60,
            machine_name: default_machine_name(),
        }
    }
}

impl ShipperConfig {
    /// Load config from standard file locations.
    pub fn from_env() -> Result<Self> {
        let machine_dir = get_machine_dir()?;
        let mut config = Self::default();

        let state_path = machine_dir.join("state.json");
        if state_path.exists() {
            let state = load_machine_state(&state_path)?;
            if let Some(name) = normalized_state_field(state.machine_name) {
                config.machine_name = name;
            }
            if let Some(url) = normalized_state_field(state.runtime_url) {
                config.api_url = url;
            }
        }

        // Read token from file
        let token_path = machine_dir.join("device-token");
        if token_path.exists() {
            let token = std::fs::read_to_string(&token_path)
                .with_context(|| format!("reading {}", token_path.display()))?
                .trim()
                .to_string();
            if !token.is_empty() {
                config.api_token = Some(token);
            }
        }

        Ok(config)
    }

    /// Override fields from CLI args (only override if non-default).
    pub fn with_overrides(
        mut self,
        url: Option<&str>,
        token: Option<&str>,
        db_path: Option<&Path>,
        workers: Option<usize>,
        machine_name: Option<&str>,
        max_batch_bytes: Option<u64>,
    ) -> Self {
        if let Some(u) = url {
            self.api_url = u.to_string();
        }
        if let Some(t) = token {
            self.api_token = Some(t.to_string());
        }
        if let Some(p) = db_path {
            self.db_path = Some(p.to_path_buf());
        }
        if let Some(w) = workers {
            if w > 0 {
                self.workers = w;
            }
        }
        if let Some(m) = machine_name {
            if !m.is_empty() {
                self.machine_name = m.to_string();
            }
        }
        if let Some(bytes) = max_batch_bytes {
            if bytes > 0 {
                self.max_batch_bytes = bytes;
            }
        }
        self
    }
}

/// Default machine name: read from hostname command.
fn default_machine_name() -> String {
    std::process::Command::new("hostname")
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Build a ShipperConfig as if CLAUDE_CONFIG_DIR points to a temp dir.
    fn config_from_dir(dir: &std::path::Path) -> ShipperConfig {
        let mut config = ShipperConfig::default();
        let machine_dir = dir.join("machine");
        let state_path = machine_dir.join("state.json");

        if state_path.exists() {
            let state = load_machine_state(&state_path).unwrap();
            if let Some(name) = normalized_state_field(state.machine_name) {
                config.machine_name = name;
            }
            if let Some(url) = normalized_state_field(state.runtime_url) {
                config.api_url = url;
            }
        }
        config
    }

    #[test]
    fn adopt_command_quotes_the_token_path_for_the_shell() {
        let _guard = crate::console_adapter::agent_state_guard();
        let command = temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some("/tmp/it's here/.longhouse")),
                ("CLAUDE_CONFIG_DIR", None),
            ],
            adopt_device_identity_command,
        );
        assert!(
            command.starts_with(
                "LONGHOUSE_DEVICE_TOKEN=\"$(cat '/tmp/it'\\''s here/.longhouse/machine/device-token')\" longhouse auth"
            ),
            "{command}"
        );
    }

    #[test]
    fn test_machine_name_loaded_from_file() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("machine")).unwrap();
        fs::write(
            dir.path().join("machine").join("state.json"),
            r#"{"machine_name":"work-macbook"}"#,
        )
        .unwrap();

        let config = config_from_dir(dir.path());
        assert_eq!(config.machine_name, "work-macbook");
    }

    #[test]
    fn test_machine_name_falls_back_to_hostname_when_file_missing() {
        let dir = tempfile::tempdir().unwrap();
        let config = config_from_dir(dir.path());
        // Should not be empty — falls back to hostname or "unknown"
        assert!(!config.machine_name.is_empty());
    }

    #[test]
    fn test_machine_name_empty_file_ignored() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("machine")).unwrap();
        fs::write(
            dir.path().join("machine").join("state.json"),
            "{\"machine_name\":\"   \"}",
        )
        .unwrap();

        let config = config_from_dir(dir.path());
        // Empty file → falls back to hostname, not empty string
        assert!(!config.machine_name.is_empty());
        assert!(config.machine_name != "   ");
    }

    #[test]
    fn test_with_overrides_sets_machine_name() {
        let config = ShipperConfig::default().with_overrides(
            None,
            None,
            None,
            None,
            Some("home-server"),
            None,
        );
        assert_eq!(config.machine_name, "home-server");
    }

    #[test]
    fn test_with_overrides_empty_machine_name_ignored() {
        let original = ShipperConfig::default();
        let original_name = original.machine_name.clone();
        let config = original.with_overrides(None, None, None, None, Some(""), None);
        // Empty string override is ignored — keeps existing name
        assert_eq!(config.machine_name, original_name);
    }

    #[test]
    fn test_with_overrides_none_machine_name_keeps_existing() {
        let mut config = ShipperConfig::default();
        config.machine_name = "my-machine".to_string();
        let config = config.with_overrides(None, None, None, None, None, None);
        assert_eq!(config.machine_name, "my-machine");
    }

    #[test]
    fn test_with_overrides_sets_max_batch_bytes() {
        let config =
            ShipperConfig::default().with_overrides(None, None, None, None, None, Some(1234));
        assert_eq!(config.max_batch_bytes, 1234);
    }

    #[test]
    fn test_provider_home_maps_custom_env_path_to_sibling_longhouse() {
        let mapped = provider_home_to_longhouse_home(PathBuf::from("/tmp/custom-claude"));
        assert_eq!(mapped, PathBuf::from("/tmp/.longhouse"));
    }
}

fn load_machine_state(path: &Path) -> Result<MachineStateFile> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_slice::<MachineStateFile>(&bytes)
        .with_context(|| format!("parsing {}", path.display()))
}

fn normalized_state_field(value: Option<String>) -> Option<String> {
    value.and_then(|item| {
        let normalized = item.trim().to_string();
        if normalized.is_empty() {
            None
        } else {
            Some(normalized)
        }
    })
}

/// Resolve the Longhouse-owned machine config directory.
pub fn get_machine_dir() -> Result<PathBuf> {
    Ok(get_longhouse_home()?.join("machine"))
}

/// Resolve the Longhouse-owned agent state directory.
pub fn get_agent_dir() -> Result<PathBuf> {
    Ok(get_longhouse_home()?.join("agent"))
}

pub fn get_agent_outbox_dir() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("outbox"))
}

pub fn get_agent_runtime_events_outbox_dir() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("runtime-events-outbox"))
}

pub fn get_agent_status_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("engine-status.json"))
}

pub fn get_agent_transcript_wake_socket_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("transcript-wake.sock"))
}

pub fn get_agent_db_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("longhouse-shipper.db"))
}

pub fn get_agent_archive_repair_control_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("archive-repair-control.json"))
}

pub fn get_agent_update_control_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("update-control.json"))
}

pub fn get_agent_update_status_path() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("update-status.json"))
}

pub fn get_agent_log_dir() -> Result<PathBuf> {
    Ok(get_agent_dir()?.join("logs"))
}

pub fn get_codex_bridge_state_dir() -> Result<PathBuf> {
    Ok(get_longhouse_home()?
        .join("managed-local")
        .join("codex-bridge"))
}

pub fn get_agent_flight_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("LONGHOUSE_ENGINE_FLIGHT_RECORDER_DIR") {
        let trimmed = dir.trim();
        if !trimmed.is_empty() {
            return Ok(PathBuf::from(trimmed));
        }
    }
    Ok(get_agent_dir()?.join("flight-recorder"))
}

/// The one command that re-adopts the stored device token's own name.
///
/// `longhouse auth` asks the Runtime Host which device a token belongs to and
/// stores that name, so re-running it over the token already on disk repairs a
/// machine configured under any other name without minting a new token.
pub fn adopt_device_identity_command() -> String {
    // Single-quote a resolved path (escaping any quote in it); the fallback
    // stays unquoted so the shell still expands $HOME.
    let token_path = get_longhouse_home()
        .map(|home| {
            let path = home.join("machine").join("device-token");
            format!("'{}'", path.display().to_string().replace('\'', "'\\''"))
        })
        .unwrap_or_else(|_| "\"$HOME/.longhouse/machine/device-token\"".to_string());
    format!(
        "LONGHOUSE_DEVICE_TOKEN=\"$(cat {token_path})\" longhouse auth && longhouse machine repair --repair-service"
    )
}

pub fn get_longhouse_home() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("LONGHOUSE_HOME") {
        return Ok(PathBuf::from(dir));
    }
    if let Ok(dir) = std::env::var("CLAUDE_CONFIG_DIR") {
        return Ok(provider_home_to_longhouse_home(PathBuf::from(dir)));
    }
    let home = std::env::var("HOME").context("HOME not set")?;
    Ok(PathBuf::from(home).join(".longhouse"))
}

fn provider_home_to_longhouse_home(path: PathBuf) -> PathBuf {
    if matches!(
        path.file_name().and_then(|value| value.to_str()),
        Some(".longhouse")
    ) {
        return path;
    }
    path.parent()
        .map(|parent| parent.join(".longhouse"))
        .unwrap_or_else(|| path.join(".longhouse"))
}

// ---------------------------------------------------------------------------
// Import scope (what local history this engine may ship)
// ---------------------------------------------------------------------------

use crate::import_scope::ImportScope;

/// The import scope this process enforces right now (see `import_scope`).
pub fn import_scope() -> ImportScope {
    match get_machine_dir() {
        Ok(machine_dir) => import_scope_in(&machine_dir),
        Err(_) => ImportScope::all("unresolved"),
    }
}

/// The scope each machine directory last resolved to in this process. A daemon
/// that loses its scope file keeps enforcing what it last knew instead of
/// reading an absent file as "no scope, import everything".
static LAST_KNOWN_SCOPE: std::sync::LazyLock<std::sync::Mutex<HashMap<PathBuf, ImportScope>>> =
    std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));

fn remember_scope(machine_dir: &Path, scope: &ImportScope) {
    if let Ok(mut known) = LAST_KNOWN_SCOPE.lock() {
        known.insert(machine_dir.to_path_buf(), scope.clone());
    }
}

fn last_known_scope(machine_dir: &Path) -> Option<ImportScope> {
    LAST_KNOWN_SCOPE.lock().ok()?.get(machine_dir).cloned()
}

/// `import_scope` for an explicit machine directory.
///
/// No stored scope and nothing resolved yet means nobody ever chose, which is
/// what every install did before scopes existed: unrestricted.
/// `resolve_import_scope` writes the first-run answer when the daemon starts, so
/// a running daemon always has one. A stored scope that cannot be read is not
/// guessed at: history stays closed from the moment this process first noticed,
/// and the error is logged. A corrupt file can hide history that should ship; it
/// can never leak history that should not.
pub fn import_scope_in(machine_dir: &Path) -> ImportScope {
    static UNREADABLE_SINCE: std::sync::OnceLock<chrono::DateTime<chrono::Utc>> =
        std::sync::OnceLock::new();
    match ImportScope::load(machine_dir) {
        Ok(Some(scope)) => {
            remember_scope(machine_dir, &scope);
            scope
        }
        Ok(None) => last_known_scope(machine_dir).unwrap_or_else(|| ImportScope::all("unset")),
        Err(error) => {
            let since = *UNREADABLE_SINCE.get_or_init(|| {
                tracing::error!(
                    error = %format!("{error:#}"),
                    "Import scope is unreadable; importing only sessions that start from now until it is fixed"
                );
                chrono::Utc::now()
            });
            ImportScope::starting(since, "invalid")
        }
    }
}

/// Write the scope this process last knew back to disk if its file is gone.
/// Returns whether a file was restored.
pub fn restore_lost_scope_file(machine_dir: &Path) -> bool {
    match (
        ImportScope::load(machine_dir),
        last_known_scope(machine_dir),
    ) {
        (Ok(None), Some(scope)) => scope.save_if_absent(machine_dir).unwrap_or(false),
        _ => false,
    }
}

/// Keep a second copy of the machine's choice in the shipper state database. The
/// file is what people and installers edit; the copy is what survives the file
/// being deleted, so "no file, but this machine has shipped" can be told apart
/// from a machine that shipped everything before scopes existed.
pub fn record_import_scope(conn: &rusqlite::Connection, scope: &ImportScope) {
    let result = serde_json::to_string(scope)
        .map_err(anyhow::Error::from)
        .and_then(|json| {
            conn.execute(
                "INSERT INTO import_scope_state (singleton_id, scope_json, recorded_at)
                 VALUES (1, ?1, ?2)
                 ON CONFLICT(singleton_id) DO UPDATE SET
                     scope_json = excluded.scope_json,
                     recorded_at = excluded.recorded_at",
                rusqlite::params![json, chrono::Utc::now().to_rfc3339()],
            )
            .map_err(anyhow::Error::from)
        });
    if let Err(error) = result {
        tracing::warn!(error = %format!("{error:#}"), "Could not record the import scope in the state database");
    }
}

fn recorded_import_scope(conn: &rusqlite::Connection) -> Result<Option<ImportScope>> {
    // A database written by an engine that predates scopes has no such table.
    let has_table: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'import_scope_state')",
        [],
        |row| row.get(0),
    )?;
    if !has_table {
        return Ok(None);
    }
    let json: Option<String> = conn
        .query_row(
            "SELECT scope_json FROM import_scope_state WHERE singleton_id = 1",
            [],
            |row| row.get(0),
        )
        .map(Some)
        .or_else(|error| match error {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })?;
    json.map(|json| serde_json::from_str(&json).context("the recorded import scope is unreadable"))
        .transpose()
}

/// What this machine's state databases say about it.
struct MachineHistory {
    /// The scope recorded beside the shipper state, if a scope-era engine ran.
    recorded: Option<ImportScope>,
    /// Whether the shipper state has ever tracked a source.
    has_history: bool,
}

/// Ask the given connection and the machine's own database. An engine started
/// with a different `--db` (a scratch database beside a real machine directory)
/// must not make a machine that has shipped for months look new.
fn machine_history(
    machine_dir: &Path,
    conn: Option<&rusqlite::Connection>,
) -> Result<MachineHistory> {
    let mut history = MachineHistory {
        recorded: None,
        has_history: false,
    };
    if let Some(conn) = conn {
        history.recorded = recorded_import_scope(conn)?;
        history.has_history = state_has_shipped_history(conn)?;
    }
    if let Some(canonical) = open_canonical_state(machine_dir)? {
        if history.recorded.is_none() {
            history.recorded = recorded_import_scope(&canonical)?;
        }
        history.has_history = history.has_history || state_has_shipped_history(&canonical)?;
    }
    Ok(history)
}

/// Settle this machine's import scope at daemon start (and for one-shot
/// `ship`): honour a stored one, otherwise restore or write the first answer.
///
/// - A stored file wins, and is copied into the state database.
/// - No file but a recorded choice: the file was lost, not the choice. Restore it.
/// - No record and the machine already shipped history: it predates scopes and
///   keeps all of it (`legacy`). Silently narrowing it would strand history
///   half-shipped.
/// - No record, no history, connected to a Runtime Host: start from now on, so a
///   stranger's old transcripts are imported only once they say so.
/// - An engine run by hand with no machine state (explicit `--url`/`--token`, as
///   tests and benchmarks do) has no machine to protect and is unrestricted;
///   nothing is written.
pub fn resolve_import_scope(conn: &rusqlite::Connection) -> Result<ImportScope> {
    resolve_import_scope_in(&get_machine_dir()?, conn)
}

pub fn resolve_import_scope_in(
    machine_dir: &Path,
    conn: &rusqlite::Connection,
) -> Result<ImportScope> {
    let adopt = |scope: ImportScope| {
        record_import_scope(conn, &scope);
        remember_scope(machine_dir, &scope);
        scope
    };
    if let Some(scope) = ImportScope::load(machine_dir)? {
        return Ok(adopt(scope));
    }
    let history = machine_history(machine_dir, Some(conn))?;
    if let Some(recorded) = history.recorded {
        tracing::warn!(
            "The import scope file was missing; restored this machine's recorded choice: {}",
            recorded.describe()
        );
        recorded.save_if_absent(machine_dir)?;
        let scope = ImportScope::load(machine_dir)?.unwrap_or(recorded);
        return Ok(adopt(scope));
    }
    if !history.has_history && !machine_is_connected(machine_dir) {
        return Ok(ImportScope::all("unset"));
    }
    Ok(adopt(crate::import_scope::resolve_at_startup(
        machine_dir,
        history.has_history,
    )?))
}

/// What the engine would enforce for this machine, without changing anything:
/// `longhouse machine scope` shows it before anything has been chosen. The bool
/// says whether the scope is stored (a file exists) or would be decided on the
/// agent's first start.
pub fn preview_import_scope(machine_dir: &Path) -> Result<(ImportScope, bool)> {
    if let Some(scope) = ImportScope::load(machine_dir)? {
        return Ok((scope, true));
    }
    let history = machine_history(machine_dir, None)?;
    if let Some(recorded) = history.recorded {
        return Ok((recorded, false));
    }
    Ok(if history.has_history {
        (ImportScope::all("legacy"), false)
    } else if machine_is_connected(machine_dir) {
        (ImportScope::starting(chrono::Utc::now(), "default"), false)
    } else {
        (ImportScope::all("unset"), false)
    })
}

/// Whether this machine has been connected to a Runtime Host: the installer,
/// `longhouse auth` and Desktop setup all leave a stored address.
fn machine_is_connected(machine_dir: &Path) -> bool {
    let state = machine_dir.join("state.json");
    state.exists()
        && load_machine_state(&state)
            .ok()
            .and_then(|state| normalized_state_field(state.runtime_url))
            .is_some()
}

/// Whether the shipper state database has ever tracked a source.
fn state_has_shipped_history(conn: &rusqlite::Connection) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM source_epoch_registry) OR EXISTS(SELECT 1 FROM file_state)",
        [],
        |row| row.get(0),
    )?)
}

/// The database this machine's own service uses, read-only, if it exists. One
/// that exists but cannot be read is an error, not an answer: guessing
/// "history" would import a stranger's whole archive and guessing "none" would
/// narrow a machine that already shipped. The daemon refuses to start on an
/// unreadable state database anyway, so this only stops sooner and says why.
fn open_canonical_state(machine_dir: &Path) -> Result<Option<rusqlite::Connection>> {
    let Some(home) = machine_dir.parent() else {
        return Ok(None);
    };
    let path = home.join("agent").join("longhouse-shipper.db");
    if !path.is_file() {
        return Ok(None);
    }
    let conn =
        rusqlite::Connection::open_with_flags(&path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .with_context(|| {
                format!(
                    "open {} to see whether this machine has shipped before",
                    path.display()
                )
            })?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    Ok(Some(conn))
}

#[cfg(test)]
mod import_scope_tests {
    use super::*;

    fn track_a_source(conn: &rusqlite::Connection) {
        conn.execute(
            "INSERT INTO file_state (path, provider, queued_offset, acked_offset, last_updated)
             VALUES ('/x.jsonl', 'claude', 10, 10, '2026-01-01T00:00:00Z')",
            [],
        )
        .unwrap();
    }

    fn connect_machine(machine: &Path) {
        std::fs::create_dir_all(machine).unwrap();
        std::fs::write(
            machine.join("state.json"),
            "{\"runtime_url\":\"https://you.longhouse.ai\",\"machine_name\":\"laptop\"}",
        )
        .unwrap();
    }

    #[test]
    fn shipped_history_is_read_from_the_state_database() {
        let dir = tempfile::tempdir().unwrap();
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        assert!(!state_has_shipped_history(&conn).unwrap());
        track_a_source(&conn);
        assert!(state_has_shipped_history(&conn).unwrap());
    }

    /// The promise to existing installs: a machine that already shipped history
    /// keeps all of it, and the answer is written down so an emptied database
    /// cannot narrow it later.
    #[test]
    fn a_machine_that_already_shipped_history_keeps_all_of_it() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        track_a_source(&conn);
        let scope = resolve_import_scope_in(&machine, &conn).unwrap();
        assert_eq!(scope.chosen_via, "legacy");
        assert!(scope.is_unrestricted());
        assert!(import_scope_in(&machine).is_unrestricted());
        assert_eq!(resolve_import_scope_in(&machine, &conn).unwrap(), scope);
    }

    /// An engine started with a scratch `--db` beside a real machine directory
    /// must not narrow the machine: the machine's own database decides.
    #[test]
    fn a_scratch_database_does_not_make_a_shipping_machine_look_new() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        std::fs::create_dir_all(dir.path().join("agent")).unwrap();
        let real = crate::state::db::open_db(Some(&dir.path().join("agent/longhouse-shipper.db")))
            .unwrap();
        track_a_source(&real);
        drop(real);
        let scratch = crate::state::db::open_db(Some(&dir.path().join("scratch.db"))).unwrap();
        let scope = resolve_import_scope_in(&machine, &scratch).unwrap();
        assert_eq!(scope.chosen_via, "legacy");
        assert!(scope.is_unrestricted());
    }

    /// Neither guess is safe, so the machine's own unreadable database stops the
    /// resolution instead of importing a stranger's archive or narrowing a
    /// machine that already shipped.
    #[test]
    fn an_unreadable_machine_database_is_an_error_not_a_guess() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        std::fs::create_dir_all(dir.path().join("agent")).unwrap();
        std::fs::write(
            dir.path().join("agent/longhouse-shipper.db"),
            "not a database at all",
        )
        .unwrap();
        let scratch = crate::state::db::open_db(Some(&dir.path().join("scratch.db"))).unwrap();
        assert!(resolve_import_scope_in(&machine, &scratch).is_err());
        assert!(!machine.join("import-scope.json").exists());
    }

    #[test]
    fn a_connected_machine_that_never_shipped_starts_from_now_and_the_engine_enforces_it() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        let scope = resolve_import_scope_in(&machine, &conn).unwrap();
        assert_eq!(scope.chosen_via, "default");
        assert!(!scope.is_unrestricted());
        assert_eq!(import_scope_in(&machine), scope);
        // Written down: a later start reads the same answer.
        assert_eq!(resolve_import_scope_in(&machine, &conn).unwrap(), scope);
    }

    /// Tests, benchmarks and hand runs pass `--url` and `--token` and have no
    /// machine state; there is no machine to protect and nothing is written.
    #[test]
    fn an_engine_run_by_hand_with_no_machine_state_stays_unrestricted_and_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        let scope = resolve_import_scope_in(&machine, &conn).unwrap();
        assert!(scope.is_unrestricted());
        assert!(!machine.join("import-scope.json").exists());
    }

    /// A from-now machine that has shipped its new sessions has history in its
    /// database. If its scope file is then lost, that history must not be read
    /// as "shipped everything before scopes existed".
    #[test]
    fn a_lost_scope_file_is_restored_from_the_state_database_not_read_as_legacy() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        let first = resolve_import_scope_in(&machine, &conn).unwrap();
        assert_eq!(first.chosen_via, "default");
        track_a_source(&conn); // a session the machine shipped after it started
        std::fs::remove_file(machine.join("import-scope.json")).unwrap();

        let again = resolve_import_scope_in(&machine, &conn).unwrap();
        assert_eq!(again, first, "the choice comes back, not `legacy`");
        assert!(again.since.is_some());
        assert!(machine.join("import-scope.json").exists());
    }

    /// The machine's own database answers even when the engine runs on another.
    #[test]
    fn the_recorded_choice_is_found_through_the_machines_own_database() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        std::fs::create_dir_all(dir.path().join("agent")).unwrap();
        let real = crate::state::db::open_db(Some(&dir.path().join("agent/longhouse-shipper.db")))
            .unwrap();
        let recorded = crate::import_scope::ImportScope::starting(chrono::Utc::now(), "prompt");
        record_import_scope(&real, &recorded);
        track_a_source(&real);
        drop(real);
        let scratch = crate::state::db::open_db(Some(&dir.path().join("scratch.db"))).unwrap();
        assert_eq!(
            resolve_import_scope_in(&machine, &scratch).unwrap(),
            recorded
        );
    }

    #[test]
    fn a_daemon_that_loses_its_scope_file_keeps_enforcing_the_last_scope() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        let scope = resolve_import_scope_in(&machine, &conn).unwrap();
        std::fs::remove_file(machine.join("import-scope.json")).unwrap();
        // Not "no scope, import everything".
        assert_eq!(import_scope_in(&machine), scope);
        assert!(restore_lost_scope_file(&machine));
        assert!(machine.join("import-scope.json").exists());
        assert!(
            !restore_lost_scope_file(&machine),
            "nothing to restore twice"
        );
    }

    #[test]
    fn a_database_from_an_engine_that_predates_scopes_records_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let conn = rusqlite::Connection::open(dir.path().join("old.db")).unwrap();
        conn.execute_batch("CREATE TABLE file_state (path TEXT)")
            .unwrap();
        assert!(recorded_import_scope(&conn).unwrap().is_none());
        let fresh = crate::state::db::open_db(Some(&dir.path().join("new.db"))).unwrap();
        assert!(recorded_import_scope(&fresh).unwrap().is_none());
        record_import_scope(&fresh, &crate::import_scope::ImportScope::all("cli"));
        assert!(recorded_import_scope(&fresh)
            .unwrap()
            .unwrap()
            .is_unrestricted());
    }

    #[test]
    fn preview_says_what_the_agent_would_settle_on_without_changing_anything() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        // Nothing connected: a hand-run engine is unrestricted.
        let (scope, stored) = preview_import_scope(&machine).unwrap();
        assert!(scope.is_unrestricted() && !stored);
        // Connected and never shipped: from now on.
        connect_machine(&machine);
        let (scope, stored) = preview_import_scope(&machine).unwrap();
        assert!(!scope.is_unrestricted() && !stored);
        // Already shipped before scopes: all of it.
        std::fs::create_dir_all(dir.path().join("agent")).unwrap();
        let real = crate::state::db::open_db(Some(&dir.path().join("agent/longhouse-shipper.db")))
            .unwrap();
        track_a_source(&real);
        let (scope, stored) = preview_import_scope(&machine).unwrap();
        assert!(scope.is_unrestricted() && !stored);
        assert_eq!(scope.chosen_via, "legacy");
        // A stored file is what is in force.
        crate::import_scope::ImportScope::starting(chrono::Utc::now(), "cli")
            .save(&machine)
            .unwrap();
        let (scope, stored) = preview_import_scope(&machine).unwrap();
        assert!(!scope.is_unrestricted() && stored);
        assert!(!machine.join("agent").exists(), "preview writes nothing");
    }

    #[test]
    fn an_explicit_choice_beats_the_first_run_default() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        connect_machine(&machine);
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        crate::import_scope::ImportScope::all("cli")
            .save(&machine)
            .unwrap();
        let scope = resolve_import_scope_in(&machine, &conn).unwrap();
        assert_eq!(scope.chosen_via, "cli");
        assert!(scope.is_unrestricted());
    }

    #[test]
    fn an_unreadable_scope_closes_history_instead_of_opening_it() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        std::fs::create_dir_all(&machine).unwrap();
        std::fs::write(machine.join("import-scope.json"), "{oops").unwrap();
        let scope = import_scope_in(&machine);
        assert!(!scope.is_unrestricted());
        assert_eq!(scope.chosen_via, "invalid");
        let conn = crate::state::db::open_db(Some(&dir.path().join("s.db"))).unwrap();
        assert!(resolve_import_scope_in(&machine, &conn).is_err());
    }
}
