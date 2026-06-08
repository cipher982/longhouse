//! Multi-provider session file discovery.
//!
//! Discovers session files across Claude, Codex, Antigravity, and legacy Gemini providers.
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
            name: "antigravity",
            root: home.join(".gemini").join("antigravity-cli").join("brain"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "antigravity",
            root: home.join(".gemini").join("antigravity").join("brain"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "opencode",
            root: home.join(".local").join("share").join("opencode"),
            extension: "db",
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
            if is_provider_session_file(provider, path) {
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
        if path.starts_with(&provider.root) && is_provider_session_file(provider, path) {
            return Some(provider.name);
        }
    }
    None
}

pub fn session_path_for_watcher_event(
    path: &std::path::Path,
    providers: &[ProviderConfig],
) -> Option<(PathBuf, &'static str)> {
    for provider in providers {
        if !path.starts_with(&provider.root) {
            continue;
        }
        if provider.name == "opencode" {
            if let Some(db_path) = opencode_database_path_for_event(path) {
                return Some((db_path, provider.name));
            }
            continue;
        }
        if is_provider_session_file(provider, path) {
            return Some((path.to_path_buf(), provider.name));
        }
    }
    None
}

fn opencode_database_path_for_event(path: &Path) -> Option<PathBuf> {
    match path.file_name().and_then(|name| name.to_str()) {
        Some("opencode.db") => Some(path.to_path_buf()),
        Some("opencode.db-wal") | Some("opencode.db-shm") => {
            Some(path.with_file_name("opencode.db"))
        }
        _ => None,
    }
}

fn is_provider_session_file(provider: &ProviderConfig, path: &Path) -> bool {
    if provider.name == "opencode" {
        return path.file_name().and_then(|name| name.to_str()) == Some("opencode.db");
    }
    let extension_matches = path
        .extension()
        .map_or(false, |ext| ext == provider.extension);
    if !extension_matches {
        return false;
    }
    if provider.name == "antigravity" {
        return path.file_name().and_then(|name| name.to_str()) == Some("transcript.jsonl");
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Root of the committed Claude dynamic-workflow fixture tree.
    /// Mirrors the real on-disk layout produced by a `/deep-research` run.
    fn workflow_fixture_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("workflows")
            .join("claude")
    }

    const FIXTURE_SID: &str = "11111111-2222-3333-4444-555555555555";
    const FIXTURE_RUN: &str = "wf_testrun01";

    fn claude_provider_for(root: &Path) -> ProviderConfig {
        ProviderConfig {
            name: "claude",
            root: root.to_path_buf(),
            extension: "jsonl",
        }
    }

    // === Phase 0 characterization: TODAY's behavior for dynamic-workflow files ===
    // These assert the CURRENT (pre-fix) behavior so Phase 1 can invert them.

    #[test]
    fn baseline_workflow_journal_is_discovered_as_claude_session_today() {
        // BASELINE (to be inverted in Phase 1): journal.jsonl is a control ledger,
        // not a transcript, but discovery currently accepts it as a claude session
        // purely because the extension is `.jsonl`.
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let journal = workflow_fixture_root()
            .join(FIXTURE_SID)
            .join("subagents")
            .join("workflows")
            .join(FIXTURE_RUN)
            .join("journal.jsonl");
        assert!(journal.exists(), "fixture journal missing: {}", journal.display());
        assert_eq!(
            provider_for_path(&journal, &providers),
            Some("claude"),
            "BASELINE: journal.jsonl is (wrongly) treated as a claude session today"
        );
    }

    #[test]
    fn workflow_agent_transcript_is_discovered_as_claude_session() {
        // INVARIANT (must stay true across all phases): agent-*.jsonl ARE real
        // subagent transcripts and must always be discovered.
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let agent = workflow_fixture_root()
            .join(FIXTURE_SID)
            .join("subagents")
            .join("workflows")
            .join(FIXTURE_RUN)
            .join("agent-a049eaf15e4dbcae3.jsonl");
        assert!(agent.exists(), "fixture agent file missing: {}", agent.display());
        assert_eq!(provider_for_path(&agent, &providers), Some("claude"));
    }

    #[test]
    fn workflow_main_transcript_is_discovered() {
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let main = workflow_fixture_root().join(format!("{FIXTURE_SID}.jsonl"));
        assert!(main.exists(), "fixture main transcript missing: {}", main.display());
        assert_eq!(provider_for_path(&main, &providers), Some("claude"));
    }

    #[test]
    fn workflow_non_jsonl_sidecars_are_never_discovered() {
        // INVARIANT: .meta.json / .js / .txt sidecars are never sessions.
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let meta = workflow_fixture_root()
            .join(FIXTURE_SID)
            .join("subagents")
            .join("workflows")
            .join(FIXTURE_RUN)
            .join("agent-a049eaf15e4dbcae3.meta.json");
        assert!(meta.exists());
        assert_eq!(provider_for_path(&meta, &providers), None);

        let script = workflow_fixture_root()
            .join(FIXTURE_SID)
            .join("workflows")
            .join("scripts")
            .join(format!("deep-research-{FIXTURE_RUN}.js"));
        assert!(script.exists());
        assert_eq!(provider_for_path(&script, &providers), None);
    }

    #[test]
    fn provider_candidates_use_claude_config_dir_for_claude_root() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");

        let providers = provider_candidates(&home, &claude_root);

        assert_eq!(providers[0].name, "claude");
        assert_eq!(providers[0].root, claude_root.join("projects"));
        assert_eq!(providers[1].root, home.join(".codex").join("sessions"));
        assert_eq!(
            providers[2].root,
            home.join(".gemini").join("antigravity-cli").join("brain")
        );
        assert_eq!(
            providers[3].root,
            home.join(".gemini").join("antigravity").join("brain")
        );
        assert_eq!(
            providers[4].root,
            home.join(".local").join("share").join("opencode")
        );
        assert_eq!(providers[5].root, home.join(".gemini").join("tmp"));
    }

    #[test]
    fn antigravity_provider_ignores_full_transcript_mirror() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root);
        let transcript = home
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain")
            .join("conversation")
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        let full_transcript = transcript.with_file_name("transcript_full.jsonl");

        assert_eq!(
            provider_for_path(&transcript, &providers),
            Some("antigravity")
        );
        assert_eq!(provider_for_path(&full_transcript, &providers), None);
    }

    #[test]
    fn opencode_provider_only_matches_canonical_database_file() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root);
        let db = home
            .join(".local")
            .join("share")
            .join("opencode")
            .join("opencode.db");
        let wal = db.with_file_name("opencode.db-wal");

        assert_eq!(provider_for_path(&db, &providers), Some("opencode"));
        assert_eq!(provider_for_path(&wal, &providers), None);
    }

    #[test]
    fn opencode_watcher_sidecars_map_to_canonical_database_file() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root);
        let db = home
            .join(".local")
            .join("share")
            .join("opencode")
            .join("opencode.db");
        let wal = db.with_file_name("opencode.db-wal");
        let shm = db.with_file_name("opencode.db-shm");

        assert_eq!(
            session_path_for_watcher_event(&db, &providers),
            Some((db.clone(), "opencode"))
        );
        assert_eq!(
            session_path_for_watcher_event(&wal, &providers),
            Some((db.clone(), "opencode"))
        );
        assert_eq!(
            session_path_for_watcher_event(&shm, &providers),
            Some((db, "opencode"))
        );
    }
}
