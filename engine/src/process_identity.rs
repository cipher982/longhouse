//! Shared process identity helpers for PID-reuse-safe liveness checks.

use std::collections::HashMap;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Output, Stdio};
#[cfg(not(unix))]
use std::thread;
use std::time::{Duration, Instant};

use chrono::DateTime;
use chrono::Utc;
#[cfg(target_os = "macos")]
#[path = "process_identity_macos.rs"]
mod macos;

pub const PID_REUSE_TOLERANCE_SECS: i64 = 120;
// A full process inventory can take several seconds on hosts servicing
// hundreds of provider and helper processes.
#[cfg(not(target_os = "macos"))]
const PROCESS_INVENTORY_TIMEOUT: Duration = Duration::from_secs(10);
#[cfg(not(target_os = "macos"))]
const TARGETED_PROCESS_INVENTORY_TIMEOUT: Duration = Duration::from_secs(1);
pub(crate) const TARGETED_PROCESS_FACT_BATCH_MAX: usize = 256;

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcessFactLookup {
    Present(ProcessFact),
    Absent,
    Unavailable,
}

pub fn collect_process_facts_by_pid() -> HashMap<u32, ProcessFact> {
    try_collect_process_facts_by_pid().unwrap_or_default()
}

/// Read one process identity without depending on a successful whole-system
/// inventory. This is used when persisting or validating an owned child PID.
pub fn try_collect_process_fact(pid: u32) -> Option<ProcessFact> {
    match inspect_process_fact(pid) {
        ProcessFactLookup::Present(fact) => Some(fact),
        ProcessFactLookup::Absent | ProcessFactLookup::Unavailable => None,
    }
}

/// Inspect one PID while preserving the difference between a missing process
/// and an unavailable process-identity probe.
#[cfg(target_os = "macos")]
pub fn inspect_process_fact(pid: u32) -> ProcessFactLookup {
    macos::inspect_process_fact(pid)
}

#[cfg(not(target_os = "macos"))]
pub fn inspect_process_fact(pid: u32) -> ProcessFactLookup {
    let output = Command::new("ps")
        .args([
            "-p",
            &pid.to_string(),
            "-o",
            "pid=,tty=,stat=,lstart=,command=",
        ])
        .output();
    let Ok(output) = output else {
        return ProcessFactLookup::Unavailable;
    };
    if !output.status.success() {
        return if output.status.code() == Some(1) && output.stderr.is_empty() {
            ProcessFactLookup::Absent
        } else {
            ProcessFactLookup::Unavailable
        };
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut lines = text.lines().filter(|line| !line.trim().is_empty());
    let Some(line) = lines.next() else {
        return ProcessFactLookup::Absent;
    };
    let Some((parsed_pid, fact)) = parse_process_fact(line) else {
        return ProcessFactLookup::Unavailable;
    };
    if lines.next().is_some() || parsed_pid != pid {
        return ProcessFactLookup::Unavailable;
    }
    ProcessFactLookup::Present(fact)
}

/// Collect one coherent process inventory, distinguishing a valid empty scan
/// from an unavailable or partial observation. Callers reconciling durable
/// state must retain their last observation when this returns `None`.
#[cfg(target_os = "macos")]
pub fn try_collect_process_facts_by_pid() -> Option<HashMap<u32, ProcessFact>> {
    macos::try_collect_process_facts_by_pid()
}

#[cfg(not(target_os = "macos"))]
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
/// Whether process group `pgid` has a member that is not a zombie. `None` when
/// the native process scan or its non-macOS `ps` fallback is incomplete.
#[cfg(target_os = "macos")]
pub fn try_group_has_non_zombie(pgid: i32) -> Option<bool> {
    macos::try_group_has_non_zombie(pgid)
}

#[cfg(not(target_os = "macos"))]
pub fn try_group_has_non_zombie(pgid: i32) -> Option<bool> {
    let mut command = Command::new("ps");
    command.args(["-axo", "pgid=,stat="]);
    let output = output_with_timeout(command, PROCESS_INVENTORY_TIMEOUT)?;
    if !output.status.success() {
        return None;
    }
    group_has_non_zombie_in(&String::from_utf8_lossy(&output.stdout), pgid)
}

/// The parse behind [`try_group_has_non_zombie`]: `pgid stat` lines. Any line
/// that does not parse makes the whole pass unusable.
fn group_has_non_zombie_in(text: &str, pgid: i32) -> Option<bool> {
    let mut running = false;
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let mut fields = line.split_whitespace();
        let group = fields.next()?.parse::<i32>().ok()?;
        let stat = fields.next()?;
        // `T` (stopped) is still a member that holds resources; only `Z` has exited.
        if group == pgid && !stat.starts_with('Z') {
            running = true;
        }
    }
    Some(running)
}

/// Read one bounded batch of exact process identities without enumerating the
/// rest of the machine. A successful result is a complete observation for the
/// requested PIDs: omitted PIDs were absent, while a failed/partial probe
/// remains unavailable.
#[cfg(target_os = "macos")]
pub fn try_collect_process_facts_for_pids(pids: &[u32]) -> Option<HashMap<u32, ProcessFact>> {
    macos::try_collect_process_facts_for_pids(pids)
}

#[cfg(not(target_os = "macos"))]
pub fn try_collect_process_facts_for_pids(pids: &[u32]) -> Option<HashMap<u32, ProcessFact>> {
    if pids.len() > TARGETED_PROCESS_FACT_BATCH_MAX || pids.iter().any(|pid| *pid <= 1) {
        return None;
    }
    let mut requested = pids.to_vec();
    requested.sort_unstable();
    requested.dedup();
    if requested.is_empty() {
        return Some(HashMap::new());
    }

    let pid_list = requested
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let mut command = Command::new("ps");
    command.args(["-p", &pid_list, "-o", "pid=,tty=,stat=,lstart=,command="]);
    let output = output_with_timeout(command, TARGETED_PROCESS_INVENTORY_TIMEOUT)?;
    if !output.status.success() {
        if output.status.code() == Some(1) && output.stderr.is_empty() {
            return Some(HashMap::new());
        }
        return None;
    }

    let text = String::from_utf8_lossy(&output.stdout);
    let line_count = text.lines().filter(|line| !line.trim().is_empty()).count();
    let mut facts = HashMap::with_capacity(requested.len().min(line_count));
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let (pid, fact) = parse_process_fact_for_inventory(line)?;
        if requested.binary_search(&pid).is_err() || facts.insert(pid, fact).is_some() {
            return None;
        }
    }
    (facts.len() == line_count).then_some(facts)
}

/// Resolve one process's executable name and parent without launching `ps`.
/// The name matches `ps -o comm=`; callers use it for executable identity,
/// not the potentially user-controlled argument vector.
pub(crate) fn process_comm_and_parent(pid: u32) -> Option<(String, u32)> {
    #[cfg(target_os = "macos")]
    {
        macos::process_comm_and_parent(pid)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let output = Command::new("ps")
            .args(["-o", "comm=,ppid=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let mut fields = text.split_whitespace();
        Some((fields.next()?.to_string(), fields.next()?.parse().ok()?))
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn try_collect_process_cwds(pids: &[u32]) -> Option<HashMap<u32, String>> {
    macos::try_collect_process_cwds(pids)
}

#[cfg(target_os = "macos")]
pub(crate) fn try_collect_process_open_files(
    pids: &[u32],
) -> Option<HashMap<u32, Vec<std::path::PathBuf>>> {
    macos::try_collect_process_open_files(pids)
}

#[cfg(unix)]
pub(crate) fn output_with_timeout(mut command: Command, timeout: Duration) -> Option<Output> {
    use std::io::ErrorKind;
    use std::os::fd::{AsRawFd, OwnedFd};
    use std::os::unix::net::UnixStream;
    use std::os::unix::process::CommandExt;

    let (stdout, stdout_writer) = UnixStream::pair().ok()?;
    let (stderr, stderr_writer) = UnixStream::pair().ok()?;
    stdout.set_nonblocking(true).ok()?;
    stderr.set_nonblocking(true).ok()?;
    let mut child = command
        .process_group(0)
        .stdout(Stdio::from(OwnedFd::from(stdout_writer)))
        .stderr(Stdio::from(OwnedFd::from(stderr_writer)))
        .spawn()
        .ok()?;
    drop(command);

    let result = (|| -> std::io::Result<Output> {
        let deadline = Instant::now() + timeout;
        let mut streams = [stdout, stderr];
        let mut captured = [Vec::new(), Vec::new()];
        let mut ended = [false; 2];
        let mut buffer = [0_u8; 8192];
        loop {
            for index in 0..2 {
                while !ended[index] {
                    if Instant::now() >= deadline {
                        return Err(ErrorKind::TimedOut.into());
                    }
                    match streams[index].read(&mut buffer) {
                        Ok(0) => ended[index] = true,
                        Ok(count) => {
                            if captured[index].len() + count > 16 * 1024 * 1024 {
                                return Err(ErrorKind::InvalidData.into());
                            }
                            captured[index].extend_from_slice(&buffer[..count]);
                        }
                        Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                        Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                        Err(error) => return Err(error),
                    }
                }
            }
            // Keep the leader unreaped while a descendant may hold either stream.
            // Its owned process group can then be terminated safely at the deadline.
            if ended == [true, true] {
                if let Some(status) = child.try_wait()? {
                    let [stdout, stderr] = captured;
                    return Ok(Output {
                        status,
                        stdout,
                        stderr,
                    });
                }
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(ErrorKind::TimedOut.into());
            }
            // Background macOS services coalesce sleep timers. Wait on output
            // readiness instead of turning a fast child into a timed-out scan.
            let mut poll_fds: [libc::pollfd; 2] = std::array::from_fn(|index| libc::pollfd {
                fd: if ended[index] {
                    -1
                } else {
                    streams[index].as_raw_fd()
                },
                events: libc::POLLIN,
                revents: 0,
            });
            let wait = if ended == [true, true] {
                remaining.min(Duration::from_millis(5))
            } else {
                remaining
            };
            let wait_ms = wait.as_millis().clamp(1, libc::c_int::MAX as u128) as libc::c_int;
            let polled = unsafe {
                libc::poll(
                    poll_fds.as_mut_ptr(),
                    poll_fds.len() as libc::nfds_t,
                    wait_ms,
                )
            };
            if polled < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() != ErrorKind::Interrupted {
                    return Err(error);
                }
            }
        }
    })();
    if result.is_err() {
        if let Ok(pgid) = i32::try_from(child.id()) {
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    result.ok()
}

#[cfg(not(unix))]
pub(crate) fn output_with_timeout(mut command: Command, timeout: Duration) -> Option<Output> {
    // Lead a process group so the timeout can reach the whole tree. Without
    // this the helper is only bounded on platforms where the shell execs its
    // single command; see the kill path below.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
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
            // Kill the group, not just the leader. A shell that forks rather
            // than execs its command leaves that child holding the stdout pipe,
            // so killing only the leader leaves the reader thread blocked on a
            // pipe nobody will close — and this "bounded" helper then waits out
            // the full runtime of the very process it was abandoning. On Linux
            // that made the process-inventory timeout inert: a stalled `ps`
            // would have blocked the scan for as long as it wanted to run.
            #[cfg(unix)]
            if let Ok(pgid) = i32::try_from(child.id()) {
                unsafe {
                    libc::killpg(pgid, libc::SIGKILL);
                }
            }
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
                        | "pi"
                        | "omp"
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

/// Collect a coherent process-lineage inventory. `None` distinguishes a
/// failed native process scan or `ps` fallback from a genuinely empty result,
/// so callers never read a scan failure as evidence of anything.
#[cfg(target_os = "macos")]
pub fn try_collect_process_lineage() -> Option<Vec<ProcessLineage>> {
    macos::try_collect_process_lineage()
}

#[cfg(not(target_os = "macos"))]
pub fn try_collect_process_lineage() -> Option<Vec<ProcessLineage>> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,pgid=,stat=,command="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line_count = text.lines().filter(|line| !line.trim().is_empty()).count();
    let entries: Vec<ProcessLineage> = text.lines().filter_map(parse_process_lineage).collect();
    if entries.len() != line_count || !entries.iter().any(|e| e.pid == std::process::id()) {
        return None;
    }
    Some(entries)
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

    #[test]
    fn group_membership_parse_counts_only_non_zombie_members() {
        let text = "  41 Ss\n  41 Z\n  77 R+\n";
        assert_eq!(group_has_non_zombie_in(text, 41), Some(true));
        assert_eq!(group_has_non_zombie_in("  41 Z\n  77 S\n", 41), Some(false));
        assert_eq!(
            group_has_non_zombie_in("  41 T\n", 41),
            Some(true),
            "stopped is still a member"
        );
        assert_eq!(group_has_non_zombie_in("  77 S\n", 41), Some(false));
        assert_eq!(group_has_non_zombie_in("garbage\n", 41), None);
        assert_eq!(group_has_non_zombie_in("  41\n", 41), None);
    }

    use super::*;

    #[test]
    fn bounded_command_output_returns_successful_output() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let mut command = Command::new("sh");
        command.args(["-c", "printf '%262144s' ready; printf diagnostic >&2"]);
        let output = output_with_timeout(command, Duration::from_secs(1)).unwrap();
        assert!(output.status.success());
        let mut expected = vec![b' '; 262144];
        expected[262139..].copy_from_slice(b"ready");
        assert!(
            output.stdout == expected,
            "stdout must survive output backpressure"
        );
        #[cfg(unix)]
        assert_eq!(output.stderr, b"diagnostic");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn bounded_command_output_works_in_a_background_process() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let mut command = Command::new("/usr/sbin/taskpolicy");
        command
            .arg("-b")
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "process_identity::tests::bounded_command_output_returns_successful_output",
            ]);
        let output = output_with_timeout(command, Duration::from_secs(5)).unwrap();
        assert!(
            output.status.success(),
            "background output capture failed: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }

    #[test]
    fn bounded_command_output_terminates_a_stalled_process() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        // The claim is "gives up long before the child would finish", so the
        // gap between the two has to be big enough that a loaded shared CI
        // runner cannot close it. A 1s sleep against a 500ms bound left 500ms
        // of headroom and went red on ubuntu-latest; 5s against 2s proves the
        // same property with room to spare.
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 5"]);
        let started = Instant::now();
        assert!(output_with_timeout(command, Duration::from_millis(10)).is_none());
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "returned after {:?}, which is not bounded termination",
            started.elapsed()
        );
    }

    #[cfg(unix)]
    #[test]
    fn bounded_command_output_bounds_streams_after_leader_exit() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 1 & exit 0"]);
        let started = Instant::now();
        assert!(output_with_timeout(command, Duration::from_millis(100)).is_none());
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn inventory_keeps_start_time_for_bare_pi_and_omp() {
        // Unmanaged discovery drops a fact without a start time, and it
        // recognizes bare `pi`/`omp` launches (unmanaged_bindings).
        for command in ["pi --continue", "/opt/homebrew/bin/omp", "codex resume"] {
            let line = format!("  4242 ttys000  S+   Tue May  5 11:58:00 2026 {command}");
            let (_, fact) = parse_process_fact_for_inventory(&line).unwrap();
            assert!(fact.start_time.is_some(), "{command}");
        }
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
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let pid = std::process::id();
        let targeted = try_collect_process_fact(pid).expect("targeted process identity");
        let inventory = try_collect_process_facts_by_pid().expect("full process inventory");
        let full = inventory.get(&pid).expect("current process in inventory");

        // Identity, not instantaneous state: `stat` is the process's state at the
        // moment `ps` looked, and the two probes are two moments. This process is
        // the test runner, which is running while one probe reads it and can be
        // sleeping while the other does, so comparing `stat` between them asserts
        // a race rather than an identity.
        assert_eq!(targeted.pid, full.pid);
        assert_eq!(targeted.tty, full.tty);
        assert_eq!(targeted.lstart, full.lstart);
        assert_eq!(targeted.command, full.command);
        assert!(!targeted.stat.trim().is_empty());
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
    #[cfg(target_os = "macos")]
    #[test]
    fn macos_libproc_inventory_matches_ps_and_lsof() {
        use std::os::fd::AsRawFd;

        let _guard = crate::console_adapter::agent_state_guard();
        let ps_started = Instant::now();

        fn identity_matches(actual: &ProcessFact, expected: &ProcessFact) -> bool {
            actual.pid == expected.pid
                && actual.tty == expected.tty
                && actual.lstart == expected.lstart
                && actual.command == expected.command
        }

        fn compare_identity(actual: &ProcessFact, expected: &ProcessFact) {
            assert_eq!(actual.pid, expected.pid);
            assert_eq!(actual.tty, expected.tty);
            assert_eq!(actual.lstart, expected.lstart);
            assert_eq!(actual.command, expected.command);
        }

        // kinfo_proc reports every live process as SRUN; ps derives R/S/I from
        // per-thread run states. Callers only read stopped (T), zombie (Z)
        // and the foreground-group `+`, so those are the axes compared.
        fn compare_stat(actual: &ProcessFact, expected: &ProcessFact) {
            let first = |fact: &ProcessFact| fact.stat.chars().next();
            assert_eq!(first(actual) == Some('T'), first(expected) == Some('T'));
            assert_eq!(first(actual) == Some('Z'), first(expected) == Some('Z'));
            assert_eq!(actual.stat.contains('+'), expected.stat.contains('+'));
        }

        // `/bin/ps` is setuid root, so it reads every process's argv; an
        // unprivileged engine can only read its own user's. Provider processes
        // always run as the user, so the oracle is processes whose effective
        // uid is ours. The uid column rides on the same scan, so a setuid
        // child (e.g. a `ps` the live engine runs) can't race the filter.
        fn read_ps_facts() -> HashMap<u32, ProcessFact> {
            let uid = unsafe { libc::geteuid() };
            let output = Command::new("ps")
                .args(["-axo", "uid=,pid=,tty=,stat=,lstart=,command="])
                .output()
                .expect("run full ps oracle");
            assert!(
                output.status.success(),
                "ps failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            let mut facts = HashMap::new();
            for line in String::from_utf8_lossy(&output.stdout)
                .lines()
                .filter(|line| !line.trim().is_empty())
            {
                let line = line.trim_start();
                let (euid, rest) = line.split_once(char::is_whitespace).expect("uid column");
                // ps prints `nobody` as -2.
                if euid.parse::<i64>().expect("numeric uid") != i64::from(uid) {
                    continue;
                }
                let (pid, fact) = parse_process_fact(rest).expect("parse full ps output");
                assert!(facts.insert(pid, fact).is_none(), "duplicate ps pid {pid}");
            }
            facts
        }

        fn ps_fact_for_pid(pid: u32) -> Option<ProcessFact> {
            let output = Command::new("ps")
                .args([
                    "-p",
                    &pid.to_string(),
                    "-o",
                    "pid=,tty=,stat=,lstart=,command=",
                ])
                .output()
                .expect("run one-pid ps oracle");
            if !output.status.success() {
                assert_eq!(output.status.code(), Some(1));
                assert!(output.stderr.is_empty());
                return None;
            }
            let text = String::from_utf8_lossy(&output.stdout);
            let mut lines = text.lines().filter(|line| !line.trim().is_empty());
            let Some(line) = lines.next() else {
                return None;
            };
            let (parsed_pid, fact) = parse_process_fact(line).expect("parse one-pid ps output");
            assert_eq!(parsed_pid, pid);
            assert!(lines.next().is_none());
            Some(fact)
        }

        let ps_before = read_ps_facts();
        let ps_elapsed = ps_started.elapsed();
        let backend_started = Instant::now();
        let inventory =
            try_collect_process_facts_by_pid().expect("complete libproc process inventory");
        let backend_elapsed = backend_started.elapsed();
        eprintln!(
            "process inventory benchmark ({} processes): libproc {:?}; ps -axo {:?}",
            inventory.len(),
            backend_elapsed,
            ps_elapsed
        );
        let ps_after = read_ps_facts();
        assert!(inventory.contains_key(&std::process::id()));
        for (pid, fact) in &inventory {
            let before = ps_before
                .get(pid)
                .filter(|oracle| oracle.lstart == fact.lstart);
            let after = ps_after
                .get(pid)
                .filter(|oracle| oracle.lstart == fact.lstart);
            let oracle = before
                .filter(|oracle| identity_matches(fact, oracle))
                .or_else(|| after.filter(|oracle| identity_matches(fact, oracle)))
                .cloned()
                .or_else(|| {
                    ps_fact_for_pid(*pid)
                        .filter(|oracle| oracle.lstart == fact.lstart)
                        .filter(|oracle| identity_matches(fact, oracle))
                });
            if let Some(oracle) = oracle {
                compare_identity(fact, &oracle);
            } else if let (Some(before), Some(after)) = (before, after) {
                if before.command != after.command {
                    continue;
                }
                // Both ps reads agree, so the process did not exec across
                // the window; a short-lived process that exec'd between reads
                // legitimately shows either command.
                panic!(
                    "libproc identity mismatch for exec-stable PID {pid}: libproc={fact:?} ps={before:?}"
                );
            }
            if let (Some(before), Some(after)) = (before, after) {
                if before.stat == after.stat && identity_matches(fact, before) {
                    compare_stat(fact, before);
                }
            }
        }
        for (pid, earlier) in &ps_before {
            if inventory
                .get(pid)
                .is_some_and(|fact| fact.lstart == earlier.lstart)
            {
                continue;
            }
            if let Some(current) = ps_fact_for_pid(*pid) {
                assert_ne!(
                    current.lstart, earlier.lstart,
                    "libproc omitted still-live PID {pid}"
                );
            }
        }

        let pid = std::process::id();
        let cwd = std::env::current_dir().unwrap();
        let cwd = cwd.to_string_lossy().into_owned();
        let probed_cwd = try_collect_process_cwds(&[pid])
            .expect("complete cwd probe")
            .remove(&pid)
            .expect("test process cwd");
        assert_eq!(probed_cwd, cwd);
        let cwd_oracle = Command::new("lsof")
            .args(["-a", "-d", "cwd", "-F", "pn", "-p", &pid.to_string()])
            .output()
            .expect("run lsof cwd oracle");
        assert!(
            cwd_oracle.status.success(),
            "lsof cwd failed: {}",
            String::from_utf8_lossy(&cwd_oracle.stderr)
        );
        let cwd_text = String::from_utf8_lossy(&cwd_oracle.stdout);
        let cwd_from_lsof = cwd_text
            .lines()
            .find_map(|line| line.strip_prefix('n'))
            .expect("lsof cwd path");
        assert_eq!(probed_cwd, cwd_from_lsof);

        let file = tempfile::NamedTempFile::new().expect("create open-file oracle");
        // Kernel and lsof report the resolved path (/private/var/...).
        let expected_path = file
            .path()
            .canonicalize()
            .expect("resolve open-file oracle");
        let descriptor = file.as_file().as_raw_fd().to_string();
        let file_oracle = Command::new("lsof")
            .args(["-a", "-d", &descriptor, "-F", "pn", "-p", &pid.to_string()])
            .output()
            .expect("run lsof open-file oracle");
        assert!(
            file_oracle.status.success(),
            "lsof open-file probe failed: {}",
            String::from_utf8_lossy(&file_oracle.stderr)
        );
        let file_text = String::from_utf8_lossy(&file_oracle.stdout);
        let path_from_lsof = file_text
            .lines()
            .find_map(|line| line.strip_prefix('n'))
            .expect("lsof open-file path");
        assert_eq!(Path::new(path_from_lsof), expected_path);
        let open_files =
            try_collect_process_open_files(&[pid]).expect("complete libproc open-file inventory");
        assert!(
            open_files
                .get(&pid)
                .is_some_and(|paths| paths.contains(&expected_path)),
            "libproc did not find the open file {expected_path:?}"
        );
    }
}
