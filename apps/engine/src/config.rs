//! Shipper configuration.
//!
//! Reads API URL and token from `~/.claude/longhouse-url` and
//! `~/.claude/longhouse-device-token` (same files as the Python shipper).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

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
    /// Stored in `~/.claude/longhouse-machine-name`. Defaults to hostname.
    pub machine_name: String,
}

impl Default for ShipperConfig {
    fn default() -> Self {
        Self {
            api_url: "http://localhost:8080".to_string(),
            api_token: None,
            db_path: None,
            workers: num_cpus::get(),
            max_batch_bytes: 5 * 1024 * 1024, // 5 MB
            timeout_seconds: 60,
            max_retries_429: 5,
            base_backoff_seconds: 1.0,
            machine_name: default_machine_name(),
        }
    }
}

impl ShipperConfig {
    /// Load config from standard file locations + env vars.
    pub fn from_env() -> Result<Self> {
        let claude_dir = get_claude_dir()?;
        let mut config = Self::default();

        // Read machine name from file (set during --install)
        let machine_name_path = claude_dir.join("longhouse-machine-name");
        if machine_name_path.exists() {
            if let Ok(name) = std::fs::read_to_string(&machine_name_path) {
                let name = name.trim().to_string();
                if !name.is_empty() {
                    config.machine_name = name;
                }
            }
        }

        // Read URL from file
        let url_path = claude_dir.join("longhouse-url");
        if url_path.exists() {
            let url = std::fs::read_to_string(&url_path)
                .with_context(|| format!("reading {}", url_path.display()))?
                .trim()
                .to_string();
            if !url.is_empty() {
                config.api_url = url;
            }
        }

        // Read token from file
        let token_path = claude_dir.join("longhouse-device-token");
        if token_path.exists() {
            let token = std::fs::read_to_string(&token_path)
                .with_context(|| format!("reading {}", token_path.display()))?
                .trim()
                .to_string();
            if !token.is_empty() {
                config.api_token = Some(token);
            }
        }

        // Env var override for token
        if let Ok(token) = std::env::var("AGENTS_API_TOKEN") {
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

/// Resolve `~/.claude/` or `CLAUDE_CONFIG_DIR`.
fn get_claude_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("CLAUDE_CONFIG_DIR") {
        return Ok(PathBuf::from(dir));
    }
    let home = std::env::var("HOME").context("HOME not set")?;
    Ok(PathBuf::from(home).join(".claude"))
}
