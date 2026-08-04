//! Shared process identity helpers for PID-reuse-safe liveness checks.

use std::collections::HashMap;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use chrono::DateTime;
use chrono::Utc;

pub const PID_REUSE_TOLERANCE_SECS: i64 = 120;
const PROCESS_INVENTORY_TIMEOUT: Duration = Duration::from_millis(750);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessFact {
    pub pid: u32,
    /// Controlling terminal, e.g. `ttys000`. `??` when the process has none.
    pub tty: String,
    /// `ps` process state field, e.g. `S+`, `Ss`, `R`. A `+` suffix means the
    /// process is in the foreground process group of its controlling terminal.
    pub stat: String,
    /// Raw fixed-width `lstart` field, e.g. `Sun Apr 27 10:15:23 2026`.
    pub lstart: String,
    pub command: String,
    pub start_time: Option<DateTime<Utc>>,
}

impl ProcessFact {
    /// True iff the process has a real controlling terminal and is in that
    /// terminal's foreground process group — i.e. an interactive, attached TUI
    /// rather than a backgrounded or detached process.
    pub fn is_foreground_tty(&self) -> bool {
        let tty = self.tty.trim();
        !tty.is_empty() && tty != "??" && !self.is_stopped_or_zombie() && self.stat.contains('+')
    }

    /// A stopped provider is still present in `ps`, but it cannot make
    /// progress or respond to terminal input. Treat it as non-live evidence
    /// for UI attachment decisions instead of calling it a foreground TUI.
    pub fn is_stopped_or_zombie(&self) -> bool {
        self.stat.starts_with('T') || self.stat.starts_with('Z')
    }
}

pub fn collect_process_facts_by_pid() -> HashMap<u32, ProcessFact> {
    try_collect_process_facts_by_pid().unwrap_or_default()
}

/// Read one process identity without depending on a successful whole-system
/// inventory. This is used when persisting or validating an owned child PID.
pub fn try_collect_process_fact(pid: u32) -> Option<ProcessFact> {
    let output = Command::new("ps")
        .args([
            "-p",
            &pid.to_string(),
            "-o",
            "pid=,tty=,stat=,lstart=,command=",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut lines = text.lines().filter(|line| !line.trim().is_empty());
    let (parsed_pid, fact) = parse_process_fact(lines.next()?)?;
    (lines.next().is_none() && parsed_pid == pid).then_some(fact)
}

/// Collect one coherent process inventory, distinguishing a valid empty scan
/// from a failed `ps` invocation. Callers reconciling durable state must retain
/// their last observation when this returns `None`.
pub fn try_collect_process_facts_by_pid() -> Option<HashMap<u32, ProcessFact>> {
    let mut command = Command::new("ps");
    command.args(["-axo", "pid=,tty=,stat=,lstart=,command="]);
    let output = output_with_timeout(command, PROCESS_INVENTORY_TIMEOUT)?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line_count = text.lines().filter(|line| !line.trim().is_empty()).count();
    let facts = text
        .lines()
        .filter_map(parse_process_fact_for_inventory)
        .collect::<HashMap<_, _>>();
    if facts.len() != line_count || !facts.contains_key(&std::process::id()) {
        return None;
    }
    Some(facts)
}

fn output_with_timeout(mut command: Command, timeout: Duration) -> Option<Output> {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes).ok()
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait().ok()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = reader.join();
            return None;
        }
        thread::sleep(Duration::from_millis(5));
    };
    let stdout = reader.join().ok()??;
    Some(Output {
        status,
        stdout,
        stderr: Vec::new(),
    })
}

/// Parse one `ps -axo pid=,tty=,stat=,lstart=,command=` line.
///
/// `pid`, `tty`, and `stat` are whitespace-delimited single tokens; `lstart` is
/// a fixed-width 24-char field like `Sun Apr 27 10:15:23 2026`; `command` is the
/// remainder (and may contain spaces), so it stays last.
pub fn parse_process_fact(line: &str) -> Option<(u32, ProcessFact)> {
    parse_process_fact_impl(line, true)
}

fn parse_process_fact_for_inventory(line: &str) -> Option<(u32, ProcessFact)> {
    parse_process_fact_impl(line, false)
}

fn parse_process_fact_impl(line: &str, parse_all_start_times: bool) -> Option<(u32, ProcessFact)> {
    let trimmed = line.trim_start();
    let (pid_text, rest) = trimmed.split_once(char::is_whitespace)?;
    let pid = pid_text.parse::<u32>().ok()?;
    let (tty, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (stat, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let rest = rest.trim_start();
    if rest.len() <= 24 {
        return None;
    }
    let (lstart_raw, command) = rest.split_at(24);
    let lstart = lstart_raw.trim().to_string();
    let command = command.trim().to_string();
    if command.is_empty() {
        return None;
    }
    let start_time = (parse_all_start_times || command_needs_start_time(&command))
        .then(|| parse_lstart(&lstart))
        .flatten();
    Some((
        pid,
        ProcessFact {
            pid,
            tty: tty.to_string(),
            stat: stat.to_string(),
            lstart: lstart.clone(),
            command,
            start_time,
        },
    ))
}

fn command_needs_start_time(command: &str) -> bool {
    command.split_whitespace().any(|part| {
        Path::new(part)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| {
                matches!(
                    name,
                    "claude"
                        | "codex"
                        | "opencode"
                        | "cursor-agent"
                        | "longhouse"
                        | "longhouse-engine"
                        | "agy"
                        | "antigravity"
                        | "gemini"
                        | "node"
                        | "nodejs"
                        | "bun"
                )
            })
    })
}

pub fn parse_lstart(value: &str) -> Option<DateTime<Utc>> {
    // ps -o lstart= emits local time ("Mon Apr 27 10:15:23 2026"). Parse as
    // naive and anchor to the system's local tz.
    use chrono::Local;
    use chrono::NaiveDateTime;
    use chrono::TimeZone;
    let naive = NaiveDateTime::parse_from_str(value, "%a %b %e %H:%M:%S %Y").ok()?;
    Local
        .from_local_datetime(&naive)
        .single()
        .map(|dt| dt.with_timezone(&Utc))
}

pub fn parse_rfc3339(value: &str) -> Option<DateTime<Utc>> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    DateTime::parse_from_rfc3339(trimmed)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
}

pub fn command_contains_basename(command: &str, expected: &str) -> bool {
    command.split_whitespace().any(|part| {
        Path::new(part)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name == expected)
    })
}

/// Reject a PID whose process started meaningfully after the recorded start.
///
/// If either timestamp is unknown, callers cannot prove PID reuse and should
/// fall back to their command-shape check, preserving previous behavior.
pub fn started_before_or_near_recorded(
    fact: &ProcessFact,
    recorded_start: Option<DateTime<Utc>>,
) -> bool {
    match (fact.start_time, recorded_start) {
        (Some(proc_start), Some(recorded)) => {
            (proc_start - recorded).num_seconds() <= PID_REUSE_TOLERANCE_SECS
        }
        _ => true,
    }
}

pub fn lstart_matches_recorded(fact: &ProcessFact, recorded_lstart: &str) -> bool {
    let recorded = recorded_lstart.trim();
    recorded.is_empty() || fact.lstart.trim() == recorded
}

/// Parent and process-group lineage for one process.
///
/// Kept separate from [`ProcessFact`] because ownership questions ("is this
/// subprocess ours?") need `ppid`/`pgid`, while liveness questions need `tty` and
/// `lstart`. Collecting both in one struct would widen every existing caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessLineage {
    pub pid: u32,
    pub ppid: u32,
    pub pgid: i32,
    pub stat: String,
    pub command: String,
}

/// One coherent process observation carrying both identity and ownership
/// fields. Stall corroboration uses this instead of combining a targeted
/// identity read with a later lineage read, which could straddle a process
/// exit or PID/PGID reuse.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessSnapshot {
    pub fact: ProcessFact,
    pub ppid: u32,
    pub pgid: i32,
}

impl ProcessLineage {
    /// True for a process stopped by a signal (`T`) or reaped-but-unwaited (`Z`).
    /// Either state under a live parent means work that will never finish on its
    /// own.
    pub fn is_stopped_or_zombie(&self) -> bool {
        self.stat
            .chars()
            .next()
            .is_some_and(|state| state == 'T' || state == 'Z')
    }
}

/// Collect one timeout-bounded process snapshot containing identity and
/// parent/process-group lineage in the same `ps` result.
pub fn try_collect_process_snapshots() -> Option<HashMap<u32, ProcessSnapshot>> {
    let mut command = Command::new("ps");
    command.args(["-axo", "pid=,ppid=,pgid=,tty=,stat=,lstart=,command="]);
    let output = output_with_timeout(command, PROCESS_INVENTORY_TIMEOUT)?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line_count = text.lines().filter(|line| !line.trim().is_empty()).count();
    let snapshots = text
        .lines()
        .filter_map(parse_process_snapshot)
        .map(|snapshot| (snapshot.fact.pid, snapshot))
        .collect::<HashMap<_, _>>();
    if snapshots.len() != line_count || !snapshots.contains_key(&std::process::id()) {
        return None;
    }
    Some(snapshots)
}

/// Parse one `ps -axo pid=,ppid=,pgid=,tty=,stat=,lstart=,command=` line.
pub fn parse_process_snapshot(line: &str) -> Option<ProcessSnapshot> {
    let trimmed = line.trim_start();
    let (pid_text, rest) = trimmed.split_once(char::is_whitespace)?;
    let (ppid_text, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (pgid_text, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (tty, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (stat, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let rest = rest.trim_start();
    if rest.len() <= 24 {
        return None;
    }
    let (lstart_raw, command) = rest.split_at(24);
    let lstart = lstart_raw.trim().to_string();
    let command = command.trim().to_string();
    if command.is_empty() {
        return None;
    }
    let pid = pid_text.parse::<u32>().ok()?;
    Some(ProcessSnapshot {
        fact: ProcessFact {
            pid,
            tty: tty.to_string(),
            stat: stat.to_string(),
            lstart: lstart.clone(),
            command,
            start_time: parse_lstart(&lstart),
        },
        ppid: ppid_text.parse().ok()?,
        pgid: pgid_text.parse().ok()?,
    })
}

/// Parse one `ps -axo pid=,ppid=,pgid=,stat=,command=` line. `command` stays last
/// because it may contain spaces.
pub fn parse_process_lineage(line: &str) -> Option<ProcessLineage> {
    let trimmed = line.trim_start();
    let (pid_text, rest) = trimmed.split_once(char::is_whitespace)?;
    let (ppid_text, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (pgid_text, rest) = rest.trim_start().split_once(char::is_whitespace)?;
    let (stat, command) = rest.trim_start().split_once(char::is_whitespace)?;
    let command = command.trim();
    if command.is_empty() {
        return None;
    }
    Some(ProcessLineage {
        pid: pid_text.parse().ok()?,
        ppid: ppid_text.parse().ok()?,
        pgid: pgid_text.parse().ok()?,
        stat: stat.to_string(),
        command: command.to_string(),
    })
}

/// Transitive descendants of `root`, plus any process sharing `pgid` when one is
/// recorded. The process-group arm is load-bearing: a wedged command can be
/// reparented to PID 1 and drop out of the descendant tree entirely.
pub fn owned_processes<'a>(
    entries: &'a [ProcessLineage],
    root: u32,
    pgid: Option<i32>,
) -> Vec<(&'a ProcessLineage, &'static str)> {
    let mut descendants: std::collections::HashSet<u32> = std::collections::HashSet::new();
    descendants.insert(root);
    // Bounded relaxation: process trees here are shallow, and each pass can only
    // add processes, so this converges well before the entry count.
    for _ in 0..entries.len().min(64) {
        let mut grew = false;
        for entry in entries {
            if !descendants.contains(&entry.pid) && descendants.contains(&entry.ppid) {
                descendants.insert(entry.pid);
                grew = true;
            }
        }
        if !grew {
            break;
        }
    }

    entries
        .iter()
        .filter(|entry| entry.pid != root)
        .filter_map(|entry| {
            if descendants.contains(&entry.pid) {
                Some((entry, "descendant"))
            } else if pgid.is_some_and(|group| group == entry.pgid) {
                Some((entry, "process_group"))
            } else {
                None
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_command_output_returns_successful_output() {
        let mut command = Command::new("sh");
        command.args(["-c", "printf ready"]);
        let output = output_with_timeout(command, Duration::from_secs(1)).unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, b"ready");
    }

    #[test]
    fn bounded_command_output_terminates_a_stalled_process() {
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 1"]);
        let started = Instant::now();
        assert!(output_with_timeout(command, Duration::from_millis(10)).is_none());
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[test]
    fn parse_process_fact_extracts_pid_tty_stat_lstart_command() {
        let line =
            "  4242 ttys000  S+   Mon May  5 11:58:00 2026 opencode serve --hostname 127.0.0.1";
        let (pid, fact) = parse_process_fact(line).unwrap();
        assert_eq!(pid, 4242);
        assert_eq!(fact.pid, 4242);
        assert_eq!(fact.tty, "ttys000");
        assert_eq!(fact.stat, "S+");
        assert_eq!(fact.lstart, "Mon May  5 11:58:00 2026");
        assert!(fact.command.starts_with("opencode serve"));
    }

    #[test]
    fn foreground_tty_requires_real_terminal_and_plus_state() {
        let foreground = parse_process_fact(
            "  101 ttys003  S+   Mon May  5 11:58:00 2026 claude --session-id abc",
        )
        .unwrap()
        .1;
        assert!(foreground.is_foreground_tty());

        // No controlling terminal (daemon / detached).
        let no_tty =
            parse_process_fact("  102 ??       Ss   Mon May  5 11:58:00 2026 claude --resume")
                .unwrap()
                .1;
        assert!(!no_tty.is_foreground_tty());

        // Has a terminal but is backgrounded (no `+`).
        let background =
            parse_process_fact("  103 ttys003  S    Mon May  5 11:58:00 2026 claude --resume")
                .unwrap()
                .1;
        assert!(!background.is_foreground_tty());

        let stopped =
            parse_process_fact("  104 ttys003  T+   Mon May  5 11:58:00 2026 claude --resume")
                .unwrap()
                .1;
        assert!(stopped.is_stopped_or_zombie());
        assert!(!stopped.is_foreground_tty());
    }

    #[test]
    fn lstart_parses_current_year() {
        let parsed = parse_lstart("Mon Apr 27 10:15:23 2026").unwrap();
        assert_eq!(parsed.date_naive().to_string(), "2026-04-27");
    }

    #[test]
    fn targeted_process_identity_matches_full_inventory() {
        let pid = std::process::id();
        let targeted = try_collect_process_fact(pid).expect("targeted process identity");
        let inventory = try_collect_process_facts_by_pid().expect("full process inventory");
        let full = inventory.get(&pid).expect("current process in inventory");

        assert_eq!(targeted.pid, full.pid);
        assert_eq!(targeted.tty, full.tty);
        assert_eq!(targeted.stat, full.stat);
        assert_eq!(targeted.lstart, full.lstart);
        assert_eq!(targeted.command, full.command);
        assert!(targeted.start_time.is_some());
    }

    fn lineage(pid: u32, ppid: u32, pgid: i32, stat: &str) -> ProcessLineage {
        ProcessLineage {
            pid,
            ppid,
            pgid,
            stat: stat.to_string(),
            command: format!("proc-{pid}"),
        }
    }

    #[test]
    fn owned_processes_finds_reparented_group_member() {
        // The motivating incident: the wedged shell was orphaned to PID 1, so it
        // was no longer a descendant of the app-server. Process-group membership
        // is the only thing that still identifies it as ours.
        let entries = vec![
            lineage(16956, 900, 16956, "Ss"),
            lineage(17210, 1, 16956, "Ts"),
            lineage(4242, 1, 4242, "Ts"),
        ];

        let owned = owned_processes(&entries, 16956, Some(16956));
        let pids: Vec<(u32, &str)> = owned
            .iter()
            .map(|(entry, matched)| (entry.pid, *matched))
            .collect();

        assert_eq!(pids, vec![(17210, "process_group")]);
        assert!(
            !pids.iter().any(|(pid, _)| *pid == 4242),
            "an unrelated stopped process must not be claimed"
        );
    }

    #[test]
    fn owned_processes_walks_transitive_descendants() {
        let entries = vec![
            lineage(100, 1, 100, "Ss"),
            lineage(101, 100, 100, "S"),
            lineage(102, 101, 100, "T"),
        ];

        let owned = owned_processes(&entries, 100, None);
        let mut pids: Vec<u32> = owned.iter().map(|(entry, _)| entry.pid).collect();
        pids.sort_unstable();
        assert_eq!(pids, vec![101, 102]);
        assert!(owned.iter().all(|(_, matched)| *matched == "descendant"));
    }

    #[test]
    fn stopped_and_zombie_states_are_recognized() {
        assert!(lineage(1, 1, 1, "Ts").is_stopped_or_zombie());
        assert!(lineage(1, 1, 1, "T").is_stopped_or_zombie());
        assert!(lineage(1, 1, 1, "Z").is_stopped_or_zombie());
        assert!(!lineage(1, 1, 1, "S+").is_stopped_or_zombie());
        assert!(!lineage(1, 1, 1, "R").is_stopped_or_zombie());
    }

    #[test]
    fn parse_process_lineage_keeps_command_with_spaces() {
        let entry = parse_process_lineage(
            "17210     1 16956 Ts   /bin/zsh -lc for n in {1..10}; do x; done",
        )
        .unwrap();
        assert_eq!(entry.pid, 17210);
        assert_eq!(entry.ppid, 1);
        assert_eq!(entry.pgid, 16956);
        assert_eq!(entry.stat, "Ts");
        assert_eq!(entry.command, "/bin/zsh -lc for n in {1..10}; do x; done");
    }

    #[test]
    fn parse_process_snapshot_keeps_identity_and_lineage_together() {
        let snapshot = parse_process_snapshot(
            "17210     1 16956 ?? Ts   Tue Aug  4 20:00:00 2026 /bin/zsh -lc sleep 300",
        )
        .unwrap();
        assert_eq!(snapshot.fact.pid, 17210);
        assert_eq!(snapshot.ppid, 1);
        assert_eq!(snapshot.pgid, 16956);
        assert_eq!(snapshot.fact.stat, "Ts");
        assert_eq!(snapshot.fact.lstart, "Tue Aug  4 20:00:00 2026");
        assert_eq!(snapshot.fact.command, "/bin/zsh -lc sleep 300");
    }

    #[test]
    fn full_inventory_parses_start_time_only_for_relevant_processes() {
        let shell = parse_process_fact_for_inventory(
            "  101 ttys003  S+   Mon May  5 11:58:00 2026 /bin/zsh -l",
        )
        .unwrap()
        .1;
        let provider = parse_process_fact_for_inventory(
            "  102 ttys003  S+   Tue May  5 11:58:00 2026 /opt/homebrew/bin/codex --remote ws://x",
        )
        .unwrap()
        .1;

        assert!(shell.start_time.is_none());
        assert!(provider.start_time.is_some());
    }
}
