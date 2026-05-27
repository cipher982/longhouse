//! Shipper configuration.
//!
//! Reads canonical machine state from `~/.longhouse/machine/state.json` and the
//! device token from `~/.longhouse/machine/device-token`.

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
    pub max_retries_429: u32,
    pub base_backoff_seconds: f64,
    /// Human-readable machine label (set by user during `longhouse connect --install`).
    /// Stored in `~/.longhouse/machine/state.json`. Defaults to hostname.
    pub machine_name: String,
}

#[derive(Debug, Default, Deserialize)]
struct MachineStateFile {
    runtime_url: Option<String>,
    machine_name: Option<String>,
}

impl Default for ShipperConfig {
    fn default() -> Self {
        Self {
            api_url: "http://localhost:8080".to_string(),
            api_token: None,
            db_path: None,
            workers: num_cpus::get(),
            max_batch_bytes: 50 * 1024 * 1024, // 50 MB
            timeout_seconds: 60,
            max_retries_429: 5,
            base_backoff_seconds: 1.0,
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
