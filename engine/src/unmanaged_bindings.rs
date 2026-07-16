//! Phase 5b of `docs/specs/session-liveness-honesty.md`: machine-observed
//! bindings between unmanaged provider-CLI processes and their JSONL
//! transcripts.
//!
//! The scanner answers one question per unmanaged session the user has open
//! locally: *is a `claude` / `codex` / `antigravity` process actually holding
//! this transcript file right now?*
//!
//! That ground truth lets the Runtime Host mark a session's
//! `host_state=online`/`offline` honestly (Phase 5c) and lets Phase 6
//! promote `lifecycle=closed` when the process is confirmed gone.
//!
//! Algorithm (macOS- and Linux-friendly via `ps` + `lsof`):
//!
//!   1. Enumerate recent transcript files under the existing provider
//!      roots via `discovery::discover_all_files`, filtered by mtime.
//!   2. Enumerate candidate provider-CLI processes with
//!      `ps -axo pid=,lstart=,command=`. Filter by command basename
//!      (`claude`, `codex`, `agy`, `opencode`) plus the stock Node-backed
//!      launcher shapes (`node .../codex`, `node .../opencode`, etc.) - never
//!      `longhouse-*` wrappers (those are managed sessions and get their
//!      own lease surface).
//!   3. For each candidate pid, ask `lsof -F n -p <pid>` which regular
//!      files it has open, and look for transcript paths.
//!   4. Emit one [`UnmanagedSessionBinding`] per `(provider,
//!      provider_session_id)` with `(pid, process_start_time)` as the
//!      liveness identity.
//!
//! Kept behind a `ProcessScanner` trait so tests can inject fixtures
//! without shelling out.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use chrono::DateTime;
use chrono::Utc;

use crate::discovery;
use crate::heartbeat::UnmanagedSessionBinding;
#[cfg(test)]
use crate::process_identity::parse_lstart;
use crate::process_identity::parse_process_fact;
use crate::state::unmanaged_process_binding::UnmanagedProcessBindingStore;

/// Cap the number of bindings emitted per heartbeat. Provider roots with
/// thousands of stale transcripts shouldn't inflate the payload.
const MAX_BINDINGS: usize = 128;

/// Only consider transcripts modified within this window. A well-kept
/// user's recent-unmanaged-sessions set is small; older files are noise
/// for liveness decisions.
const TRANSCRIPT_MTIME_WINDOW: chrono::Duration = chrono::Duration::hours(24);

/// Merge fd-scanned bindings with hook-observed unmanaged provider bindings.
///
/// Claude does not reliably keep its JSONL transcript open between writes, so
/// the fd scanner can see the process but miss the session identity. The Claude
/// hook writes the provider pid + session id locally; this function validates
/// that the same pid/start-time is still alive before emitting the normal
/// heartbeat binding shape.
pub fn collect_unmanaged_session_bindings_with_store(
    conn: &rusqlite::Connection,
    machine_id: &str,
    now: DateTime<Utc>,
    excluded_managed_pids: &HashSet<u32>,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    let scanner = SystemScanner;
    let processes = scanner.list_processes()?;
    let provider_processes =
        unresolved_unmanaged_processes(processes, excluded_managed_pids);
    let store = UnmanagedProcessBindingStore::new(conn);
    if let Err(err) = store.prune_older_than(now - chrono::Duration::days(30)) {
        tracing::warn!("pruning unmanaged process binding state failed: {err}");
    }
    let hook_rows = store
        .load_all()
        .map_err(|err| format!("reading unmanaged process binding state failed: {err}"))?;
    let mut out = Vec::new();
    let mut hook_resolved_pids = HashSet::new();

    for row in hook_rows {
        let Some(process) = provider_processes.iter().find(|proc| proc.pid == row.pid) else {
            continue;
        };
        if process.start_time_key != row.process_start_time_key {
            continue;
        }
        if is_provider_process(&process.command) != Some(row.provider.as_str()) {
            continue;
        }

        let (source_inode, source_device, source_offset, source_mtime) = row
            .source_path
            .as_ref()
            .and_then(|path| std::fs::metadata(path).ok())
            .map(|meta| {
                (
                    inode_of(&meta),
                    device_of(&meta),
                    Some(meta.len()),
                    meta.modified().ok().map(DateTime::<Utc>::from),
                )
            })
            .unwrap_or((None, None, None, None));

        let binding = UnmanagedSessionBinding {
            machine_id: machine_id.to_string(),
            provider: row.provider,
            provider_session_id: row.provider_session_id,
            source_path: row
                .source_path
                .as_ref()
                .and_then(|path| path.to_str().map(str::to_string)),
            source_inode,
            source_device,
            pid: Some(process.pid),
            process_start_time: Some(process.start_time.to_rfc3339()),
            cwd: row.cwd,
            source_offset,
            source_mtime: source_mtime.map(|mtime| mtime.to_rfc3339()),
            observed_at: now.to_rfc3339(),
        };

        upsert_newer_binding(&mut out, binding);
        hook_resolved_pids.insert(process.pid);
    }

    let unresolved_processes = provider_processes
        .into_iter()
        .filter(|process| !hook_resolved_pids.contains(&process.pid))
        .collect::<Vec<_>>();
    if unresolved_processes.is_empty() {
        return Ok(out);
    }

    let transcripts = discover_recent_transcripts(now);
    let fd_bindings = collect_from_transcripts_with_processes(
        machine_id,
        &transcripts,
        &scanner,
        now,
        &unresolved_processes,
    )?;
    for binding in fd_bindings {
        upsert_newer_binding(&mut out, binding);
    }
    Ok(out)
}

fn unresolved_unmanaged_processes(
    processes: Vec<ProcessInfo>,
    excluded_managed_pids: &HashSet<u32>,
) -> Vec<ProcessInfo> {
    processes
        .into_iter()
        .filter(|process| {
            is_provider_process(&process.command).is_some()
                && !excluded_managed_pids.contains(&process.pid)
        })
        .collect()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessInfo {
    pub pid: u32,
    pub start_time: DateTime<Utc>,
    pub start_time_key: String,
    pub command: String,
}

/// Injectable source of process + fd truth. Implemented by
/// [`SystemScanner`] in production; tests substitute fixtures.
pub trait ProcessScanner {
    fn list_processes(&self) -> Result<Vec<ProcessInfo>, String>;
    fn list_open_files(&self, pid: u32) -> Result<Vec<PathBuf>, String>;
}

struct SystemScanner;

impl ProcessScanner for SystemScanner {
    fn list_processes(&self) -> Result<Vec<ProcessInfo>, String> {
        run_ps()
    }

    fn list_open_files(&self, pid: u32) -> Result<Vec<PathBuf>, String> {
        run_lsof(pid)
    }
}

fn run_ps() -> Result<Vec<ProcessInfo>, String> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,tty=,stat=,lstart=,command="])
        .output()
        .map_err(|err| format!("running ps for unmanaged inventory: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "ps for unmanaged inventory exited with {}",
            output.status
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    Ok(parse_ps(&text))
}

fn run_lsof(pid: u32) -> Result<Vec<PathBuf>, String> {
    let output = Command::new("lsof")
        .args(["-F", "n", "-p", &pid.to_string()])
        .output()
        .map_err(|err| format!("running lsof for unmanaged pid {pid}: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "lsof for unmanaged pid {pid} exited with {}",
            output.status
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    Ok(parse_lsof(&text))
}

/// Parse `ps -axo pid=,tty=,stat=,lstart=,command=` output.
///
/// `lstart` is a fixed-width 24-char field like `Sun Apr 27 10:15:23 2026`.
fn parse_ps(text: &str) -> Vec<ProcessInfo> {
    let mut out = Vec::new();
    for line in text.lines() {
        let Some((_pid, fact)) = parse_process_fact(line) else {
            continue;
        };
        let Some(start_time) = fact.start_time else {
            continue;
        };
        out.push(ProcessInfo {
            pid: fact.pid,
            start_time,
            start_time_key: fact.lstart,
            command: fact.command,
        });
    }
    out
}

/// Parse `lsof -F n -p <pid>` output. `-F n` only prints `n<path>` records
/// (and process `p<pid>` headers). We ignore headers and keep paths.
fn parse_lsof(text: &str) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    for line in text.lines() {
        let Some(stripped) = line.strip_prefix('n') else {
            continue;
        };
        // Skip socket / pipe / device entries — they don't start with '/'.
        if !stripped.starts_with('/') {
            continue;
        }
        paths.push(PathBuf::from(stripped));
    }
    paths
}

fn is_provider_process(command: &str) -> Option<&'static str> {
    // Grab argv[0] — the first whitespace-separated token.
    let mut argv = command.split_whitespace();
    let argv0 = argv.next().unwrap_or("");
    let basename = Path::new(argv0)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if let Some(provider) = provider_from_argv0_basename(basename) {
        return Some(provider);
    }

    // Homebrew/npm CLIs can appear in `ps` as `node /opt/homebrew/bin/<cli> ...`;
    // the provider executable is still the user's stock launcher, not a
    // Longhouse-owned runtime.
    if !matches!(basename, "node" | "nodejs" | "bun") {
        return None;
    }
    let script = argv.next().unwrap_or("");
    let script_basename = Path::new(script)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if script_basename.starts_with("longhouse-") {
        return None;
    }
    // Claude and legacy Gemini are not Node-launched on supported installs today.
    match script_basename {
        "opencode" | "opencode.js" => Some("opencode"),
        "codex" | "codex.js" if matches!(basename, "node" | "nodejs") => Some("codex"),
        "agy" | "agy.js" | "antigravity" | "antigravity.js" => Some("antigravity"),
        _ => None,
    }
}

pub fn process_info_for_pid(pid: u32, provider: &str) -> Option<ProcessInfo> {
    run_ps()
        .ok()?
        .into_iter()
        .find(|proc| proc.pid == pid && is_provider_process(&proc.command) == Some(provider))
}

fn upsert_newer_binding(
    bindings: &mut Vec<UnmanagedSessionBinding>,
    next: UnmanagedSessionBinding,
) {
    let Some(existing) = bindings.iter_mut().find(|binding| {
        binding.provider == next.provider && binding.provider_session_id == next.provider_session_id
    }) else {
        bindings.push(next);
        return;
    };

    let existing_observed = DateTime::parse_from_rfc3339(&existing.observed_at)
        .ok()
        .map(|value| value.with_timezone(&Utc));
    let next_observed = DateTime::parse_from_rfc3339(&next.observed_at)
        .ok()
        .map(|value| value.with_timezone(&Utc));
    if next_observed >= existing_observed {
        *existing = next;
    }
}

fn provider_from_argv0_basename(basename: &str) -> Option<&'static str> {
    // Reject Longhouse-managed wrappers. Those sessions show up on the
    // managed-lease surface; we don't want to double-count.
    if basename.starts_with("longhouse-") {
        return None;
    }
    match basename {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "agy" | "antigravity" => Some("antigravity"),
        "gemini" => Some("antigravity"),
        "opencode" => Some("opencode"),
        _ => None,
    }
}

fn provider_session_id_from_path(path: &Path, provider: &str) -> Option<String> {
    let stem = path.file_stem()?.to_str()?.to_string();
    if provider == "antigravity" && stem == "transcript" {
        if let Some(id) = antigravity_conversation_id_from_path(path) {
            return Some(id);
        }
    }
    // Claude/Gemini name transcripts after the session UUID. Codex
    // rollout files are named `rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl`;
    // the runtime session stores only `<uuid>` as provider_session_id.
    if stem.is_empty() {
        return None;
    }
    Some(normalize_provider_session_id(provider, &stem))
}

fn antigravity_conversation_id_from_path(path: &Path) -> Option<String> {
    let components: Vec<&str> = path
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .collect();
    for window in components.windows(2) {
        if window[0] == "brain" && is_uuidish(window[1]) {
            return Some(window[1].to_string());
        }
    }
    None
}

fn claude_task_session_id_from_path(path: &Path) -> Option<String> {
    let mut previous: Option<&str> = None;
    for component in path
        .components()
        .filter_map(|component| component.as_os_str().to_str())
    {
        if previous == Some("tasks") && is_uuidish(component) {
            return Some(component.to_string());
        }
        previous = Some(component);
    }
    None
}

fn is_uuidish(value: &str) -> bool {
    value.len() == 36 && value.chars().all(|ch| ch.is_ascii_hexdigit() || ch == '-')
}

fn normalize_provider_session_id(provider: &str, value: &str) -> String {
    let value = value.trim();
    if provider == "codex" && is_codex_rollout_stem(value) {
        return value[CODEX_ROLLOUT_PREFIX_LEN..].to_string();
    }
    value.to_string()
}

const CODEX_ROLLOUT_PREFIX_LEN: usize = "rollout-YYYY-MM-DDTHH-MM-SS-".len();

fn is_codex_rollout_stem(value: &str) -> bool {
    let bytes = value.as_bytes();
    value.len() > CODEX_ROLLOUT_PREFIX_LEN
        && value.starts_with("rollout-")
        && bytes.get(12) == Some(&b'-')
        && bytes.get(15) == Some(&b'-')
        && bytes.get(18) == Some(&b'T')
        && bytes.get(21) == Some(&b'-')
        && bytes.get(24) == Some(&b'-')
        && bytes.get(27) == Some(&b'-')
        && bytes[8..12].iter().all(u8::is_ascii_digit)
        && bytes[13..15].iter().all(u8::is_ascii_digit)
        && bytes[16..18].iter().all(u8::is_ascii_digit)
        && bytes[19..21].iter().all(u8::is_ascii_digit)
        && bytes[22..24].iter().all(u8::is_ascii_digit)
        && bytes[25..27].iter().all(u8::is_ascii_digit)
}

fn canonicalize(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

fn discover_recent_transcripts(now: DateTime<Utc>) -> Vec<(PathBuf, &'static str)> {
    let providers = discovery::get_providers();
    let mut transcripts: Vec<(PathBuf, &'static str)> = Vec::new();
    for (path, provider_name) in discovery::discover_all_files(&providers) {
        if let Ok(meta) = path.metadata() {
            if let Ok(mtime) = meta.modified() {
                let mtime_utc = DateTime::<Utc>::from(mtime);
                if now.signed_duration_since(mtime_utc) <= TRANSCRIPT_MTIME_WINDOW {
                    transcripts.push((path, provider_name));
                }
            }
        }
        if transcripts.len() >= MAX_BINDINGS * 4 {
            break;
        }
    }

    transcripts
}

/// Test seam over already-discovered transcripts and injected process/fd truth.
#[cfg(test)]
pub fn collect_from_transcripts(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    let processes = scanner
        .list_processes()?
        .into_iter()
        .filter(|process| is_provider_process(&process.command).is_some())
        .collect::<Vec<_>>();
    collect_from_transcripts_with_processes(machine_id, transcripts, scanner, now, &processes)
}

fn collect_from_transcripts_with_processes(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
    processes: &[ProcessInfo],
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    if transcripts.is_empty() {
        return Ok(Vec::new());
    }

    // Pre-index transcripts by canonicalized path for fast fd lookup.
    let mut transcript_index: HashMap<PathBuf, (PathBuf, &'static str)> = HashMap::new();
    let mut transcript_by_session: HashMap<(String, String), PathBuf> = HashMap::new();
    for (path, provider) in transcripts {
        transcript_index.insert(canonicalize(path), (path.clone(), *provider));
        if let Some(session_id) = provider_session_id_from_path(path, provider) {
            transcript_by_session.insert((provider.to_string(), session_id), path.clone());
        }
    }

    if processes.is_empty() {
        return Ok(Vec::new());
    }

    // If two processes claim the same transcript, prefer the newer one.
    let mut best_by_transcript: HashMap<PathBuf, (ProcessInfo, &'static str)> = HashMap::new();

    for proc in processes {
        let Some(provider) = is_provider_process(&proc.command) else {
            continue;
        };
        for open_path in scanner.list_open_files(proc.pid)? {
            let canon = canonicalize(&open_path);
            let matched_transcript = transcript_index
                .get(&canon)
                .filter(|(_orig, file_provider)| *file_provider == provider)
                .map(|(orig, _file_provider)| orig.clone())
                .or_else(|| {
                    if provider != "claude" {
                        return None;
                    }
                    let session_id = claude_task_session_id_from_path(&open_path)?;
                    transcript_by_session
                        .get(&(provider.to_string(), session_id))
                        .cloned()
                });
            let Some(display_path) = matched_transcript else {
                continue;
            };
            let display_canon = canonicalize(&display_path);
            match best_by_transcript.get(&display_canon) {
                Some((existing, _)) if existing.start_time >= proc.start_time => continue,
                _ => {
                    best_by_transcript.insert(display_canon, (proc.clone(), provider));
                }
            }
        }
    }

    let mut bindings: Vec<UnmanagedSessionBinding> = Vec::new();
    for (canon_path, (proc, provider)) in best_by_transcript {
        let Some((display_path, _)) = transcript_index.get(&canon_path) else {
            continue;
        };
        let Some(session_id) = provider_session_id_from_path(display_path, provider) else {
            continue;
        };

        let (inode, dev, size, mtime) = match std::fs::metadata(display_path) {
            Ok(meta) => {
                let mtime = meta.modified().ok().map(DateTime::<Utc>::from);
                (inode_of(&meta), device_of(&meta), Some(meta.len()), mtime)
            }
            Err(_) => (None, None, None, None),
        };

        bindings.push(UnmanagedSessionBinding {
            machine_id: machine_id.to_string(),
            provider: provider.to_string(),
            provider_session_id: session_id,
            source_path: display_path.to_str().map(str::to_string),
            source_inode: inode,
            source_device: dev,
            pid: Some(proc.pid),
            process_start_time: Some(proc.start_time.to_rfc3339()),
            cwd: None, // populated in a follow-up; costs another per-pid call.
            source_offset: size,
            source_mtime: mtime.map(|m| m.to_rfc3339()),
            observed_at: now.to_rfc3339(),
        });

        if bindings.len() >= MAX_BINDINGS {
            break;
        }
    }

    Ok(bindings)
}

#[cfg(unix)]
fn inode_of(meta: &std::fs::Metadata) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    Some(meta.ino())
}

#[cfg(not(unix))]
fn inode_of(_: &std::fs::Metadata) -> Option<u64> {
    None
}

#[cfg(unix)]
fn device_of(meta: &std::fs::Metadata) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    Some(meta.dev())
}

#[cfg(not(unix))]
fn device_of(_: &std::fs::Metadata) -> Option<u64> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct FakeScanner {
        processes: Vec<ProcessInfo>,
        open_files: RefCell<HashMap<u32, Vec<PathBuf>>>,
    }

    impl ProcessScanner for FakeScanner {
        fn list_processes(&self) -> Result<Vec<ProcessInfo>, String> {
            Ok(self.processes.clone())
        }

        fn list_open_files(&self, pid: u32) -> Result<Vec<PathBuf>, String> {
            Ok(self
                .open_files
                .borrow()
                .get(&pid)
                .cloned()
                .unwrap_or_default())
        }
    }

    struct FailingLsofScanner {
        process: ProcessInfo,
    }

    impl ProcessScanner for FailingLsofScanner {
        fn list_processes(&self) -> Result<Vec<ProcessInfo>, String> {
            Ok(vec![self.process.clone()])
        }

        fn list_open_files(&self, _pid: u32) -> Result<Vec<PathBuf>, String> {
            Err("fixture lsof failure".to_string())
        }
    }

    fn t(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn proc_info(pid: u32, start: &str, command: &str) -> ProcessInfo {
        ProcessInfo {
            pid,
            start_time: t(start),
            start_time_key: start.to_string(),
            command: command.to_string(),
        }
    }

    #[test]
    fn parses_ps_output() {
        // Apr 27 2026 is a Monday. ps emits local weekday abbreviation; we
        // validate the format strictly via `%a`.
        let input = concat!(
            " 1234 ttys001 S+ Mon Apr 27 10:15:23 2026 /usr/local/bin/codex --config /foo\n",
            "   99 ??      Ss Mon Apr 27 10:00:00 2026 /bin/zsh -l\n",
        );
        let parsed = parse_ps(input);
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].pid, 1234);
        assert!(parsed[0].command.starts_with("/usr/local/bin/codex"));
    }

    #[test]
    fn parses_lsof_output() {
        let input =
            "p1234\nn/Users/x/.codex/sessions/abc.jsonl\nnpipe:[something]\nn/Users/x/.zshrc\n";
        let paths = parse_lsof(input);
        assert_eq!(paths.len(), 2);
        assert!(paths[0].ends_with("abc.jsonl"));
        assert!(paths[1].ends_with(".zshrc"));
    }

    #[test]
    fn scanner_failure_is_non_authoritative_instead_of_empty_truth() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();
        let scanner = FailingLsofScanner {
            process: proc_info(1234, "2026-04-27T10:00:00Z", "/usr/local/bin/codex"),
        };

        let result = collect_from_transcripts(
            "mac",
            &[(transcript, "codex")],
            &scanner,
            now,
        );

        assert_eq!(result.unwrap_err(), "fixture lsof failure");
    }

    #[test]
    fn provider_filter_accepts_bare_clis_and_rejects_wrappers() {
        assert_eq!(
            is_provider_process("/usr/local/bin/codex --tui"),
            Some("codex")
        );
        assert_eq!(
            is_provider_process("node /opt/homebrew/bin/codex --tui"),
            Some("codex")
        );
        assert_eq!(
            is_provider_process(
                "/opt/homebrew/opt/node/bin/node /opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js --tui"
            ),
            Some("codex")
        );
        assert_eq!(
            is_provider_process("/opt/homebrew/bin/opencode serve --port 41967"),
            Some("opencode")
        );
        assert_eq!(
            is_provider_process("node /opt/homebrew/bin/opencode serve"),
            Some("opencode")
        );
        assert_eq!(
            is_provider_process("bun /opt/homebrew/bin/opencode serve"),
            Some("opencode")
        );
        assert_eq!(
            is_provider_process("/Users/x/.local/bin/agy"),
            Some("antigravity")
        );
        assert_eq!(
            is_provider_process("node /opt/homebrew/bin/agy"),
            Some("antigravity")
        );
        assert_eq!(
            is_provider_process("bun /opt/homebrew/bin/codex --tui"),
            None
        );
        assert_eq!(is_provider_process("claude"), Some("claude"));
        assert_eq!(is_provider_process("gemini chat"), Some("antigravity"));
        assert_eq!(is_provider_process("longhouse-codex --attach"), None);
        assert_eq!(is_provider_process("longhouse-opencode serve"), None);
        assert_eq!(is_provider_process("longhouse-antigravity"), None);
        assert_eq!(is_provider_process("/usr/local/bin/longhouse-claude"), None);
        assert_eq!(
            is_provider_process("node /usr/local/bin/longhouse-codex --attach"),
            None
        );
        assert_eq!(
            is_provider_process("node /usr/local/bin/longhouse-opencode serve"),
            None
        );
        assert_eq!(
            is_provider_process("node /usr/local/bin/longhouse-antigravity"),
            None
        );
        assert_eq!(is_provider_process("node server.js"), None);
    }

    #[test]
    fn managed_provider_pids_are_excluded_before_lsof_candidates() {
        let processes = vec![
            proc_info(41, "2026-04-27T10:00:00Z", "/usr/local/bin/codex"),
            proc_info(42, "2026-04-27T10:00:00Z", "/usr/local/bin/codex"),
        ];

        let unresolved = unresolved_unmanaged_processes(processes, &HashSet::from([41]));

        assert_eq!(unresolved.len(), 1);
        assert_eq!(unresolved[0].pid, 42);
    }

    #[test]
    fn codex_rollout_paths_emit_provider_session_uuid() {
        let path = Path::new(
            "/Users/x/.codex/sessions/2026/04/24/rollout-2026-04-24T16-25-08-019dc0f3-fb30-71e3-b0fd-2085e7d045a8.jsonl",
        );

        assert_eq!(
            provider_session_id_from_path(path, "codex").as_deref(),
            Some("019dc0f3-fb30-71e3-b0fd-2085e7d045a8"),
        );
    }

    #[test]
    fn codex_non_rollout_paths_keep_stem() {
        let path = Path::new("/Users/x/.codex/sessions/manual-session.jsonl");

        assert_eq!(
            provider_session_id_from_path(path, "codex").as_deref(),
            Some("manual-session"),
        );
    }

    #[test]
    fn antigravity_transcript_path_uses_brain_conversation_id() {
        let path = Path::new(
            "/Users/x/.gemini/antigravity/brain/53116f30-f150-458c-b36e-2e30f576dc74/.system_generated/logs/transcript.jsonl",
        );

        assert_eq!(
            provider_session_id_from_path(path, "antigravity").as_deref(),
            Some("53116f30-f150-458c-b36e-2e30f576dc74"),
        );
    }

    #[test]
    fn scanner_matches_process_to_transcript() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "/usr/local/bin/codex --tui",
            )],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![transcript.clone()]);
                m
            }),
        };

        let transcripts = vec![(transcript.clone(), "codex")];
        let bindings = collect_from_transcripts("mac", &transcripts, &scanner, now).unwrap();

        assert_eq!(bindings.len(), 1);
        let b = &bindings[0];
        assert_eq!(b.machine_id, "mac");
        assert_eq!(b.provider, "codex");
        assert_eq!(b.provider_session_id, "abc");
        assert_eq!(b.pid, Some(1234));
        assert_eq!(
            b.process_start_time.as_deref(),
            Some("2026-04-27T10:00:00+00:00")
        );
        assert!(b.source_inode.is_some());
        assert_eq!(b.source_offset, Some(3));
    }

    #[test]
    fn scanner_matches_node_wrapped_homebrew_codex_to_transcript() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "node /opt/homebrew/bin/codex --tui",
            )],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![transcript.clone()]);
                m
            }),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript.clone(), "codex")], &scanner, now)
                .unwrap();

        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].provider, "codex");
        assert_eq!(bindings[0].provider_session_id, "abc");
        assert_eq!(bindings[0].pid, Some(1234));
    }

    #[test]
    fn scanner_matches_node_wrapped_npm_codex_js_to_transcript() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "node /opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js --tui",
            )],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![transcript.clone()]);
                m
            }),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript.clone(), "codex")], &scanner, now)
                .unwrap();

        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].provider, "codex");
        assert_eq!(bindings[0].provider_session_id, "abc");
        assert_eq!(bindings[0].pid, Some(1234));
    }

    #[test]
    fn scanner_normalizes_codex_rollout_transcript_ids() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp
            .path()
            .join("rollout-2026-04-24T16-25-08-019dc0f3-fb30-71e3-b0fd-2085e7d045a8.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "/usr/local/bin/codex --tui",
            )],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![transcript.clone()]);
                m
            }),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript.clone(), "codex")], &scanner, now)
                .unwrap();

        assert_eq!(bindings.len(), 1);
        assert_eq!(
            bindings[0].provider_session_id,
            "019dc0f3-fb30-71e3-b0fd-2085e7d045a8",
        );
    }

    #[test]
    fn scanner_matches_claude_task_directory_to_transcript() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let session_id = "718372e2-248c-48a8-b0e9-0f70cbdce6eb";
        let transcript = tmp.path().join(format!("{session_id}.jsonl"));
        let task_dir = tmp.path().join(".claude").join("tasks").join(session_id);
        std::fs::write(&transcript, "{}\n").unwrap();
        std::fs::create_dir_all(&task_dir).unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(1234, "2026-04-27T10:00:00Z", "claude")],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![task_dir]);
                m
            }),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript.clone(), "claude")], &scanner, now)
                .unwrap();

        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].provider, "claude");
        assert_eq!(bindings[0].provider_session_id, session_id);
        assert_eq!(
            bindings[0].source_path,
            transcript.to_str().map(str::to_string)
        );
    }

    #[test]
    fn scanner_prefers_newer_process_on_collision() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let older = proc_info(1000, "2026-04-27T09:00:00Z", "/usr/local/bin/codex");
        let newer = proc_info(2000, "2026-04-27T11:00:00Z", "/usr/local/bin/codex");

        let mut open_files = HashMap::new();
        open_files.insert(1000u32, vec![transcript.clone()]);
        open_files.insert(2000u32, vec![transcript.clone()]);

        let scanner = FakeScanner {
            processes: vec![older.clone(), newer.clone()],
            open_files: RefCell::new(open_files),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript.clone(), "codex")], &scanner, now)
                .unwrap();
        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].pid, Some(newer.pid));
    }

    #[test]
    fn scanner_skips_unheld_transcripts() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "").unwrap();

        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "/usr/local/bin/codex",
            )],
            open_files: RefCell::new(HashMap::new()),
        };

        let bindings =
            collect_from_transcripts("mac", &[(transcript, "codex")], &scanner, now).unwrap();
        assert!(bindings.is_empty());
    }

    #[test]
    fn lstart_parses_current_year() {
        let parsed = parse_lstart("Mon Apr 27 10:15:23 2026").unwrap();
        assert_eq!(parsed.date_naive().to_string(), "2026-04-27");
    }
}
