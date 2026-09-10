//! Multi-provider session file discovery.
//!
//! Discovers session files across Claude, Codex, and Antigravity providers.
//! Replaces the Claude-only `bench::discover_session_files()`.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
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
    existing_provider_roots(configured_provider_roots())
}

/// Configured roots, including stores a provider has not created yet.
pub fn configured_provider_roots() -> Vec<ProviderConfig> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    let home = PathBuf::from(home);
    let claude_root = std::env::var("CLAUDE_CONFIG_DIR")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".claude"));
    let xdg_config_root = std::env::var("XDG_CONFIG_HOME")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));

    let mut providers = provider_candidates(&home, &claude_root, &xdg_config_root);
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    if let Ok(pi_roots) = crate::pi_session::configured_native_session_roots(&cwd) {
        for root in pi_roots {
            if !providers
                .iter()
                .any(|provider| provider.name == "pi" && provider.root == root)
            {
                providers.push(ProviderConfig {
                    name: "pi",
                    root,
                    extension: "jsonl",
                });
            }
        }
    }
    for root in crate::omp_session::configured_session_roots(&cwd) {
        if !providers
            .iter()
            .any(|provider| provider.name == "omp" && provider.root == root)
        {
            providers.push(ProviderConfig {
                name: "omp",
                root,
                extension: "jsonl",
            });
        }
    }
    providers
}

fn existing_provider_roots(candidates: Vec<ProviderConfig>) -> Vec<ProviderConfig> {
    candidates
        .into_iter()
        .filter_map(|mut provider| {
            // Native watchers report physical paths (for example /private/tmp
            // on macOS). Use the same identity for scans and event routing.
            provider.root = provider.root.canonicalize().ok()?;
            Some(provider)
        })
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
        "pi" => Some("pi"),
        "omp" => Some("omp"),
        "cursor" => Some("cursor"),
        "cursor_acp" => Some("cursor_acp"),
        _ => None,
    }
}

fn provider_candidates(
    home: &Path,
    claude_root: &Path,
    xdg_config_root: &Path,
) -> Vec<ProviderConfig> {
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
            name: "pi",
            root: home.join(".longhouse").join("agent").join("pi-console"),
            extension: "jsonl",
        },
        ProviderConfig {
            name: "cursor",
            root: xdg_config_root.join("cursor").join("chats"),
            extension: "db",
        },
        ProviderConfig {
            name: "cursor",
            root: home.join(".cursor").join("chats"),
            extension: "db",
        },
        ProviderConfig {
            name: "cursor",
            root: home.join(".cursor").join("projects"),
            extension: "jsonl",
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
                let provider_inventory =
                    inventory
                        .entry(provider.name)
                        .or_insert_with(|| ProviderSourceInventory {
                            provider: provider.name.to_string(),
                            ..ProviderSourceInventory::default()
                        });
                provider_inventory.source_count = provider_inventory.source_count.saturating_add(1);
                provider_inventory.source_bytes =
                    provider_inventory.source_bytes.saturating_add(source_bytes);
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
    let mut omp_sources_by_native_id: BTreeMap<String, Vec<PathBuf>> = BTreeMap::new();
    for (path, provider, _) in &files {
        if *provider == "omp" {
            if let Ok(header) = crate::omp_session::read_session_header(path) {
                omp_sources_by_native_id
                    .entry(header.native_id)
                    .or_default()
                    .push(path.clone());
            }
        }
    }
    let ambiguous_omp_paths: BTreeSet<PathBuf> = omp_sources_by_native_id
        .values()
        .filter(|paths| paths.iter().collect::<BTreeSet<_>>().len() > 1)
        .flatten()
        .cloned()
        .collect();
    let mut seen = BTreeSet::new();
    let files = files
        .into_iter()
        .filter_map(|(path, provider, _)| {
            if provider == "omp" && ambiguous_omp_paths.contains(&path) {
                return None;
            }
            seen.insert((path.clone(), provider))
                .then_some((path, provider))
        })
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
    if !matches!(
        path.file_name().and_then(|value| value.to_str()),
        Some("opencode.db") | Some("store.db")
    ) {
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
    if let Ok(physical) = path.canonicalize() {
        if physical != path {
            return provider_for_path(&physical, providers);
        }
    }
    None
}

/// Provider hook paths are hints, not permission to enroll a second transcript.
/// Antigravity's transcript.jsonl truncates tool output and stringifies args.
/// Use its full native transcript for discovery, hooks and Console. Resolve
/// summary hints even before the full file arrives; never fall back to lossy data.
pub(crate) fn canonical_transcript_hint<'a>(provider: &str, path: &'a Path) -> Cow<'a, Path> {
    if provider.eq_ignore_ascii_case("antigravity")
        && path
            .file_name()
            .is_some_and(|name| name == "transcript.jsonl")
        && path.parent().is_some_and(|parent| {
            parent.file_name().is_some_and(|name| name == "logs")
                && parent.parent().is_some_and(|system| {
                    system
                        .file_name()
                        .is_some_and(|name| name == ".system_generated")
                })
        })
    {
        Cow::Owned(path.with_file_name("transcript_full.jsonl"))
    } else {
        Cow::Borrowed(path)
    }
}
fn omp_native_id_conflict(path: &Path, providers: &[ProviderConfig]) -> bool {
    let Ok(header) = crate::omp_session::read_session_header(path) else {
        return false;
    };
    let candidate = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    providers
        .iter()
        .filter(|provider| provider.name == "omp")
        .flat_map(|provider| {
            WalkDir::new(&provider.root)
                .max_depth(DISCOVERY_MAX_DEPTH)
                .into_iter()
                .filter_map(Result::ok)
                .map(|entry| entry.path().to_path_buf())
        })
        .filter(|other| {
            other != path
                && other.canonicalize().unwrap_or_else(|_| other.clone()) != candidate
        })
        .filter(|other| {
            providers
                .iter()
                .find(|provider| provider.name == "omp" && other.starts_with(&provider.root))
                .is_some_and(|provider| is_provider_session_file(provider, other))
        })
        .filter_map(|other| crate::omp_session::read_session_header(&other).ok())
        .any(|other| other.native_id == header.native_id)
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
        if provider.name == "omp" {
            if is_provider_session_file(provider, path)
                && !omp_native_id_conflict(path, providers)
            {
                return Some((path.to_path_buf(), provider.name));
            }
            continue;
        }
        if provider.name == "cursor" {
            if let Some(db_path) = cursor_database_path_for_event(path) {
                return Some((db_path, provider.name));
            }
            if is_provider_session_file(provider, path) {
                return Some((path.to_path_buf(), provider.name));
            }
            continue;
        }
        if is_provider_session_file(provider, path) {
            return Some((path.to_path_buf(), provider.name));
        }
    }
    if let Ok(physical) = path.canonicalize() {
        if physical != path {
            return session_path_for_watcher_event(&physical, providers);
        }
    }
    None
}

fn cursor_database_path_for_event(path: &Path) -> Option<PathBuf> {
    match path.file_name().and_then(|name| name.to_str()) {
        Some("store.db") => Some(path.to_path_buf()),
        Some("store.db-wal") => Some(path.with_file_name("store.db")),
        // SQLite readers update WAL shared-memory coordination state. Treating
        // that ephemeral write as source data makes our own read enqueue the
        // database again forever. Durable commits also touch the WAL or DB.
        Some("store.db-shm") => None,
        _ => None,
    }
}

fn opencode_database_path_for_event(path: &Path) -> Option<PathBuf> {
    match path.file_name().and_then(|name| name.to_str()) {
        Some("opencode.db") => Some(path.to_path_buf()),
        Some("opencode.db-wal") => Some(path.with_file_name("opencode.db")),
        Some("opencode.db-shm") => None,
        _ => None,
    }
}

fn is_provider_session_file(provider: &ProviderConfig, path: &Path) -> bool {
    if provider.name == "opencode" {
        return path.file_name().and_then(|name| name.to_str()) == Some("opencode.db");
    }
    if provider.name == "cursor" {
        if path.file_name().and_then(|name| name.to_str()) == Some("store.db") {
            return true;
        }
        return provider.extension == "jsonl"
            && path.extension().and_then(|value| value.to_str()) == Some("jsonl")
            && path
                .components()
                .any(|component| component.as_os_str() == "agent-transcripts");
    }
    if provider.name == "cursor_acp" {
        return path.extension().and_then(|value| value.to_str()) == Some("jsonl");
    }
    if provider.name == "pi" {
        // Both the upstream cwd-encoded session tree and the historical
        // pi-console store are append-only JSONL sources. Keep this predicate
        // deliberately filename-agnostic: Pi permits explicit session paths.
        return path.extension().and_then(|value| value.to_str()) == Some("jsonl");
    }
    if provider.name == "omp" {
        return crate::omp_session::is_session_path(&provider.root, path);
    }
    let extension_matches = path
        .extension()
        .map_or(false, |ext| ext == provider.extension);
    if !extension_matches {
        return false;
    }
    if provider.name == "antigravity" {
        if provider.extension == "json" {
            // Legacy Gemini/Antigravity keeps one rewritten message log at
            // ~/.gemini/tmp/<project>/logs.json. Other JSON files under tmp
            // are configuration or sidecars and are not sessions.
            return path.file_name().and_then(|name| name.to_str()) == Some("logs.json");
        }
        return path.file_name().and_then(|name| name.to_str()) == Some("transcript_full.jsonl");
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

    #[test]
    #[cfg(unix)]
    fn physical_watcher_paths_match_provider_roots_reached_through_aliases() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let root = home.join(".codex/sessions");
        fs::create_dir_all(&root).unwrap();
        let alias = temp.path().join("home-alias");
        std::os::unix::fs::symlink(&home, &alias).unwrap();
        let alias_file = alias.join(".codex/sessions/reply.jsonl");
        fs::write(&alias_file, b"{}\n").unwrap();
        let physical_file = alias_file.canonicalize().unwrap();
        let providers = existing_provider_roots(provider_candidates(
            &alias,
            &alias.join(".claude"),
            &alias.join(".config"),
        ));
        assert_eq!(
            session_path_for_watcher_event(&physical_file, &providers),
            Some((physical_file.clone(), "codex")),
        );
        assert_eq!(provider_for_path(&alias_file, &providers), Some("codex"));
        assert_eq!(
            discover_all_files(&providers),
            vec![(physical_file, "codex")],
        );
    }

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
    fn cursor_agent_transcript_jsonl_is_discovered_beside_store_db() {
        let home = PathBuf::from("/tmp/home");
        let providers =
            provider_candidates(&home, Path::new("/tmp/claude"), Path::new("/tmp/config"));
        let transcript = home
            .join(".cursor/projects/workspace/agent-transcripts")
            .join(
                "019c638d-0000-0000-0000-000000000099/019c638d-0000-0000-0000-000000000099.jsonl",
            );
        assert_eq!(provider_for_path(&transcript, &providers), Some("cursor"));
        assert_eq!(
            session_path_for_watcher_event(&transcript, &providers),
            Some((transcript, "cursor"))
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
        let xdg_config_root = PathBuf::from("/tmp/custom-config");

        let providers = provider_candidates(&home, &claude_root, &xdg_config_root);

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
        assert_eq!(
            providers[5].root,
            home.join(".longhouse").join("agent").join("pi-console")
        );
        assert_eq!(
            providers[6].root,
            xdg_config_root.join("cursor").join("chats")
        );
        assert_eq!(providers[7].root, home.join(".cursor").join("chats"));
        assert_eq!(providers[8].root, home.join(".cursor").join("projects"));
        assert_eq!(
            providers[9].root,
            home.join(".longhouse")
                .join("agent")
                .join("cursor-acp-source")
        );
        assert_eq!(providers[10].root, home.join(".gemini").join("tmp"));
        assert!(providers
            .iter()
            .all(|provider| canonical_provider_name(provider.name) == Some(provider.name)));
        assert_eq!(canonical_provider_name("gemini"), Some("antigravity"));
        assert_eq!(canonical_provider_name("unknown"), None);
    }

    #[test]
    fn omp_candidate_is_profile_aware_and_only_accepts_its_cwd_bucket() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(xdg_data.join("omp/sessions")).unwrap();
        std::fs::create_dir_all(xdg_data.join("omp/profiles/work/sessions")).unwrap();
        std::fs::create_dir_all(xdg_data.join("omp/profiles/other/sessions")).unwrap();
        let _home =
            temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
                temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                    temp_env::with_var("PI_CONFIG_DIR", Some("/pi/config"), || {
                        temp_env::with_var("PI_CODING_AGENT_DIR", Some("/pi/agent"), || {
                            temp_env::with_var("PI_PROFILE", Some("pi-profile"), || {
                                temp_env::with_var("OMP_PROFILE", Some("work"), || {
                                    let roots = crate::omp_session::configured_session_roots(&cwd);
                                    assert!(roots
                                        .contains(&xdg_data.join("omp/profiles/work/sessions")));
                                    assert!(roots
                                        .contains(&xdg_data.join("omp/profiles/other/sessions")));
                                    assert!(roots.contains(&std::path::PathBuf::from(
                                        "/pi/config/profiles/work/agent/sessions",
                                    )));
                                    assert!(!roots.iter().any(|root| root.starts_with("/pi/agent")));
                                })
                            })
                        })
                    })
                })
            });
        let root = xdg_data.join("omp/profiles/work/sessions");
        std::fs::create_dir_all(root.join("-tmp-workspace")).unwrap();
        std::fs::create_dir_all(root.join("other").join("nested")).unwrap();
        let valid = root.join("-tmp-workspace").join("session.jsonl");
        let wrong_bucket = root.join("other").join("nested").join("session.jsonl");
        std::fs::write(
            &valid,
            b"{\"type\":\"session\",\"id\":\"native-valid\",\"cwd\":\"/tmp/workspace\"}\n",
        )
        .unwrap();
        std::fs::write(
            &wrong_bucket,
            b"{\"type\":\"session\",\"id\":\"native-wrong\",\"cwd\":\"/tmp/other\"}\n",
        )
        .unwrap();
        assert!(crate::omp_session::is_session_path(&root, &valid));
        assert!(!crate::omp_session::is_session_path(&root, &wrong_bucket));
    }

    #[test]
    fn omp_owned_session_override_is_additive_and_ignores_profile() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        std::fs::create_dir_all(&cwd).unwrap();
        let _home = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", None::<&str>, || {
                temp_env::with_var("OMP_PROFILE", Some("work"), || {
                    temp_env::with_var("LONGHOUSE_OMP_SESSION_DIR", Some("omp-sessions"), || {
                        let roots = crate::omp_session::configured_session_roots(&cwd);
                        assert!(roots.contains(&cwd.join("omp-sessions")));
                         assert!(!roots.contains(&home.path().join(".local/share/omp/sessions")));
                        assert!(roots.contains(&home.path().join(".omp/agent/sessions")));
                    })
                })
            })
        });
    }

    #[test]
    fn omp_default_profile_uses_upstream_xdg_sessions_layout() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        std::fs::create_dir_all(xdg_data.join("omp")).unwrap();
        std::fs::create_dir_all(&cwd).unwrap();

        let roots = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                temp_env::with_var("OMP_PROFILE", None::<&str>, || {
                    temp_env::with_var("LONGHOUSE_OMP_DATA_DIR", None::<&str>, || {
                        temp_env::with_var("LONGHOUSE_OMP_CONFIG_DIR", None::<&str>, || {
                            crate::omp_session::configured_session_roots(&cwd)
                        })
                    })
                })
            })
        });

        assert!(roots.contains(&xdg_data.join("omp/sessions")));
    }

    #[test]
    fn non_omp_symlink_behavior_remains_unchanged() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("claude");
        fs::create_dir_all(&root).unwrap();
        let real = root.join("real.jsonl");
        let link = root.join("link.jsonl");
        fs::write(&real, b"{}\n").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&real, &link).unwrap();

        let provider = ProviderConfig {
            name: "claude",
            root,
            extension: "jsonl",
        };
        #[cfg(unix)]
        assert_eq!(discover_all_files(&[provider]).len(), 2);
    }
    #[test]
    fn pi_native_source_is_discovered_once_without_managed_claim() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("pi/agent/sessions");
        let bucket = root.join("--tmp-project--");
        std::fs::create_dir_all(&bucket).unwrap();
        let source = bucket.join("2026-09-07T00-00-00-000Z_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl");
        std::fs::write(
            &source,
            concat!(
                "{\"type\":\"session\",\"version\":3,\"id\":\"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\",\"cwd\":\"/tmp/project\"}\n",
                "{\"type\":\"message\",\"id\":\"user-1\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"shadow source\"}]}}\n"
            ),
        )
        .unwrap();
        let provider = ProviderConfig {
            name: "pi",
            root,
            extension: "jsonl",
        };

        assert_eq!(discover_all_files(&[provider]), vec![(source, "pi")]);
    }


    #[test]
    fn omp_native_source_is_discovered_once() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("xdg/omp/sessions");
        let bucket = root.join("-tmp-workspace");
        std::fs::create_dir_all(&bucket).unwrap();
        std::fs::write(
            bucket.join("session.jsonl"),
            b"{\"type\":\"session\",\"id\":\"opaque\",\"cwd\":\"/tmp/workspace\"}\n",
        )
        .unwrap();
        let provider = ProviderConfig {
            name: "omp",
            root,
            extension: "jsonl",
        };
        assert_eq!(
            discover_all_files(&[provider]),
            vec![(bucket.join("session.jsonl"), "omp")]
        );
    }

    #[test]
    fn omp_duplicate_native_ids_are_refused_instead_of_merged() {
        let dir = tempfile::tempdir().unwrap();
        let first_root = dir.path().join("first/sessions");
        let second_root = dir.path().join("second/sessions");
        let first_bucket = first_root.join("--workspace--");
        let second_bucket = second_root.join("--workspace--");
        std::fs::create_dir_all(&first_bucket).unwrap();
        std::fs::create_dir_all(&second_bucket).unwrap();
        let record = b"{\"type\":\"session\",\"id\":\"same-native\",\"cwd\":\"/workspace\"}\n";
        std::fs::write(first_bucket.join("first.jsonl"), record).unwrap();
        std::fs::write(second_bucket.join("second.jsonl"), record).unwrap();
        let providers = vec![
            ProviderConfig {
                name: "omp",
                root: first_root,
                extension: "jsonl",
            },
            ProviderConfig {
                name: "omp",
                root: second_root,
                extension: "jsonl",
            },
        ];
        assert!(discover_all_files(&providers).is_empty());
    }

    #[test]
    fn antigravity_provider_ignores_truncated_transcript_mirror() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root, &home.join(".config"));
        let transcript = home
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain")
            .join("conversation")
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        let full_transcript = transcript.with_file_name("transcript_full.jsonl");

        assert_eq!(provider_for_path(&transcript, &providers), None);
        assert_eq!(
            provider_for_path(&full_transcript, &providers),
            Some("antigravity")
        );
    }

    #[test]
    fn antigravity_legacy_logs_json_is_discovered_but_other_tmp_json_is_not() {
        let home = PathBuf::from("/tmp/home");
        let providers = provider_candidates(
            &home,
            Path::new("/tmp/custom-claude"),
            &home.join(".config"),
        );
        let logs = home
            .join(".gemini")
            .join("tmp")
            .join("project-hash")
            .join("logs.json");
        let config = logs.with_file_name("settings.json");

        assert_eq!(provider_for_path(&logs, &providers), Some("antigravity"));
        assert_eq!(provider_for_path(&config, &providers), None);
    }

    #[test]
    fn opencode_provider_only_matches_canonical_database_file() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root, &home.join(".config"));
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
    fn opencode_watcher_maps_durable_files_but_ignores_shared_memory() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root, &home.join(".config"));
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
        assert_eq!(session_path_for_watcher_event(&shm, &providers), None);
    }

    #[test]
    fn cursor_provider_only_matches_the_canonical_store_database() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root, &home.join(".config"));
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
    fn cursor_watcher_maps_durable_files_but_ignores_shared_memory() {
        let home = PathBuf::from("/tmp/home");
        let claude_root = PathBuf::from("/tmp/custom-claude");
        let providers = provider_candidates(&home, &claude_root, &home.join(".config"));
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
        assert_eq!(session_path_for_watcher_event(&shm, &providers), None);
    }
}
