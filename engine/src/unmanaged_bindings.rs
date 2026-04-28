//! Phase 5b of `docs/specs/session-liveness-honesty.md`: machine-observed
//! bindings between unmanaged provider-CLI processes and their JSONL
//! transcripts.
//!
//! The scanner answers one question per unmanaged session the user has open
//! locally: *is a `claude` / `codex` / `gemini` process actually holding
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
//!      (`claude`, `codex`, `gemini`) — never `longhouse-*` wrappers
//!      (those are managed sessions and get their own lease surface).
//!   3. For each candidate pid, ask `lsof -F n -p <pid>` which regular
//!      files it has open, and look for transcript paths.
//!   4. Emit one [`UnmanagedSessionBinding`] per `(provider,
//!      provider_session_id)` with `(pid, process_start_time)` as the
//!      liveness identity.
//!
//! Kept behind a `ProcessScanner` trait so tests can inject fixtures
//! without shelling out.

use std::collections::HashMap;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use chrono::DateTime;
use chrono::Utc;

use crate::discovery;
use crate::heartbeat::UnmanagedSessionBinding;

/// Cap the number of bindings emitted per heartbeat. Provider roots with
/// thousands of stale transcripts shouldn't inflate the payload.
const MAX_BINDINGS: usize = 128;

/// Only consider transcripts modified within this window. A well-kept
/// user's recent-unmanaged-sessions set is small; older files are noise
/// for liveness decisions.
const TRANSCRIPT_MTIME_WINDOW: chrono::Duration = chrono::Duration::hours(24);

/// Extracts one row per alive unmanaged provider CLI process holding a
/// recent transcript file. Callers pass this straight through on the
/// heartbeat.
pub fn collect_unmanaged_session_bindings(machine_id: &str) -> Vec<UnmanagedSessionBinding> {
    collect_with_scanner(machine_id, &SystemScanner, Utc::now())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessInfo {
    pub pid: u32,
    pub start_time: DateTime<Utc>,
    pub command: String,
}

/// Injectable source of process + fd truth. Implemented by
/// [`SystemScanner`] in production; tests substitute fixtures.
pub trait ProcessScanner {
    fn list_processes(&self) -> Vec<ProcessInfo>;
    fn list_open_files(&self, pid: u32) -> Vec<PathBuf>;
}

struct SystemScanner;

impl ProcessScanner for SystemScanner {
    fn list_processes(&self) -> Vec<ProcessInfo> {
        run_ps()
    }

    fn list_open_files(&self, pid: u32) -> Vec<PathBuf> {
        run_lsof(pid)
    }
}

fn run_ps() -> Vec<ProcessInfo> {
    let Ok(output) = Command::new("ps").args(["-axo", "pid=,lstart=,command="]).output() else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&output.stdout);
    parse_ps(&text)
}

fn run_lsof(pid: u32) -> Vec<PathBuf> {
    let Ok(output) = Command::new("lsof")
        .args(["-F", "n", "-p", &pid.to_string()])
        .output()
    else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&output.stdout);
    parse_lsof(&text)
}

/// Parse `ps -axo pid=,lstart=,command=` output.
///
/// `lstart` is a fixed-width 24-char field like `Sun Apr 27 10:15:23 2026`.
fn parse_ps(text: &str) -> Vec<ProcessInfo> {
    let mut out = Vec::new();
    for line in text.lines() {
        let trimmed = line.trim_start();
        let Some((pid_str, rest)) = trimmed.split_once(char::is_whitespace) else {
            continue;
        };
        let Ok(pid) = pid_str.parse::<u32>() else {
            continue;
        };
        let rest = rest.trim_start();
        // lstart is always 24 chars.
        if rest.len() <= 24 {
            continue;
        }
        let (lstart_raw, command) = rest.split_at(24);
        let lstart = lstart_raw.trim();
        let command = command.trim().to_string();
        let Some(start_time) = parse_lstart(lstart) else {
            continue;
        };
        out.push(ProcessInfo {
            pid,
            start_time,
            command,
        });
    }
    out
}

fn parse_lstart(value: &str) -> Option<DateTime<Utc>> {
    // ps -o lstart= always emits local time ("Mon Apr 27 10:15:23 2026").
    // Parse as naive and anchor to the system's local tz.
    use chrono::Local;
    use chrono::NaiveDateTime;
    use chrono::TimeZone;
    let naive = NaiveDateTime::parse_from_str(value, "%a %b %e %H:%M:%S %Y").ok()?;
    Local.from_local_datetime(&naive).single().map(|dt| dt.with_timezone(&Utc))
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
    let argv0 = command.split_whitespace().next().unwrap_or("");
    let basename = Path::new(argv0)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    // Reject Longhouse-managed wrappers. Those sessions show up on the
    // managed-lease surface; we don't want to double-count.
    if basename.starts_with("longhouse-") {
        return None;
    }
    match basename {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "gemini" => Some("gemini"),
        _ => None,
    }
}

fn provider_session_id_from_path(path: &Path, provider: &str) -> Option<String> {
    let stem = path.file_stem()?.to_str()?.to_string();
    // All three providers name transcripts after the session UUID.
    // Gemini uses `<uuid>.json` under `~/.gemini/tmp/<cwd>/`; the stem
    // still works as an identifier.
    if stem.is_empty() {
        return None;
    }
    let _ = provider; // accepted for future provider-specific normalization.
    Some(stem)
}

fn canonicalize(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Core logic, pure over (provider discovery + scanner + clock). Tests
/// inject all three.
pub fn collect_with_scanner(
    machine_id: &str,
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
) -> Vec<UnmanagedSessionBinding> {
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

    collect_from_transcripts(machine_id, &transcripts, scanner, now)
}

/// Same as [`collect_with_scanner`] but takes the already-discovered
/// transcripts. Keeps the integration surface small for tests.
pub fn collect_from_transcripts(
    machine_id: &str,
    transcripts: &[(PathBuf, &'static str)],
    scanner: &dyn ProcessScanner,
    now: DateTime<Utc>,
) -> Vec<UnmanagedSessionBinding> {
    if transcripts.is_empty() {
        return Vec::new();
    }

    // Pre-index transcripts by canonicalized path for fast fd lookup.
    let mut transcript_index: HashMap<PathBuf, (PathBuf, &'static str)> = HashMap::new();
    for (path, provider) in transcripts {
        transcript_index.insert(canonicalize(path), (path.clone(), *provider));
    }

    let mut processes = scanner.list_processes();
    // Filter + tag provider from argv[0].
    processes.retain(|proc| is_provider_process(&proc.command).is_some());
    if processes.is_empty() {
        return Vec::new();
    }

    // If two processes claim the same transcript, prefer the newer one.
    let mut best_by_transcript: HashMap<PathBuf, (ProcessInfo, &'static str)> = HashMap::new();

    for proc in &processes {
        let Some(provider) = is_provider_process(&proc.command) else {
            continue;
        };
        for open_path in scanner.list_open_files(proc.pid) {
            let canon = canonicalize(&open_path);
            let Some((_orig, file_provider)) = transcript_index.get(&canon) else {
                continue;
            };
            if *file_provider != provider {
                continue;
            }
            match best_by_transcript.get(&canon) {
                Some((existing, _)) if existing.start_time >= proc.start_time => continue,
                _ => {
                    best_by_transcript.insert(canon, (proc.clone(), provider));
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

    bindings
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
    use chrono::TimeZone;
    use std::cell::RefCell;

    struct FakeScanner {
        processes: Vec<ProcessInfo>,
        open_files: RefCell<HashMap<u32, Vec<PathBuf>>>,
    }

    impl ProcessScanner for FakeScanner {
        fn list_processes(&self) -> Vec<ProcessInfo> {
            self.processes.clone()
        }

        fn list_open_files(&self, pid: u32) -> Vec<PathBuf> {
            self.open_files
                .borrow()
                .get(&pid)
                .cloned()
                .unwrap_or_default()
        }
    }

    fn t(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn parses_ps_output() {
        // Apr 27 2026 is a Monday. ps emits local weekday abbreviation; we
        // validate the format strictly via `%a`.
        let input = concat!(
            " 1234 Mon Apr 27 10:15:23 2026 /usr/local/bin/codex --config /foo\n",
            "   99 Mon Apr 27 10:00:00 2026 /bin/zsh -l\n",
        );
        let parsed = parse_ps(input);
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].pid, 1234);
        assert!(parsed[0].command.starts_with("/usr/local/bin/codex"));
    }

    #[test]
    fn parses_lsof_output() {
        let input = "p1234\nn/Users/x/.codex/sessions/abc.jsonl\nnpipe:[something]\nn/Users/x/.zshrc\n";
        let paths = parse_lsof(input);
        assert_eq!(paths.len(), 2);
        assert!(paths[0].ends_with("abc.jsonl"));
        assert!(paths[1].ends_with(".zshrc"));
    }

    #[test]
    fn provider_filter_accepts_bare_clis_and_rejects_wrappers() {
        assert_eq!(is_provider_process("/usr/local/bin/codex --tui"), Some("codex"));
        assert_eq!(is_provider_process("claude"), Some("claude"));
        assert_eq!(is_provider_process("gemini chat"), Some("gemini"));
        assert_eq!(is_provider_process("longhouse-codex --attach"), None);
        assert_eq!(is_provider_process("/usr/local/bin/longhouse-claude"), None);
        assert_eq!(is_provider_process("node server.js"), None);
    }

    #[test]
    fn scanner_matches_process_to_transcript() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let scanner = FakeScanner {
            processes: vec![ProcessInfo {
                pid: 1234,
                start_time: t("2026-04-27T10:00:00Z"),
                command: "/usr/local/bin/codex --tui".into(),
            }],
            open_files: RefCell::new({
                let mut m = HashMap::new();
                m.insert(1234u32, vec![transcript.clone()]);
                m
            }),
        };

        let transcripts = vec![(transcript.clone(), "codex")];
        let bindings =
            collect_from_transcripts("mac", &transcripts, &scanner, now);

        assert_eq!(bindings.len(), 1);
        let b = &bindings[0];
        assert_eq!(b.machine_id, "mac");
        assert_eq!(b.provider, "codex");
        assert_eq!(b.provider_session_id, "abc");
        assert_eq!(b.pid, Some(1234));
        assert_eq!(b.process_start_time.as_deref(), Some("2026-04-27T10:00:00+00:00"));
        assert!(b.source_inode.is_some());
        assert_eq!(b.source_offset, Some(3));
    }

    #[test]
    fn scanner_prefers_newer_process_on_collision() {
        let now = t("2026-04-27T12:00:00Z");
        let tmp = tempfile::tempdir().unwrap();
        let transcript = tmp.path().join("abc.jsonl");
        std::fs::write(&transcript, "{}\n").unwrap();

        let older = ProcessInfo {
            pid: 1000,
            start_time: t("2026-04-27T09:00:00Z"),
            command: "/usr/local/bin/codex".into(),
        };
        let newer = ProcessInfo {
            pid: 2000,
            start_time: t("2026-04-27T11:00:00Z"),
            command: "/usr/local/bin/codex".into(),
        };

        let mut open_files = HashMap::new();
        open_files.insert(1000u32, vec![transcript.clone()]);
        open_files.insert(2000u32, vec![transcript.clone()]);

        let scanner = FakeScanner {
            processes: vec![older.clone(), newer.clone()],
            open_files: RefCell::new(open_files),
        };

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript.clone(), "codex")],
            &scanner,
            now,
        );
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
            processes: vec![ProcessInfo {
                pid: 1234,
                start_time: t("2026-04-27T10:00:00Z"),
                command: "/usr/local/bin/codex".into(),
            }],
            open_files: RefCell::new(HashMap::new()),
        };

        let bindings = collect_from_transcripts(
            "mac",
            &[(transcript, "codex")],
            &scanner,
            now,
        );
        assert!(bindings.is_empty());
    }

    #[test]
    fn lstart_parses_current_year() {
        let parsed = parse_lstart("Mon Apr 27 10:15:23 2026").unwrap();
        assert_eq!(parsed.date_naive().to_string(), "2026-04-27");
    }
}
