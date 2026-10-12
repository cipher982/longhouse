//! Linux process identity and fd inspection using `/proc`.
//!
//! A missing or unreadable PID is absent. A failed table walk or malformed
//! readable process record remains unknown instead of becoming an empty scan.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use chrono::{Local, TimeZone, Utc};

use super::{
    ProcessFact, ProcessFactLookup, ProcessLineage, ProcessTreeEntry,
    TARGETED_PROCESS_FACT_BATCH_MAX,
};

const PROC_ROOT: &str = "/proc";

#[derive(Debug)]
struct ProcStat {
    pid: u32,
    comm: String,
    state: char,
    ppid: u32,
    pgrp: i32,
    session: u32,
    tty_nr: i64,
    tpgid: i32,
    priority: i64,
    nice: i64,
    thread_count: u64,
    start_ticks: u64,
}

pub(super) fn inspect_process_fact(pid: u32) -> ProcessFactLookup {
    if pid == 0 {
        return ProcessFactLookup::Absent;
    }
    let mut buffer = Vec::with_capacity(512);
    let stat = match read_pid_stat(pid, &mut buffer, true) {
        Ok(Some(stat)) => stat,
        Ok(None) => return ProcessFactLookup::Absent,
        Err(()) => return ProcessFactLookup::Unavailable,
    };
    let Some(boot_time) = boot_time_seconds() else {
        return ProcessFactLookup::Unavailable;
    };
    let Some(ticks_per_second) = clock_ticks_per_second() else {
        return ProcessFactLookup::Unavailable;
    };
    let mut process_dir = pid_directory(pid);
    let Some(command) = read_process_command(&mut process_dir, &mut buffer, &stat.comm, stat.state)
    else {
        return ProcessFactLookup::Absent;
    };
    match process_fact(
        &stat,
        command,
        boot_time,
        ticks_per_second,
        true,
        &process_dir,
    ) {
        Some(fact) => ProcessFactLookup::Present(fact),
        None => ProcessFactLookup::Unavailable,
    }
}

pub(super) fn try_collect_process_facts_by_pid() -> Option<HashMap<u32, ProcessFact>> {
    let boot_time = boot_time_seconds()?;
    let ticks_per_second = clock_ticks_per_second()?;
    let mut facts = HashMap::new();
    let mut found_current = false;
    walk_processes(true, |pid, stat, process_dir, buffer| {
        let Some(command) = read_process_command(process_dir, buffer, &stat.comm, stat.state)
        else {
            return Some(());
        };
        let fact = process_fact(
            stat,
            command,
            boot_time,
            ticks_per_second,
            false,
            process_dir,
        )?;
        if pid == std::process::id() {
            found_current = true;
        }
        facts.insert(pid, fact);
        Some(())
    })?;
    found_current.then_some(facts)
}

pub(super) fn try_group_has_non_zombie(pgid: i32) -> Option<bool> {
    let mut found_current = false;
    let mut non_zombie = false;
    walk_processes(false, |pid, stat, _, _| {
        found_current |= pid == std::process::id();
        if stat.pgrp == pgid && stat.state != 'Z' {
            non_zombie = true;
        }
        Some(())
    })?;
    found_current.then_some(non_zombie)
}

pub(super) fn try_collect_process_start_keys(pids: &[u32]) -> Option<HashMap<u32, String>> {
    let mut requested: Vec<u32> = pids.iter().copied().filter(|pid| *pid > 1).collect();
    requested.sort_unstable();
    requested.dedup();
    if requested.is_empty() {
        return Some(HashMap::new());
    }
    let boot_time = boot_time_seconds()?;
    let ticks_per_second = clock_ticks_per_second()?;
    let mut starts = HashMap::with_capacity(requested.len());
    if requested.len() > TARGETED_PROCESS_FACT_BATCH_MAX {
        let mut found_current = false;
        walk_processes(false, |pid, stat, _, _| {
            found_current |= pid == std::process::id();
            if requested.binary_search(&pid).is_ok() {
                starts.insert(
                    pid,
                    format_lstart(start_epoch_seconds(stat, boot_time, ticks_per_second)?)?,
                );
            }
            Some(())
        })?;
        return found_current.then_some(starts);
    }
    let mut buffer = Vec::with_capacity(512);
    for pid in requested {
        let Some(stat) = read_pid_stat(pid, &mut buffer, false).ok()? else {
            continue;
        };
        starts.insert(
            pid,
            format_lstart(start_epoch_seconds(&stat, boot_time, ticks_per_second)?)?,
        );
    }
    Some(starts)
}

pub(super) fn try_collect_process_facts_for_pids(
    pids: &[u32],
) -> Option<HashMap<u32, ProcessFact>> {
    if pids.len() > TARGETED_PROCESS_FACT_BATCH_MAX || pids.iter().any(|pid| *pid <= 1) {
        return None;
    }
    let mut requested = pids.to_vec();
    requested.sort_unstable();
    requested.dedup();
    if requested.is_empty() {
        return Some(HashMap::new());
    }

    let boot_time = boot_time_seconds()?;
    let ticks_per_second = clock_ticks_per_second()?;
    let mut facts = HashMap::with_capacity(requested.len());
    let mut buffer = Vec::with_capacity(512);
    for pid in requested {
        let Some(stat) = read_pid_stat(pid, &mut buffer, true).ok()? else {
            continue;
        };
        let mut process_dir = pid_directory(pid);
        let Some(command) =
            read_process_command(&mut process_dir, &mut buffer, &stat.comm, stat.state)
        else {
            continue;
        };
        facts.insert(
            pid,
            process_fact(
                &stat,
                command,
                boot_time,
                ticks_per_second,
                false,
                &process_dir,
            )?,
        );
    }
    Some(facts)
}

pub(super) fn process_comm_and_parent(pid: u32) -> Option<(String, u32)> {
    let mut buffer = Vec::with_capacity(512);
    let stat = read_pid_stat(pid, &mut buffer, true).ok()??;
    let mut names = stat.comm.split_whitespace();
    let comm = names.next()?.to_string();
    if names.next().is_some() {
        return None;
    }
    Some((comm, stat.ppid))
}

pub(super) fn try_collect_process_cwds(pids: &[u32]) -> Option<HashMap<u32, String>> {
    if pids.iter().any(|pid| *pid <= 1) {
        return None;
    }
    let mut requested = pids.to_vec();
    requested.sort_unstable();
    requested.dedup();
    let mut cwds = HashMap::with_capacity(requested.len());
    for pid in requested {
        let path = pid_directory(pid).join("cwd");
        match fs::read_link(path) {
            Ok(cwd) => {
                let cwd = cwd.to_string_lossy().into_owned();
                if !cwd.trim().is_empty() {
                    cwds.insert(pid, cwd);
                }
            }
            Err(_) => {}
        }
    }
    Some(cwds)
}

pub(super) fn try_collect_process_open_files(pids: &[u32]) -> Option<HashMap<u32, Vec<PathBuf>>> {
    if pids.iter().any(|pid| *pid <= 1) {
        return None;
    }
    let mut requested = pids.to_vec();
    requested.sort_unstable();
    requested.dedup();
    let mut files = HashMap::with_capacity(requested.len());
    for pid in requested {
        let fd_dir = pid_directory(pid).join("fd");
        let entries = match fs::read_dir(fd_dir) {
            Ok(entries) => entries,
            Err(error) if process_vanished(&error) => continue,
            Err(_) => return None,
        };
        let mut paths = Vec::new();
        for entry in entries {
            let entry = entry.ok()?;
            match fs::read_link(entry.path()) {
                Ok(path) => paths.push(path),
                Err(error) if process_vanished(&error) => {}
                Err(_) => return None,
            }
        }
        files.insert(pid, paths);
    }
    Some(files)
}

pub(super) fn try_collect_process_lineage() -> Option<Vec<ProcessLineage>> {
    let mut entries = Vec::new();
    let mut found_current = false;
    walk_processes(true, |pid, stat, process_dir, buffer| {
        let Some(command) = read_process_command(process_dir, buffer, &stat.comm, stat.state)
        else {
            return Some(());
        };
        found_current |= pid == std::process::id();
        entries.push(ProcessLineage {
            pid,
            ppid: stat.ppid,
            pgid: stat.pgrp,
            stat: process_stat(stat),
            command,
        });
        Some(())
    })?;
    found_current.then_some(entries)
}

pub(super) fn try_collect_process_tree() -> Option<Vec<ProcessTreeEntry>> {
    let mut entries = Vec::new();
    let mut found_current = false;
    walk_processes(false, |pid, stat, _, _| {
        found_current |= pid == std::process::id();
        entries.push(ProcessTreeEntry {
            pid,
            ppid: stat.ppid,
            pgid: stat.pgrp,
        });
        Some(())
    })?;
    found_current.then_some(entries)
}

fn process_fact(
    stat: &ProcStat,
    command: String,
    boot_time: i64,
    ticks_per_second: u64,
    parse_all_start_times: bool,
    process_dir: &Path,
) -> Option<ProcessFact> {
    let start_epoch = start_epoch_seconds(stat, boot_time, ticks_per_second)?;
    let start_time = Utc.timestamp_opt(start_epoch, 0).single()?;
    let start_time_for_caller =
        (parse_all_start_times || super::command_needs_start_time(&command)).then_some(start_time);
    Some(ProcessFact {
        pid: stat.pid,
        tty: tty_name(stat.tty_nr, process_dir),
        stat: process_stat(stat),
        lstart: start_time
            .with_timezone(&Local)
            .format("%a %b %e %H:%M:%S %Y")
            .to_string(),
        command,
        start_time: start_time_for_caller,
    })
}

fn process_stat(stat: &ProcStat) -> String {
    let mut value = String::with_capacity(7);
    value.push(stat.state);
    if stat.priority < 0 {
        value.push('<');
    }
    if stat.nice > 0 {
        value.push('N');
    }
    if stat.pid == stat.session {
        value.push('s');
    }
    if stat.thread_count > 1 {
        value.push('l');
    }
    if stat.tty_nr != 0 && stat.tpgid == stat.pgrp {
        value.push('+');
    }
    value
}

fn start_epoch_seconds(stat: &ProcStat, boot_time: i64, ticks_per_second: u64) -> Option<i64> {
    let elapsed = i64::try_from(stat.start_ticks / ticks_per_second).ok()?;
    boot_time.checked_add(elapsed)
}

fn format_lstart(epoch_seconds: i64) -> Option<String> {
    let start = Utc.timestamp_opt(epoch_seconds, 0).single()?;
    Some(
        start
            .with_timezone(&Local)
            .format("%a %b %e %H:%M:%S %Y")
            .to_string(),
    )
}

fn boot_time_seconds() -> Option<i64> {
    let bytes = fs::read(Path::new(PROC_ROOT).join("stat")).ok()?;
    bytes.split(|byte| *byte == b'\n').find_map(|line| {
        let value = line.strip_prefix(b"btime ")?;
        std::str::from_utf8(value).ok()?.trim().parse().ok()
    })
}

fn clock_ticks_per_second() -> Option<u64> {
    static CLOCK_TICKS: LazyLock<Option<u64>> = LazyLock::new(|| {
        let ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
        u64::try_from(ticks).ok().filter(|ticks| *ticks > 0)
    });
    *CLOCK_TICKS
}

fn read_pid_stat(
    pid: u32,
    buffer: &mut Vec<u8>,
    include_comm: bool,
) -> Result<Option<ProcStat>, ()> {
    let mut path = pid_directory(pid);
    path.push("stat");
    match read_proc_file(&path, buffer) {
        Ok(()) => {
            let stat = parse_proc_stat(buffer, include_comm).ok_or(())?;
            Ok((stat.pid == pid).then_some(stat))
        }
        Err(_) => Ok(None),
    }
}

fn walk_processes(
    include_comm: bool,
    visit: impl FnMut(u32, &ProcStat, &mut PathBuf, &mut Vec<u8>) -> Option<()>,
) -> Option<()> {
    walk_processes_at(Path::new(PROC_ROOT), include_comm, visit)
}

fn walk_processes_at(
    proc_root: &Path,
    include_comm: bool,
    mut visit: impl FnMut(u32, &ProcStat, &mut PathBuf, &mut Vec<u8>) -> Option<()>,
) -> Option<()> {
    let entries = fs::read_dir(proc_root).ok()?;
    let mut path = proc_root.to_path_buf();
    let mut buffer = Vec::with_capacity(512);
    for entry in entries {
        let entry = entry.ok()?;
        let name = entry.file_name();
        let Some(pid) = name.to_str().and_then(|name| name.parse::<u32>().ok()) else {
            continue;
        };
        if pid == 0 {
            continue;
        }
        path.push(name);
        path.push("stat");
        let read_result = read_proc_file(&path, &mut buffer);
        path.pop();
        if read_result.is_err() {
            path.pop();
            continue;
        }
        let stat = parse_proc_stat(&buffer, include_comm)?;
        if stat.pid == pid {
            visit(pid, &stat, &mut path, &mut buffer)?;
        }
        path.pop();
    }
    Some(())
}

fn read_proc_file(path: &Path, buffer: &mut Vec<u8>) -> io::Result<()> {
    buffer.clear();
    File::open(path)?.read_to_end(buffer)?;
    Ok(())
}

fn parse_proc_stat(bytes: &[u8], include_comm: bool) -> Option<ProcStat> {
    let open = bytes.iter().position(|byte| *byte == b'(')?;
    let close = bytes.iter().rposition(|byte| *byte == b')')?;
    if close <= open {
        return None;
    }
    let pid = std::str::from_utf8(&bytes[..open])
        .ok()?
        .trim()
        .parse()
        .ok()?;
    let comm = if include_comm {
        String::from_utf8_lossy(&bytes[open + 1..close]).into_owned()
    } else {
        String::new()
    };
    let after_comm = std::str::from_utf8(&bytes[close + 1..]).ok()?;
    let mut fields = after_comm.split_ascii_whitespace();
    let state = fields.next()?.chars().next()?;
    let ppid = fields.next()?.parse().ok()?;
    let pgrp = fields.next()?.parse().ok()?;
    let session = fields.next()?.parse().ok()?;
    let tty_nr = fields.next()?.parse().ok()?;
    let tpgid = fields.next()?.parse().ok()?;
    // After tpgid (field 8), skip flags through cstime (fields 9-17).
    for _ in 0..9 {
        fields.next()?;
    }
    let priority = fields.next()?.parse().ok()?;
    let nice = fields.next()?.parse().ok()?;
    let thread_count = fields.next()?.parse().ok()?;
    fields.next()?;
    let start_ticks = fields.next()?.parse().ok()?;
    Some(ProcStat {
        pid,
        comm,
        state,
        ppid,
        pgrp,
        session,
        tty_nr,
        tpgid,
        priority,
        nice,
        thread_count,
        start_ticks,
    })
}

fn read_process_command(
    process_dir: &mut PathBuf,
    buffer: &mut Vec<u8>,
    comm: &str,
    state: char,
) -> Option<String> {
    process_dir.push("cmdline");
    let read_result = read_proc_file(process_dir, buffer);
    process_dir.pop();
    read_result.ok()?;
    if buffer.is_empty() {
        return Some(fallback_process_command(comm, state));
    }
    let bytes = buffer.strip_suffix(&[0]).unwrap_or(buffer);
    let mut command = String::with_capacity(bytes.len());
    for (index, argument) in bytes.split(|byte| *byte == 0).enumerate() {
        if index != 0 {
            command.push(' ');
        }
        command.push_str(&String::from_utf8_lossy(argument));
    }
    let leading = command.len() - command.trim_start().len();
    let end = command.trim_end().len();
    if leading >= end {
        return Some(fallback_process_command(comm, state));
    }
    command.truncate(end);
    if leading != 0 {
        command.drain(..leading);
    }
    Some(command)
}

fn fallback_process_command(comm: &str, state: char) -> String {
    if state == 'Z' {
        format!("[{comm}] <defunct>")
    } else {
        format!("[{comm}]")
    }
}

fn tty_name(tty_nr: i64, process_dir: &Path) -> String {
    if tty_nr == 0 {
        return "?".to_string();
    }
    if let Some(name) = tty_name_from_open_fd(process_dir, tty_nr) {
        return name;
    }
    let (major, minor) = decode_tty_device(tty_nr);
    if let Some(name) = tty_name_from_sysfs(major, minor) {
        return name;
    }
    if (136..=143).contains(&major) {
        let pty = if major == 136 {
            minor
        } else {
            (major - 136) * 256 + minor
        };
        return format!("pts/{pty}");
    }
    match (major, minor) {
        (4, 0) => "tty0".to_string(),
        (4, minor @ 1..=63) => format!("tty{minor}"),
        (4, minor @ 64..) => format!("ttyS{}", minor - 64),
        (5, 0) => "tty".to_string(),
        (5, 1) => "console".to_string(),
        (5, 2) => "ptmx".to_string(),
        _ => "?".to_string(),
    }
}

fn tty_name_from_open_fd(process_dir: &Path, tty_nr: i64) -> Option<String> {
    let fd_dir = process_dir.join("fd");
    let entries = fs::read_dir(fd_dir).ok()?;
    for entry in entries {
        let Ok(entry) = entry else {
            continue;
        };
        let fd_path = entry.path();
        let Ok(metadata) = fs::metadata(&fd_path) else {
            continue;
        };
        if !metadata.file_type().is_char_device() || !tty_device_matches(tty_nr, metadata.rdev()) {
            continue;
        }
        let Ok(target) = fs::read_link(fd_path) else {
            continue;
        };
        let target = target.to_string_lossy();
        let Some(name) = target.strip_prefix("/dev/") else {
            continue;
        };
        let name = name.strip_suffix(" (deleted)").unwrap_or(name);
        if !name.is_empty() {
            return Some(name.to_string());
        }
    }
    None
}

fn tty_name_from_sysfs(major: u32, minor: u32) -> Option<String> {
    let target = fs::read_link(format!("/sys/dev/char/{major}:{minor}")).ok()?;
    let name = target.file_name()?.to_str()?;
    if let Some(number) = name
        .strip_prefix("pts")
        .filter(|number| !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit()))
    {
        Some(format!("pts/{number}"))
    } else {
        Some(name.to_string())
    }
}

fn tty_device_matches(tty_nr: i64, device: u64) -> bool {
    let (tty_major, tty_minor) = decode_tty_device(tty_nr);
    let device_major = libc::major(device) as u32;
    let device_minor = libc::minor(device) as u32;
    if tty_major == device_major && tty_minor == device_minor {
        return true;
    }
    (136..=143).contains(&tty_major)
        && device_major == 136
        && device_minor == (tty_major - 136) * 256 + tty_minor
}

fn decode_tty_device(tty_nr: i64) -> (u32, u32) {
    let encoded = tty_nr as u32;
    let major = (encoded >> 8) & 0x0fff;
    let minor = (encoded & 0x00ff) | ((encoded >> 12) & 0x0fff00);
    (major, minor)
}

fn process_vanished(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::NotFound | io::ErrorKind::InvalidInput
    ) || error.raw_os_error() == Some(libc::ESRCH)
}

fn pid_directory(pid: u32) -> PathBuf {
    Path::new(PROC_ROOT).join(pid.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_process_table_read_is_unknown_not_empty() {
        let temporary = tempfile::tempdir().unwrap();
        let missing_proc = temporary.path().join("proc");
        let mut visited = false;
        assert!(walk_processes_at(&missing_proc, false, |_, _, _, _| {
            visited = true;
            Some(())
        })
        .is_none());
        assert!(!visited);
    }
}
