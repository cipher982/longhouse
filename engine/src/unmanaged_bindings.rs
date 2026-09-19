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
//!   1. Validate hook-resolved bindings first, then inspect open files for
//!      unresolved live provider processes.
//!   2. Discover only provider roots implicated by that open-file evidence
//!      via `discovery::discover_all_files`, filtered by mtime.
//!   3. Enumerate candidate provider-CLI processes with
//!      `ps -axo pid=,lstart=,command=`. Filter by command basename
//!      (`claude`, `codex`, `agy`, `opencode`, `pi`, `omp`) plus the stock
//!      Node-backed launcher shapes (`node .../codex`, `node .../opencode`,
//!      etc.) - never `longhouse-*` wrappers (those are managed sessions and
//!      get their own lease surface).
//!   4. For each candidate pid, ask `lsof -F n -p <pid>` which regular
//!      files it has open, and look for transcript paths.
//!   5. Emit one [`UnmanagedSessionBinding`] per `(provider,
//!      provider_session_id)` with `(pid, process_start_time)` as the
//!      liveness identity.
//!
//! Kept behind a `ProcessScanner` trait so tests can inject fixtures
//! without shelling out.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use chrono::DateTime;
use chrono::Utc;

use crate::discovery;
use crate::heartbeat::UnmanagedSessionBinding;
#[cfg(test)]
use crate::process_identity::parse_lstart;
use crate::process_identity::ProcessFact;
use crate::state::unmanaged_process_binding::UnmanagedProcessBindingStore;

/// Cap the number of bindings emitted per heartbeat. Provider roots with
/// thousands of stale transcripts shouldn't inflate the payload.
const MAX_BINDINGS: usize = 128;

/// Only consider transcripts modified within this window. A well-kept
/// user's recent-unmanaged-sessions set is small; older files are noise
/// for liveness decisions.
const TRANSCRIPT_MTIME_WINDOW: chrono::Duration = chrono::Duration::hours(24);
/// Bound one optional process inspection so one dead provider cannot hold the
/// whole Shadow discovery pass open.
const LSOF_CALL_TIMEOUT: Duration = Duration::from_secs(2);
/// Stop starting more optional work after this cooperative per-pass budget.
/// Expiry is non-authoritative, not an empty or complete process inventory.
const UNMANAGED_REFRESH_BUDGET: Duration = Duration::from_secs(10);

/// Merge fd-scanned bindings with hook-observed unmanaged provider bindings.
///
/// Claude does not reliably keep its JSONL transcript open between writes, so
/// the fd scanner can see the process but miss the session identity. The Claude
/// hook writes the provider pid + session id locally; this function validates
/// that the same pid/start-time is still alive before emitting the normal
/// heartbeat binding shape.
pub(crate) fn collect_unmanaged_session_bindings_with_process_inventory(
    conn: &rusqlite::Connection,
    machine_id: &str,
    now: DateTime<Utc>,
    excluded_managed_pids: &HashSet<u32>,
    processes: Vec<ProcessInfo>,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    collect_unmanaged_session_bindings_from_processes(
        conn,
        machine_id,
        now,
        excluded_managed_pids,
        processes,
        &SystemScanner,
    )
}

fn collect_unmanaged_session_bindings_from_processes(
    conn: &rusqlite::Connection,
    machine_id: &str,
    now: DateTime<Utc>,
    excluded_managed_pids: &HashSet<u32>,
    processes: Vec<ProcessInfo>,
    scanner: &dyn ProcessScanner,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    let deadline = Instant::now() + UNMANAGED_REFRESH_BUDGET;
    let provider_processes = unresolved_unmanaged_processes(processes, excluded_managed_pids);
    let store = UnmanagedProcessBindingStore::new(conn);
    if let Err(err) = store.prune_older_than(now - chrono::Duration::days(30)) {
        tracing::warn!("pruning unmanaged process binding state failed: {err}");
    }
    let hook_rows = store
        .load_all()
        .map_err(|err| format!("reading unmanaged process binding state failed: {err}"))?;
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    let mut out = Vec::new();
    let mut ambiguous_binding_keys = HashSet::new();
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

        // A hook row without a readable, regular source is only an activity
        // observation. It is not exact-source evidence and must not authorize
        // a binding while the archive is unavailable.
        let Some(stored_path) = row.source_path.as_ref() else {
            continue;
        };
        let source_path =
            discovery::canonical_transcript_hint(&row.provider, stored_path).into_owned();
        let Ok(meta) = std::fs::metadata(&source_path) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let (source_inode, source_device, source_offset, source_mtime) = (
            inode_of(&meta),
            device_of(&meta),
            Some(meta.len()),
            meta.modified().ok().map(DateTime::<Utc>::from),
        );

        let binding = UnmanagedSessionBinding {
            machine_id: machine_id.to_string(),
            provider: row.provider,
            provider_session_id: row.provider_session_id,
            source_path: source_path.to_str().map(str::to_string),
            source_inode,
            source_device,
            pid: Some(process.pid),
            process_start_time: Some(process.start_time.to_rfc3339()),
            cwd: row.cwd,
            source_offset,
            source_mtime: source_mtime.map(|mtime| mtime.to_rfc3339()),
            observed_at: now.to_rfc3339(),
        };

        admit_binding(&mut out, &mut ambiguous_binding_keys, binding);
        hook_resolved_pids.insert(process.pid);
    }

    let unresolved_processes = provider_processes
        .into_iter()
        .filter(|process| !hook_resolved_pids.contains(&process.pid))
        .collect::<Vec<_>>();
    if unresolved_processes.is_empty() {
        if Instant::now() >= deadline {
            return Err(unmanaged_refresh_timeout());
        }
        return Ok(out);
    }

    // Inspect live unresolved processes before touching any provider archive.
    let evidence = collect_process_open_file_evidence(unresolved_processes, scanner, deadline)?;
    if evidence.iter().all(|item| item.open_files.is_empty()) {
        return Ok(out);
    }

    let providers = discovery::get_providers();
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    let relevant = relevant_provider_names(&evidence, &providers);
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    if relevant.is_empty() {
        return Ok(out);
    }
    let transcripts = discover_recent_transcripts(now, &providers, &relevant)?;
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    let fd_bindings = collect_from_transcripts_with_open_files(
        machine_id,
        &transcripts,
        now,
        &evidence,
        deadline,
    )?;
    for binding in fd_bindings {
        admit_binding(&mut out, &mut ambiguous_binding_keys, binding);
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
#[derive(Clone, Debug)]
struct ProcessOpenFileEvidence {
    process: ProcessInfo,
    provider: &'static str,
    open_files: Vec<PathBuf>,
}

fn collect_process_open_file_evidence(
    processes: Vec<ProcessInfo>,
    scanner: &dyn ProcessScanner,
    deadline: Instant,
) -> Result<Vec<ProcessOpenFileEvidence>, String> {
    let candidates = processes
        .into_iter()
        .filter_map(|process| {
            is_provider_process(&process.command).map(|provider| (process, provider))
        })
        .collect::<Vec<_>>();
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    let pids = candidates
        .iter()
        .map(|(process, _)| process.pid)
        .collect::<Vec<_>>();
    let open_files = scanner.list_open_files(&pids)?;
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    let mut evidence = Vec::with_capacity(candidates.len());
    for (process, provider) in candidates {
        let open_files = open_files.get(&process.pid).cloned().unwrap_or_default();
        evidence.push(ProcessOpenFileEvidence {
            process,
            provider,
            open_files,
        });
    }
    Ok(evidence)
}

fn relevant_provider_names(
    evidence: &[ProcessOpenFileEvidence],
    providers: &[discovery::ProviderConfig],
) -> HashSet<&'static str> {
    let mut relevant = HashSet::new();
    for item in evidence {
        for path in &item.open_files {
            if item.provider == "claude" && claude_task_session_id_from_path(path).is_some() {
                relevant.insert("claude");
            }

            let matching = discovery::matching_provider_names_for_path(path, providers);
            if !matching.contains(&item.provider) {
                continue;
            }
            relevant.insert(item.provider);

            // A path physically shared by Pi and OMP is intentionally
            // ambiguous. Keep both names selected so discover_all_files can
            // apply its canonical ambiguity guard.
            if matching.contains(&"pi") && matching.contains(&"omp") {
                relevant.insert("pi");
                relevant.insert("omp");
            }
        }
    }
    relevant
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessInfo {
    pub pid: u32,
    pub start_time: DateTime<Utc>,
    pub start_time_key: String,
    pub command: String,
}

/// Injectable source of open-file truth. Process inventory is collected once
/// by the daemon and passed in; tests substitute only the fd lookup.
///
/// The lookup is batched because `lsof` is a subprocess: asking it once per
/// process is the same answer bought N times, and the refresh runs against
/// every unmanaged provider process on the machine.
#[allow(dead_code)]
pub trait ProcessScanner {
    /// Open files per PID. A PID that is absent from the map holds none (it can
    /// exit between the process inventory and this probe); a PID that cannot be
    /// probed at all is an error for the whole refresh.
    fn list_open_files(&self, pids: &[u32]) -> Result<HashMap<u32, Vec<PathBuf>>, String>;
}

struct SystemScanner;

impl ProcessScanner for SystemScanner {
    fn list_open_files(&self, pids: &[u32]) -> Result<HashMap<u32, Vec<PathBuf>>, String> {
        run_lsof(pids)
    }
}

fn run_lsof(pids: &[u32]) -> Result<HashMap<u32, Vec<PathBuf>>, String> {
    let list = pids
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let mut command = Command::new("lsof");
    command.args(["-F", "pn", "-p", &list]);
    let output = crate::process_identity::output_with_timeout(command, LSOF_CALL_TIMEOUT)
        .ok_or_else(|| format!("lsof output unavailable or timed out for unmanaged pids {list}"))?;
    if !output.status.success() {
        if lsof_reports_no_match(&output.status, &output.stderr) {
            // The process inventory is a point-in-time observation. A provider
            // can exit between that snapshot and this lookup; that is no
            // open-file match, not evidence that lsof itself is unreadable.
            return Ok(HashMap::new());
        }
        let detail = String::from_utf8_lossy(&output.stderr);
        let detail = detail.trim();
        return Err(format!(
            "lsof for unmanaged pids {list} exited with {}{}",
            output.status,
            if detail.is_empty() {
                String::new()
            } else {
                format!(": {detail}")
            }
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    Ok(parse_lsof(&text))
}

fn unmanaged_refresh_timeout() -> String {
    format!(
        "unmanaged binding refresh exceeded {}ms",
        UNMANAGED_REFRESH_BUDGET.as_millis()
    )
}

fn lsof_reports_no_match(status: &std::process::ExitStatus, stderr: &[u8]) -> bool {
    if status.code() != Some(1) {
        return false;
    }
    let detail = String::from_utf8_lossy(stderr);
    let detail = detail.trim();
    detail.is_empty()
        || detail
            .lines()
            .all(|line| line.trim_end().ends_with("No such process"))
}

/// Parse `lsof -F n -p <pid>` output. `-F n` only prints `n<path>` records
/// (and process `p<pid>` headers). We ignore headers and keep paths.
/// Parse `lsof -F pn` records into open files per PID. A `p<pid>` line opens a
/// process record; each `n<path>` line after it belongs to that process.
fn parse_lsof(text: &str) -> HashMap<u32, Vec<PathBuf>> {
    let mut out: HashMap<u32, Vec<PathBuf>> = HashMap::new();
    let mut current: Option<u32> = None;
    for line in text.lines() {
        if let Some(pid) = line.strip_prefix('p') {
            current = pid.trim().parse().ok();
            if let Some(pid) = current {
                out.entry(pid).or_default();
            }
            continue;
        }
        let Some(stripped) = line.strip_prefix('n') else {
            continue;
        };
        // Skip socket / pipe / device entries — they don't start with '/'.
        if !stripped.starts_with('/') {
            continue;
        }
        let Some(pid) = current else {
            continue;
        };
        out.entry(pid).or_default().push(PathBuf::from(stripped));
    }
    out
}

pub(crate) fn is_provider_process(command: &str) -> Option<&'static str> {
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
    match script_basename {
        "opencode" | "opencode.js" => Some("opencode"),
        "codex" | "codex.js" if matches!(basename, "node" | "nodejs") => Some("codex"),
        "agy" | "agy.js" | "antigravity" | "antigravity.js" => Some("antigravity"),
        "pi" | "pi.js" => Some("pi"),
        "omp" | "omp.js" | "oh-my-pi" | "oh-my-pi.js" => Some("omp"),
        _ => None,
    }
}

/// The same lookup against an inventory the caller already collected. The
/// per-pid probe it replaces spawned `ps` once per outbox payload, on a tick
/// that runs every 100 ms.
pub fn process_info_from_facts(
    facts: &HashMap<u32, ProcessFact>,
    pid: u32,
    provider: &str,
) -> Option<ProcessInfo> {
    process_info_from_fact(facts.get(&pid)?.clone(), provider)
}

fn process_info_from_fact(fact: ProcessFact, provider: &str) -> Option<ProcessInfo> {
    let start_time = fact.start_time?;
    (is_provider_process(&fact.command) == Some(provider)).then_some(ProcessInfo {
        pid: fact.pid,
        start_time,
        start_time_key: fact.lstart,
        command: fact.command,
    })
}

fn admit_binding(
    bindings: &mut Vec<UnmanagedSessionBinding>,
    ambiguous_keys: &mut HashSet<(String, String)>,
    next: UnmanagedSessionBinding,
) {
    let key = (next.provider.clone(), next.provider_session_id.clone());
    if ambiguous_keys.contains(&key) {
        return;
    }
    let Some(existing_index) = bindings.iter().position(|binding| {
        binding.provider == next.provider && binding.provider_session_id == next.provider_session_id
    }) else {
        bindings.push(next);
        return;
    };
    let existing = &bindings[existing_index];
    if same_binding_identity(existing, &next) {
        if next.observed_at >= existing.observed_at {
            bindings[existing_index] = next;
        }
    } else {
        // Two distinct process/source identities for one native session are
        // ambiguous. Retaining the newest one would make the join arbitrary.
        bindings.remove(existing_index);
        ambiguous_keys.insert(key);
    }
}

fn same_binding_identity(
    existing: &UnmanagedSessionBinding,
    next: &UnmanagedSessionBinding,
) -> bool {
    existing.pid == next.pid
        && existing.process_start_time == next.process_start_time
        && existing
            .source_path
            .as_deref()
            .zip(next.source_path.as_deref())
            .is_some_and(|(left, right)| {
                canonicalize(Path::new(left)) == canonicalize(Path::new(right))
            })
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
        "pi" => Some("pi"),
        "omp" => Some("omp"),
        _ => None,
    }
}

fn provider_session_id_from_path(path: &Path, provider: &str) -> Option<String> {
    let canonical_path = discovery::canonical_transcript_hint(provider, path);
    let path = canonical_path.as_ref();
    if provider == "pi" {
        return crate::pi_session::read_session_header_id(path).ok();
    }
    if provider == "omp" {
        return crate::omp_session::read_session_header(path)
            .ok()
            .map(|header| header.native_id);
    }
    if provider == "antigravity" {
        return path
            .file_name()
            .and_then(|name| name.to_str())
            .filter(|name| *name == "transcript_full.jsonl")
            .and_then(|_| antigravity_conversation_id_from_path(path));
    }
    let stem = path.file_stem()?.to_str()?.to_string();
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

fn discover_recent_transcripts(
    now: DateTime<Utc>,
    providers: &[discovery::ProviderConfig],
    relevant: &HashSet<&'static str>,
) -> Result<Vec<(PathBuf, &'static str)>, String> {
    let selected = providers
        .iter()
        .filter(|provider| relevant.contains(provider.name))
        .cloned()
        .collect::<Vec<_>>();
    if selected.is_empty() {
        return Ok(Vec::new());
    }

    let discovery_scan = discovery::discover_all_files_with_inventory(&selected);
    if discovery_scan.inventory.scan_error_count > 0 {
        return Err(format!(
            "unmanaged transcript discovery incomplete: {} source walk errors",
            discovery_scan.inventory.scan_error_count
        ));
    }

    let mut transcripts: Vec<(PathBuf, &'static str)> = Vec::new();
    for (path, provider_name) in discovery_scan.files {
        if let Ok(meta) = path.metadata() {
            if let Ok(mtime) = meta.modified() {
                let mtime_utc = DateTime::<Utc>::from(mtime);
                if now.signed_duration_since(mtime_utc) <= TRANSCRIPT_MTIME_WINDOW {
                    transcripts.push((path, provider_name));
                }
            }
        }
    }
    Ok(transcripts)
}

/// Test seam over already-discovered transcripts and injected process/fd truth.
#[cfg(test)]
pub fn collect_from_transcripts(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    processes: &[ProcessInfo],
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    let processes = processes
        .iter()
        .cloned()
        .filter(|process| is_provider_process(&process.command).is_some())
        .collect::<Vec<_>>();
    collect_from_transcripts_with_processes(
        machine_id,
        transcripts,
        scanner,
        now,
        &processes,
        Instant::now() + UNMANAGED_REFRESH_BUDGET,
    )
}

fn collect_from_transcripts_with_processes(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
    processes: &[ProcessInfo],
    deadline: Instant,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    if transcripts.is_empty() {
        return Ok(Vec::new());
    }

    let evidence = collect_process_open_file_evidence(processes.to_vec(), scanner, deadline)?;
    collect_from_transcripts_with_open_files(machine_id, transcripts, now, &evidence, deadline)
}

fn collect_from_transcripts_with_open_files(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    now: DateTime<Utc>,
    evidence: &[ProcessOpenFileEvidence],
    deadline: Instant,
) -> Result<Vec<UnmanagedSessionBinding>, String> {
    if Instant::now() >= deadline {
        return Err(unmanaged_refresh_timeout());
    }
    if transcripts.is_empty() || evidence.is_empty() {
        return Ok(Vec::new());
    }

    // Pre-index transcripts by canonicalized path for fast fd lookup. Keep
    // every candidate: a duplicate is ambiguity, not a reason to pick one.
    let mut transcript_index: HashMap<PathBuf, Vec<(PathBuf, &'static str)>> = HashMap::new();
    let mut transcript_by_session: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();
    for (path, provider) in transcripts {
        let source_path = discovery::canonical_transcript_hint(provider, path).into_owned();
        transcript_index
            .entry(canonicalize(&source_path))
            .or_default()
            .push((source_path.clone(), *provider));
        if let Some(session_id) = provider_session_id_from_path(&source_path, provider) {
            transcript_by_session
                .entry((provider.to_string(), session_id))
                .or_default()
                .push(source_path);
        }
    }
    let ambiguous_source_paths = transcript_index
        .iter()
        .filter(|(_, candidates)| candidates.len() != 1)
        .map(|(path, _)| path.clone())
        .collect::<HashSet<_>>();
    let ambiguous_sessions = transcript_by_session
        .iter()
        .filter(|(_, candidates)| candidates.len() != 1)
        .map(|(key, _)| key.clone())
        .collect::<HashSet<_>>();

    // If two processes claim the same transcript, the process/source join is
    // unknown. Never select the newer process as a proxy for identity.
    let mut processes_by_transcript: HashMap<PathBuf, Vec<(ProcessInfo, &'static str)>> =
        HashMap::new();

    for item in evidence {
        if Instant::now() >= deadline {
            return Err(unmanaged_refresh_timeout());
        }
        for open_path in &item.open_files {
            let canon = canonicalize(open_path);
            let matched_transcript = match transcript_index.get(&canon) {
                Some(candidates)
                    if !ambiguous_source_paths.contains(&canon)
                        && candidates.len() == 1
                        && candidates[0].1 == item.provider =>
                {
                    Some(candidates[0].0.clone())
                }
                _ if item.provider == "claude" => (|| {
                    let session_id = claude_task_session_id_from_path(open_path)?;
                    let key = (item.provider.to_string(), session_id);
                    let candidates = transcript_by_session.get(&key)?;
                    if candidates.len() != 1 {
                        return None;
                    }
                    let path = candidates[0].clone();
                    (!ambiguous_source_paths.contains(&canonicalize(&path))).then_some(path)
                })(),
                _ => None,
            };
            let Some(display_path) = matched_transcript else {
                continue;
            };
            let display_canon = canonicalize(&display_path);
            let candidates = processes_by_transcript.entry(display_canon).or_default();
            if !candidates.iter().any(|(existing, provider)| {
                *provider == item.provider
                    && existing.pid == item.process.pid
                    && existing.start_time == item.process.start_time
            }) {
                candidates.push((item.process.clone(), item.provider));
            }
        }
    }

    let mut bindings: Vec<UnmanagedSessionBinding> = Vec::new();
    for (canon_path, candidates) in processes_by_transcript {
        if Instant::now() >= deadline {
            return Err(unmanaged_refresh_timeout());
        }
        if candidates.len() != 1 {
            continue;
        }
        let Some((display_path, provider)) = transcript_index
            .get(&canon_path)
            .and_then(|candidates| candidates.first())
        else {
            continue;
        };
        let Some(session_id) = provider_session_id_from_path(display_path, provider) else {
            continue;
        };
        if ambiguous_sessions.contains(&(provider.to_string(), session_id.clone())) {
            continue;
        }
        let Ok(meta) = std::fs::metadata(display_path) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let (proc, _) = &candidates[0];
        let size = Some(meta.len());
        let mtime = meta.modified().ok().map(DateTime::<Utc>::from);

        bindings.push(UnmanagedSessionBinding {
            machine_id: machine_id.to_string(),
            provider: provider.to_string(),
            provider_session_id: session_id,
            source_path: display_path.to_str().map(str::to_string),
            source_inode: inode_of(&meta),
            source_device: device_of(&meta),
            pid: Some(proc.pid),
            process_start_time: Some(proc.start_time.to_rfc3339()),
            cwd: None, // populated in a follow-up; costs another per-pid call.
            source_offset: size,
            source_mtime: mtime.map(|m| m.to_rfc3339()),
            observed_at: now.to_rfc3339(),
        });
    }
    if bindings.len() > MAX_BINDINGS {
        return Err("unmanaged binding discovery incomplete: cap reached".to_string());
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
        fn list_open_files(&self, pids: &[u32]) -> Result<HashMap<u32, Vec<PathBuf>>, String> {
            let open_files = self.open_files.borrow();
            Ok(pids
                .iter()
                .filter_map(|pid| open_files.get(pid).cloned().map(|paths| (*pid, paths)))
                .collect())
        }
    }

    struct FailingLsofScanner {
        process: ProcessInfo,
    }

    impl ProcessScanner for FailingLsofScanner {
        fn list_open_files(&self, _pids: &[u32]) -> Result<HashMap<u32, Vec<PathBuf>>, String> {
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
    fn parses_lsof_output() {
        let input =
            "p1234\nn/Users/x/.codex/sessions/abc.jsonl\nnpipe:[something]\nn/Users/x/.zshrc\n";
        let files = parse_lsof(input);
        let paths = &files[&1234];
        assert_eq!(paths.len(), 2);
        assert!(paths[0].ends_with("abc.jsonl"));
        assert!(paths[1].ends_with(".zshrc"));
    }

    #[test]
    fn parses_one_lsof_record_per_process() {
        // One `lsof` answers for the whole pid list, so a file must stay with
        // the process that holds it.
        let input = "p1234\nn/Users/x/a.jsonl\np5678\nn/Users/x/b.jsonl\nn/Users/x/c.jsonl\n";
        let files = parse_lsof(input);
        assert_eq!(files[&1234].len(), 1);
        assert!(files[&1234][0].ends_with("a.jsonl"));
        assert_eq!(files[&5678].len(), 2);
        assert!(files[&5678][1].ends_with("c.jsonl"));
    }

    #[cfg(unix)]
    #[test]
    fn lsof_process_race_is_no_match_but_unreadable_scan_is_not() {
        use std::os::unix::process::ExitStatusExt;

        let exited_without_match = std::process::ExitStatus::from_raw(1 << 8);
        assert!(lsof_reports_no_match(&exited_without_match, b""));
        assert!(lsof_reports_no_match(
            &exited_without_match,
            b"lsof: status error on 1234: No such process"
        ));
        assert!(!lsof_reports_no_match(
            &exited_without_match,
            b"lsof: permission denied"
        ));
        assert!(!lsof_reports_no_match(
            &exited_without_match,
            b"lsof: status error on 1234: No such process\nlsof: permission denied"
        ));
        assert!(!lsof_reports_no_match(
            &std::process::ExitStatus::from_raw(2 << 8),
            b""
        ));
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
            std::slice::from_ref(&scanner.process),
            &scanner,
            now,
        );

        assert!(result.is_err());
    }

    #[test]
    fn expired_binding_pass_is_non_authoritative_instead_of_partial_truth() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();
        let scanner = FakeScanner {
            processes: vec![proc_info(
                1234,
                "2026-04-27T10:00:00Z",
                "/usr/local/bin/codex",
            )],
            open_files: RefCell::new(HashMap::new()),
        };

        let result = collect_from_transcripts_with_processes(
            "mac",
            &[(transcript, "codex")],
            &scanner,
            now,
            &scanner.processes,
            Instant::now(),
        );

        assert!(result.is_err());
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
        assert_eq!(is_provider_process("omp --print"), Some("omp"));
        assert_eq!(
            is_provider_process("node /opt/homebrew/bin/omp --print"),
            Some("omp")
        );
        assert_eq!(
            is_provider_process("node /opt/homebrew/lib/node_modules/oh-my-pi/bin/omp.js"),
            Some("omp")
        );
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
            "/Users/x/.gemini/antigravity/brain/53116f30-f150-458c-b36e-2e30f576dc74/.system_generated/logs/transcript_full.jsonl",
        );

        assert_eq!(
            provider_session_id_from_path(path, "antigravity").as_deref(),
            Some("53116f30-f150-458c-b36e-2e30f576dc74"),
        );
    }

    #[test]
    fn pi_native_identity_comes_from_session_header() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("arbitrary-name.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"session\",\"id\":\"53116f30-f150-458c-b36e-2e30f576dc74\"}\n",
        )
        .unwrap();

        assert_eq!(
            provider_session_id_from_path(&path, "pi").as_deref(),
            Some("53116f30-f150-458c-b36e-2e30f576dc74"),
        );
    }

    #[test]
    fn omp_native_identity_comes_from_session_header() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("2026-04-27T12-00-00-opaque.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"session\",\"id\":\"opaque-native-id\",\"cwd\":\"/tmp\",\"provider\":\"omp\"}\n",
        )
        .unwrap();

        assert_eq!(
            provider_session_id_from_path(&path, "omp").as_deref(),
            Some("opaque-native-id"),
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
        let bindings =
            collect_from_transcripts("mac", &transcripts, &scanner.processes, &scanner, now)
                .unwrap();

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

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
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

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
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

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
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

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "claude")],
            &scanner.processes,
            &scanner,
            now,
        )
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
    fn scanner_rejects_process_collision_instead_of_picking_newest() {
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
            processes: vec![older, newer],
            open_files: RefCell::new(open_files),
        };

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
        .unwrap();
        assert!(
            bindings.is_empty(),
            "ambiguous process/source joins are non-authoritative"
        );
    }

    #[test]
    fn scanner_rejects_unavailable_source_metadata() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("missing.jsonl");
        let process = proc_info(1234, "2026-04-27T10:00:00Z", "/usr/local/bin/codex");
        let scanner = FakeScanner {
            processes: vec![process.clone()],
            open_files: RefCell::new(HashMap::from([(process.pid, vec![transcript.clone()])])),
        };

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript, "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
        .unwrap();
        assert!(bindings.is_empty());
    }

    #[test]
    fn scanner_rejects_multiple_sources_for_one_native_session() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let first = tmp.path().join("one").join("same.jsonl");
        let second = tmp.path().join("two").join("same.jsonl");
        std::fs::create_dir_all(first.parent().unwrap()).unwrap();
        std::fs::create_dir_all(second.parent().unwrap()).unwrap();
        std::fs::write(&first, "{}\n").unwrap();
        std::fs::write(&second, "{}\n").unwrap();
        let process = proc_info(1234, "2026-04-27T10:00:00Z", "/usr/local/bin/codex");
        let scanner = FakeScanner {
            processes: vec![process.clone()],
            open_files: RefCell::new(HashMap::from([(
                process.pid,
                vec![first.clone(), second.clone()],
            )])),
        };

        let bindings = collect_from_transcripts(
            "mac",
            &[(first, "codex"), (second, "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
        .unwrap();
        assert!(bindings.is_empty());
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

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript, "codex")],
            &scanner.processes,
            &scanner,
            now,
        )
        .unwrap();
        assert!(bindings.is_empty());
    }

    #[test]
    fn optional_discovery_only_walks_provider_with_open_source_evidence() {
        let now = Utc::now();
        let tmp = tempfile::tempdir().unwrap();
        let codex_root = tmp.path().join("codex");
        let claude_root = tmp.path().join("claude");
        std::fs::create_dir_all(&codex_root).unwrap();
        std::fs::create_dir_all(&claude_root).unwrap();
        let codex_transcript = codex_root.join("codex-session.jsonl");
        let claude_transcript = claude_root.join("claude-session.jsonl");
        std::fs::write(&codex_transcript, "{}\n").unwrap();
        std::fs::write(&claude_transcript, "{}\n").unwrap();

        let providers = vec![
            discovery::ProviderConfig {
                name: "claude",
                root: claude_root,
                extension: "jsonl",
            },
            discovery::ProviderConfig {
                name: "codex",
                root: codex_root,
                extension: "jsonl",
            },
        ];
        let evidence = vec![ProcessOpenFileEvidence {
            process: proc_info(1234, "2026-04-27T10:00:00Z", "/usr/local/bin/codex"),
            provider: "codex",
            open_files: vec![codex_transcript.clone()],
        }];

        let relevant = relevant_provider_names(&evidence, &providers);
        assert_eq!(relevant, HashSet::from(["codex"]));
        let discovered = discover_recent_transcripts(now, &providers, &relevant).unwrap();
        assert_eq!(discovered, vec![(codex_transcript, "codex")]);

        // No relevant evidence must short-circuit before any provider walk.
        let none = discover_recent_transcripts(now, &providers, &HashSet::new()).unwrap();
        assert!(none.is_empty());
    }

    #[test]
    fn lstart_parses_current_year() {
        let parsed = parse_lstart("Mon Apr 27 10:15:23 2026").unwrap();
        assert_eq!(parsed.date_naive().to_string(), "2026-04-27");
    }
}
