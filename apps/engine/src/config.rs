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
            max_retries_429: 3,
            base_backoff_seconds: 1.0,
        }
    }
}

impl ShipperConfig {
    /// Load config from standard file locations + env vars.
    pub fn from_env() -> Result<Self> {
        let claude_dir = get_claude_dir()?;
        let mut config = Self::default();

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
        self
    }
}

/// Resolve `~/.claude/` or `CLAUDE_CONFIG_DIR`.
fn get_claude_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("CLAUDE_CONFIG_DIR") {
        return Ok(PathBuf::from(dir));
    }
    let home = std::env::var("HOME").context("HOME not set")?;
    Ok(PathBuf::from(home).join(".claude"))
}
