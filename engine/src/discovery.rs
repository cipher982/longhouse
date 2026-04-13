//! Multi-provider session file discovery.
//!
//! Discovers session files across Claude, Codex, and Gemini providers.
//! Replaces the Claude-only `bench::discover_session_files()`.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use walkdir::WalkDir;

const DISCOVERY_MAX_DEPTH: usize = 6;

/// Configuration for a session provider.
#[derive(Clone)]
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
    let claude_root = std::env::var("CLAUDE_CONFIG_DIR")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".claude"));

    provider_candidates(&home, &claude_root)
        .into_iter()
        .filter(|p| p.root.exists())
        .collect()
}

fn provider_candidates(home: &Path, claude_root: &Path) -> Vec<ProviderConfig> {
    vec![
        ProviderConfig {
            name: "claude",
            root: claude_root.join("projects"),
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
    ]
}

/// Discover all session files across all providers.
///
/// Returns `(path, provider_name)` tuples sorted by modification time (newest first).
pub fn discover_all_files(providers: &[ProviderConfig]) -> Vec<(PathBuf, &'static str)> {
    let mut files: Vec<(PathBuf, &'static str, SystemTime)> = Vec::new();

    for provider in providers {
        // Provider transcript layouts are shallow; bounding depth keeps fallback
        // discovery from wandering into unrelated or pathological directory trees.
        for entry in WalkDir::new(&provider.root)
            .follow_links(false)
            .max_depth(DISCOVERY_MAX_DEPTH)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if path
                .extension()
                .map_or(false, |ext| ext == provider.extension)
            {
                if let Ok(meta) = path.metadata() {
                    if meta.len() > 0 {
                        let modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
                        files.push((path.to_path_buf(), provider.name, modified));
                    }
                }
            }
        }
    }

    files.sort_by(|a, b| b.2.cmp(&a.2));
    files
        .into_iter()
        .map(|(path, provider, _)| (path, provider))
        .collect()
}

/// Determine the provider name for a file path based on registered providers.
///
/// Uses `Path::starts_with` for correct component-level matching
/// (avoids false positives like `projects2/` matching `projects/`).
pub fn provider_for_path(
    path: &std::path::Path,
    providers: &[ProviderConfig],
) -> Option<&'static str> {
    for provider in providers {
        if path.starts_with(&provider.root) {
            return Some(provider.name);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_candidates_use_claude_config_dir_for_claude_root() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");

        let providers = provider_candidates(&home, &claude_root);

        assert_eq!(providers[0].name, "claude");
        assert_eq!(providers[0].root, claude_root.join("projects"));
        assert_eq!(providers[1].root, home.join(".codex").join("sessions"));
        assert_eq!(providers[2].root, home.join(".gemini").join("tmp"));
    }
}
