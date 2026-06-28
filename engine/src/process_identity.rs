//! Shared process identity helpers for PID-reuse-safe liveness checks.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use chrono::DateTime;
use chrono::Utc;

pub const PID_REUSE_TOLERANCE_SECS: i64 = 120;

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
        !tty.is_empty() && tty != "??" && self.stat.contains('+')
    }
}

pub fn collect_process_facts_by_pid() -> HashMap<u32, ProcessFact> {
    let Ok(output) = Command::new("ps")
        .args(["-axo", "pid=,tty=,stat=,lstart=,command="])
        .output()
    else {
        return HashMap::new();
    };
    if !output.status.success() {
        return HashMap::new();
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(parse_process_fact)
        .collect()
}

/// Parse one `ps -axo pid=,tty=,stat=,lstart=,command=` line.
///
/// `pid`, `tty`, and `stat` are whitespace-delimited single tokens; `lstart` is
/// a fixed-width 24-char field like `Sun Apr 27 10:15:23 2026`; `command` is the
/// remainder (and may contain spaces), so it stays last.
pub fn parse_process_fact(line: &str) -> Option<(u32, ProcessFact)> {
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
    Some((
        pid,
        ProcessFact {
            pid,
            tty: tty.to_string(),
            stat: stat.to_string(),
            lstart: lstart.clone(),
            command,
            start_time: parse_lstart(&lstart),
        },
    ))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_process_fact_extracts_pid_tty_stat_lstart_command() {
        let line = "  4242 ttys000  S+   Mon May  5 11:58:00 2026 opencode serve --hostname 127.0.0.1";
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
    }

    #[test]
    fn lstart_parses_current_year() {
        let parsed = parse_lstart("Mon Apr 27 10:15:23 2026").unwrap();
        assert_eq!(parsed.date_naive().to_string(), "2026-04-27");
    }
}
