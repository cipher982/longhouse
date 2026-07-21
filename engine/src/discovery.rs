//! Multi-provider session file discovery.
//!
//! Discovers session files across Claude, Codex, and Antigravity providers.
//! Replaces the Claude-only `bench::discover_session_files()`.

use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use walkdir::WalkDir;

use crate::state::source_inventory::{ProviderSourceInventory, SourceInventoryObservation};

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

/// Resolve persisted/wire provider names to the canonical static names used by
/// discovery and the scheduler. Keep aliases here so retry replay cannot drift
/// from the providers accepted by fresh discovery.
pub fn canonical_provider_name(provider: &str) -> Option<&'static str> {
    match provider {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "antigravity" | "gemini" => Some("antigravity"),
        "opencode" => Some("opencode"),
        "cursor" => Some("cursor"),
        "cursor_acp" => Some("cursor_acp"),
        _ => None,
    }
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
            name: "cursor",
            root: home.join(".cursor").join("chats"),
            extension: "db",
        },
        ProviderConfig {
            name: "cursor_acp",
            root: home
                .join(".longhouse")
                .join("agent")
                .join("cursor-acp-source"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "antigravity",
            root: home.join(".gemini").join("tmp"),
            extension: "json",
        },
    ]
}

/// Discover all session files across all providers.
///
/// Returns `(path, provider_name)` tuples sorted by modification time (newest first).
pub fn discover_all_files(providers: &[ProviderConfig]) -> Vec<(PathBuf, &'static str)> {
    discover_all_files_with_inventory(providers).files
}

pub struct DiscoveryScan {
    pub files: Vec<(PathBuf, &'static str)>,
    pub inventory: SourceInventoryObservation,
}

/// Discover sources and build a path-free provider inventory in one traversal.
///
/// SQLite-backed providers include the main database and its WAL in physical
/// footprint bytes. SHM files are transient mappings and are intentionally not
/// counted. The inventory never contains a local path or filename.
pub fn discover_all_files_with_inventory(providers: &[ProviderConfig]) -> DiscoveryScan {
    let started = Instant::now();
    let mut files: Vec<(PathBuf, &'static str, SystemTime)> = Vec::new();
    let mut inventory: BTreeMap<&'static str, ProviderSourceInventory> = BTreeMap::new();
    let mut scan_error_count = 0_u64;

    for provider in providers {
        // Provider transcript layouts are shallow; bounding depth keeps fallback
        // discovery from wandering into unrelated or pathological directory trees.
        for entry in WalkDir::new(&provider.root)
            .follow_links(false)
            .max_depth(DISCOVERY_MAX_DEPTH)
            .into_iter()
        {
            let entry = match entry {
                Ok(entry) => entry,
                Err(_) => {
                    scan_error_count = scan_error_count.saturating_add(1);
                    continue;
                }
            };
            let path = entry.path();
            if is_provider_session_file(provider, path) {
                let meta = match path.metadata() {
                    Ok(meta) => meta,
                    Err(_) => {
                        scan_error_count = scan_error_count.saturating_add(1);
                        continue;
                    }
                };
                if meta.len() == 0 {
                    continue;
                }
                let modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
                let source_bytes = meta.len();
                let (wal_bytes, footprint_errors) = source_wal_bytes(provider, path);
                let footprint_bytes = source_bytes.saturating_add(wal_bytes);
                scan_error_count = scan_error_count.saturating_add(footprint_errors);
                let modified_at_ms = system_time_ms(modified);
                let provider_inventory = inventory
                    .entry(provider.name)
                    .or_insert_with(|| ProviderSourceInventory {
                        provider: provider.name.to_string(),
                        ..ProviderSourceInventory::default()
                    });
                provider_inventory.source_count =
                    provider_inventory.source_count.saturating_add(1);
                provider_inventory.source_bytes = provider_inventory
                    .source_bytes
                    .saturating_add(source_bytes);
                provider_inventory.wal_bytes =
                    provider_inventory.wal_bytes.saturating_add(wal_bytes);
                provider_inventory.footprint_bytes = provider_inventory
                    .footprint_bytes
                    .saturating_add(footprint_bytes);
                provider_inventory.oldest_modified_at_ms = Some(
                    provider_inventory
                        .oldest_modified_at_ms
                        .map_or(modified_at_ms, |current| current.min(modified_at_ms)),
                );
                provider_inventory.newest_modified_at_ms = Some(
                    provider_inventory
                        .newest_modified_at_ms
                        .map_or(modified_at_ms, |current| current.max(modified_at_ms)),
                );
                files.push((path.to_path_buf(), provider.name, modified));
            }
        }
    }

    files.sort_by(|a, b| b.2.cmp(&a.2));
    let files = files
        .into_iter()
        .map(|(path, provider, _)| (path, provider))
        .collect::<Vec<_>>();
    let providers = inventory.into_values().collect::<Vec<_>>();
    let source_count = providers.iter().map(|item| item.source_count).sum();
    let source_bytes = providers.iter().map(|item| item.source_bytes).sum();
    let wal_bytes = providers.iter().map(|item| item.wal_bytes).sum();
    let footprint_bytes = providers.iter().map(|item| item.footprint_bytes).sum();
    DiscoveryScan {
        files,
        inventory: SourceInventoryObservation {
            observed_at: chrono::Utc::now().to_rfc3339(),
            scan_duration_ms: started.elapsed().as_millis() as u64,
            scan_error_count,
            source_count,
            source_bytes,
            wal_bytes,
            footprint_bytes,
            providers,
        },
    }
}

fn source_wal_bytes(provider: &ProviderConfig, path: &Path) -> (u64, u64) {
    if provider.name != "opencode" && provider.name != "cursor" {
        return (0, 0);
    }
    let Some(file_name) = path.file_name().and_then(|value| value.to_str()) else {
        return (0, 1);
    };
    let wal_path = path.with_file_name(format!("{file_name}-wal"));
    match wal_path.metadata() {
        Ok(metadata) => (metadata.len(), 0),
        Err(error) if error.kind() == ErrorKind::NotFound => (0, 0),
        Err(_) => (0, 1),
    }
}

fn system_time_ms(value: SystemTime) -> i64 {
    value
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(i64::MAX as u128) as i64)
        .unwrap_or(0)
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
        if provider.name == "cursor" {
            if let Some(db_path) = cursor_database_path_for_event(path) {
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

fn cursor_database_path_for_event(path: &Path) -> Option<PathBuf> {
    match path.file_name().and_then(|name| name.to_str()) {
        Some("store.db") => Some(path.to_path_buf()),
        Some("store.db-wal") | Some("store.db-shm") => Some(path.with_file_name("store.db")),
        _ => None,
    }
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
    if provider.name == "cursor" {
        return path.file_name().and_then(|name| name.to_str()) == Some("store.db");
    }
    if provider.name == "cursor_acp" {
        return path.extension().and_then(|value| value.to_str()) == Some("jsonl");
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
    if provider.name == "claude" && is_workflow_journal(path) {
        // Dynamic-workflow runs write a `journal.jsonl` control ledger alongside
        // the real `agent-*.jsonl` subagent transcripts. It carries only
        // {type:"started"|"result"} bookkeeping lines (no role events), so it is
        // not a session — shipping it just pollutes the timeline with an empty
        // session row. The sibling agent transcripts are still discovered.
        return false;
    }
    true
}

/// True for a Claude dynamic-workflow `journal.jsonl` ledger, i.e. a file named
/// `journal.jsonl` living under a `.../subagents/workflows/<run>/` directory.
fn is_workflow_journal(path: &Path) -> bool {
    if path.file_name().and_then(|name| name.to_str()) != Some("journal.jsonl") {
        return false;
    }
    let parent = match path.parent().and_then(|p| p.parent()) {
        Some(p) => p,
        None => return false,
    };
    parent.file_name().and_then(|name| name.to_str()) == Some("workflows")
        && parent
            .parent()
            .and_then(|p| p.file_name())
            .and_then(|name| name.to_str())
            == Some("subagents")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

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

    #[test]
    fn inventory_aggregates_without_leaking_paths_and_counts_sqlite_wal() {
        let tmp = tempfile::tempdir().unwrap();
        let claude_root = tmp.path().join("private-claude-path");
        let opencode_root = tmp.path().join("private-opencode-path");
        fs::create_dir_all(&claude_root).unwrap();
        fs::create_dir_all(&opencode_root).unwrap();
        fs::write(claude_root.join("session.jsonl"), vec![b'x'; 7]).unwrap();
        fs::write(opencode_root.join("opencode.db"), vec![b'x'; 11]).unwrap();
        fs::write(opencode_root.join("opencode.db-wal"), vec![b'x'; 13]).unwrap();
        fs::write(opencode_root.join("opencode.db-shm"), vec![b'x'; 17]).unwrap();
        let providers = vec![
            claude_provider_for(&claude_root),
            ProviderConfig {
                name: "opencode",
                root: opencode_root,
                extension: "db",
            },
        ];

        let scan = discover_all_files_with_inventory(&providers);
        assert_eq!(scan.files.len(), 2);
        assert_eq!(scan.inventory.source_count, 2);
        assert_eq!(scan.inventory.source_bytes, 18);
        assert_eq!(scan.inventory.wal_bytes, 13);
        assert_eq!(scan.inventory.footprint_bytes, 31);
        assert_eq!(scan.inventory.scan_error_count, 0);
        assert_eq!(scan.inventory.providers.len(), 2);
        let encoded = serde_json::to_string(&scan.inventory.providers).unwrap();
        assert!(!encoded.contains(tmp.path().to_string_lossy().as_ref()));
        assert!(!encoded.contains("session.jsonl"));
        assert!(!encoded.contains("opencode.db"));
    }

    // === Phase 0 characterization: TODAY's behavior for dynamic-workflow files ===
    // These assert the CURRENT (pre-fix) behavior so Phase 1 can invert them.

    #[test]
    fn workflow_journal_is_not_discovered_as_session() {
        // Phase 1: journal.jsonl is a control ledger, not a transcript. It must
        // never be discovered as a claude session (otherwise it pollutes the
        // timeline with an empty session). The sibling agent transcripts are
        // still discovered (see the test below).
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let journal = workflow_fixture_root()
            .join(FIXTURE_SID)
            .join("subagents")
            .join("workflows")
            .join(FIXTURE_RUN)
            .join("journal.jsonl");
        assert!(
            journal.exists(),
            "fixture journal missing: {}",
            journal.display()
        );
        assert_eq!(
            provider_for_path(&journal, &providers),
            None,
            "journal.jsonl must not be treated as a session"
        );
        // The watcher-event mapping must agree with discovery.
        assert_eq!(session_path_for_watcher_event(&journal, &providers), None);
    }

    #[test]
    fn non_workflow_journal_jsonl_is_still_a_session() {
        // A file literally named journal.jsonl that is NOT under
        // subagents/workflows/<run>/ is a normal session and stays discoverable.
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let path = workflow_fixture_root().join("journal.jsonl");
        assert!(is_provider_session_file(&providers[0], &path));
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
        assert!(
            agent.exists(),
            "fixture agent file missing: {}",
            agent.display()
        );
        assert_eq!(provider_for_path(&agent, &providers), Some("claude"));
    }

    #[test]
    fn workflow_main_transcript_is_discovered() {
        let providers = vec![claude_provider_for(&workflow_fixture_root())];
        let main = workflow_fixture_root().join(format!("{FIXTURE_SID}.jsonl"));
        assert!(
            main.exists(),
            "fixture main transcript missing: {}",
            main.display()
        );
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
        assert_eq!(providers[5].root, home.join(".cursor").join("chats"));
        assert_eq!(
            providers[6].root,
            home.join(".longhouse")
                .join("agent")
                .join("cursor-acp-source")
        );
        assert_eq!(providers[7].root, home.join(".gemini").join("tmp"));
        assert!(providers
            .iter()
            .all(|provider| canonical_provider_name(provider.name) == Some(provider.name)));
        assert_eq!(canonical_provider_name("gemini"), Some("antigravity"));
        assert_eq!(canonical_provider_name("unknown"), None);
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

    #[test]
    fn cursor_provider_only_matches_the_canonical_store_database() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root);
        let db = home
            .join(".cursor")
            .join("chats")
            .join("chat-a")
            .join("store.db");
        let wal = db.with_file_name("store.db-wal");

        assert_eq!(provider_for_path(&db, &providers), Some("cursor"));
        assert_eq!(provider_for_path(&wal, &providers), None);
    }

    #[test]
    fn cursor_watcher_sidecars_map_to_canonical_store_database() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root);
        let db = home
            .join(".cursor")
            .join("chats")
            .join("chat-a")
            .join("store.db");
        let wal = db.with_file_name("store.db-wal");
        let shm = db.with_file_name("store.db-shm");

        assert_eq!(
            session_path_for_watcher_event(&db, &providers),
            Some((db.clone(), "cursor"))
        );
        assert_eq!(
            session_path_for_watcher_event(&wal, &providers),
            Some((db.clone(), "cursor"))
        );
        assert_eq!(
            session_path_for_watcher_event(&shm, &providers),
            Some((db, "cursor"))
        );
    }
}
