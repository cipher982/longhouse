//! Multi-provider session file discovery.
//!
//! Discovers session files across Claude, Codex, and Gemini providers.
//! Replaces the Claude-only `bench::discover_session_files()`.

use std::path::PathBuf;

use walkdir::WalkDir;

/// Configuration for a session provider.
pub struct ProviderConfig {
    pub name: &'static str,
    pub root: PathBuf,
    pub extension: &'static str,
}

/// Get all known provider configurations.
///
/// Returns providers whose root directories exist on this system.
pub fn get_providers() -> Vec<ProviderConfig> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    let home = PathBuf::from(home);

    let candidates = vec![
        ProviderConfig {
            name: "claude",
            root: home.join(".claude").join("projects"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "codex",
            root: home.join(".codex").join("sessions"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "gemini",
            root: home.join(".gemini").join("tmp"),
            extension: "json",
        },
    ];

    candidates
        .into_iter()
        .filter(|p| p.root.exists())
        .collect()
}

/// Discover all session files across all providers.
///
/// Returns `(path, provider_name)` tuples sorted by modification time (newest first).
pub fn discover_all_files(providers: &[ProviderConfig]) -> Vec<(PathBuf, &'static str)> {
    let mut files = Vec::new();

    for provider in providers {
        for entry in WalkDir::new(&provider.root)
            .follow_links(false)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if path.extension().map_or(false, |ext| ext == provider.extension) {
                if let Ok(meta) = path.metadata() {
                    if meta.len() > 0 {
                        files.push((path.to_path_buf(), provider.name));
                    }
                }
            }
        }
    }

    // Sort by modification time descending (newest first)
    files.sort_by(|a, b| {
        let ma = std::fs::metadata(&a.0)
            .and_then(|m| m.modified())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        let mb = std::fs::metadata(&b.0)
            .and_then(|m| m.modified())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        mb.cmp(&ma)
    });

    files
}

/// Determine the provider name for a file path based on registered providers.
///
/// Uses `Path::starts_with` for correct component-level matching
/// (avoids false positives like `projects2/` matching `projects/`).
pub fn provider_for_path(path: &std::path::Path, providers: &[ProviderConfig]) -> Option<&'static str> {
    for provider in providers {
        if path.starts_with(&provider.root) {
            return Some(provider.name);
        }
    }
    None
}
