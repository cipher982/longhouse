use std::collections::{HashMap, HashSet};
use std::ffi::CStr;
use std::mem::MaybeUninit;
use std::path::PathBuf;
use std::ptr;

use chrono::{DateTime, TimeZone, Utc};

use super::{ProcessFact, ProcessFactLookup, ProcessLineage, TARGETED_PROCESS_FACT_BATCH_MAX};

const PROC_PIDFDVNODEPATHINFO: libc::c_int = 2;
const INITIAL_FD_CAPACITY: usize = 32;
const MAX_ENUMERATION_RETRIES: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProbeError {
    Absent,
    Unavailable,
}

#[repr(C)]
struct ProcFileInfo {
    _fi_openflags: u32,
    _fi_status: u32,
    _fi_offset: libc::off_t,
    fi_type: libc::c_int,
    _fi_guardflags: u32,
}

#[repr(C)]
struct VnodeFdInfoWithPath {
    pfi: ProcFileInfo,
    pvip: libc::vnode_info_path,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoTimeval {
    tv_sec: i64,
    tv_usec: i32,
    _padding: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoPcred {
    _pc_lock: [libc::c_char; 72],
    _pc_ucred: *mut libc::c_void,
    _p_ruid: u32,
    _p_svuid: u32,
    _p_rgid: u32,
    _p_svgid: u32,
    _p_refcnt: i32,
    _padding: [u8; 4],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoUcred {
    _cr_ref: i32,
    uid: u32,
    _cr_ngroups: i16,
    _cr_groups: [u32; 16],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoVmspace {
    _dummy: i32,
    _dummy2: *mut libc::c_char,
    _dummy3: [i32; 5],
    _dummy4: [*mut libc::c_char; 3],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoExternProc {
    p_starttime: KinfoTimeval,
    _p_vmspace: *mut libc::c_void,
    _p_sigacts: *mut libc::c_void,
    _p_flag: i32,
    p_stat: i8,
    _padding: [u8; 3],
    p_pid: i32,
    _p_oppid: i32,
    _p_dupfd: i32,
    _user_stack: *mut libc::c_void,
    _exit_thread: *mut libc::c_void,
    _p_debugger: i32,
    _sigwait: i32,
    _p_estcpu: u32,
    _p_cpticks: i32,
    _p_pctcpu: u32,
    _p_wchan: *mut libc::c_void,
    _p_wmesg: *mut libc::c_char,
    _p_swtime: u32,
    _p_slptime: u32,
    _p_realtimer: libc::itimerval,
    _p_rtime: libc::timeval,
    _p_uticks: u64,
    _p_sticks: u64,
    _p_iticks: u64,
    _p_traceflag: i32,
    _p_tracep: *mut libc::c_void,
    _p_siglist: i32,
    _p_textvp: *mut libc::c_void,
    _p_holdcnt: i32,
    _p_sigmask: u32,
    _p_sigignore: u32,
    _p_sigcatch: u32,
    _p_priority: u8,
    _p_usrpri: u8,
    _p_nice: i8,
    p_comm: [libc::c_char; 17],
    _p_pgrp: *mut libc::c_void,
    _p_addr: *mut libc::c_void,
    _p_xstat: u16,
    _p_acflag: u16,
    _p_ru: *mut libc::c_void,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoEproc {
    _e_paddr: *mut libc::c_void,
    _e_sess: *mut libc::c_void,
    _e_pcred: KinfoPcred,
    e_ucred: KinfoUcred,
    _e_vm: KinfoVmspace,
    e_ppid: i32,
    e_pgid: i32,
    _e_jobc: i16,
    e_tdev: u32,
    e_tpgid: i32,
    _e_tsess: *mut libc::c_void,
    _e_wmesg: [libc::c_char; 8],
    _e_xsize: i32,
    _e_xrssize: i16,
    _e_xccount: i16,
    _e_xswrss: i16,
    _e_flag: i32,
    _e_login: [libc::c_char; 12],
    _e_spare: [i32; 4],
    _padding: [u8; 4],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KinfoProc {
    kp_proc: KinfoExternProc,
    kp_eproc: KinfoEproc,
}

const _: [(); 0x288] = [(); std::mem::size_of::<KinfoProc>()];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct ProcessCommandIdentity {
    pid: u32,
    start_time_micros: u64,
    comm: [libc::c_char; 17],
}

impl ProcessCommandIdentity {
    fn from_info(info: &KinfoProc) -> Option<Self> {
        let start_time = &info.kp_proc.p_starttime;
        let start_time_micros = u64::try_from(start_time.tv_sec)
            .ok()?
            .checked_mul(1_000_000)?
            .checked_add(u64::try_from(start_time.tv_usec).ok()?)?;
        Some(Self {
            pid: u32::try_from(info.kp_proc.p_pid).ok()?,
            start_time_micros,
            comm: info.kp_proc.p_comm,
        })
    }
}

#[derive(Default)]
struct ProcessCommandCache {
    entries: HashMap<u32, CachedProcessCommand>,
}

struct CachedProcessCommand {
    identity: ProcessCommandIdentity,
    command: String,
}

impl ProcessCommandCache {
    fn command_for(
        &mut self,
        identity: ProcessCommandIdentity,
        is_zombie: bool,
        read: impl FnOnce() -> Result<String, ProbeError>,
    ) -> Result<String, ProbeError> {
        if is_zombie {
            self.entries.remove(&identity.pid);
            return read();
        }
        if let Some(cached) = self
            .entries
            .get(&identity.pid)
            .filter(|cached| cached.identity == identity)
        {
            return Ok(cached.command.clone());
        }
        self.entries.remove(&identity.pid);
        let command = read()?;
        self.entries.insert(
            identity.pid,
            CachedProcessCommand {
                identity,
                command: command.clone(),
            },
        );
        Ok(command)
    }

    fn retain_only_pids(&mut self, pids: &HashSet<u32>) {
        self.entries.retain(|pid, _| pids.contains(pid));
    }
}

static PROCESS_COMMAND_CACHE: std::sync::LazyLock<std::sync::Mutex<ProcessCommandCache>> =
    std::sync::LazyLock::new(|| std::sync::Mutex::new(ProcessCommandCache::default()));

#[cfg(test)]
mod tests {
    use super::{ProcessCommandCache, ProcessCommandIdentity};
    use std::collections::HashSet;

    extern "C" {
        fn revoke(path: *const libc::c_char) -> libc::c_int;
    }

    #[test]
    fn a_revoked_descriptor_does_not_hide_the_process_open_files() {
        let (mut master, mut slave) = (0, 0);
        let mut name = [0 as libc::c_char; 128];
        let opened = unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                name.as_mut_ptr(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        assert_eq!(opened, 0);
        // `slave` stays open, but its vnode no longer has a path.
        assert_eq!(unsafe { revoke(name.as_ptr()) }, 0);
        let held = tempfile::NamedTempFile::new().unwrap();

        let files = super::try_collect_process_open_files(&[std::process::id()]);
        unsafe {
            libc::close(slave);
            libc::close(master);
        }

        let files = files.expect("one revoked descriptor must not void the inventory");
        let held = held.path().canonicalize().unwrap();
        assert!(files[&std::process::id()]
            .iter()
            .any(|path| path.canonicalize().ok().as_ref() == Some(&held)));
    }

    fn test_comm(name: &str) -> [libc::c_char; 17] {
        let mut comm = [0; 17];
        for (byte, source) in comm.iter_mut().zip(name.bytes()) {
            *byte = source as libc::c_char;
        }
        comm
    }

    #[test]
    fn process_command_cache_invalidates_on_exec_and_prunes_missing_pids() {
        let identity = ProcessCommandIdentity {
            pid: 42,
            start_time_micros: 1_234_567,
            comm: test_comm("sh"),
        };
        let mut cache = ProcessCommandCache::default();
        let mut reads = 0;

        assert_eq!(
            cache
                .command_for(identity, false, || {
                    reads += 1;
                    Ok("sh -c cat".to_string())
                })
                .unwrap(),
            "sh -c cat"
        );
        assert_eq!(
            cache
                .command_for(identity, false, || {
                    panic!("unchanged process identity should use the cache")
                })
                .unwrap(),
            "sh -c cat"
        );

        let after_exec = ProcessCommandIdentity {
            comm: test_comm("cat"),
            ..identity
        };
        assert_eq!(
            cache
                .command_for(after_exec, false, || {
                    reads += 1;
                    Ok("cat".to_string())
                })
                .unwrap(),
            "cat"
        );
        assert_eq!(reads, 2, "a changed comm must re-read argv");

        cache.retain_only_pids(&HashSet::from([99]));
        assert!(cache.entries.is_empty(), "disappeared pids must be pruned");
    }
}

pub(super) fn inspect_process_fact(pid: u32) -> ProcessFactLookup {
    match process_fact(pid, true) {
        Ok(Some(fact)) => ProcessFactLookup::Present(fact),
        Ok(None) | Err(ProbeError::Absent) => ProcessFactLookup::Absent,
        Err(ProbeError::Unavailable) => ProcessFactLookup::Unavailable,
    }
}

pub(super) fn try_collect_process_facts_by_pid() -> Option<HashMap<u32, ProcessFact>> {
    let processes = list_kinfo_processes().ok()?;
    let mut enumerated_pids = HashSet::with_capacity(processes.len());
    for info in &processes {
        let pid = u32::try_from(info.kp_proc.p_pid).ok()?;
        if pid != 0 {
            enumerated_pids.insert(pid);
        }
    }

    let mut cache = PROCESS_COMMAND_CACHE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let mut facts = HashMap::with_capacity(processes.len());
    let mut unavailable = false;
    for info in processes {
        let Ok(pid) = u32::try_from(info.kp_proc.p_pid) else {
            unavailable = true;
            break;
        };
        if pid == 0 {
            continue;
        }
        match process_fact_with_info_cached(info, false, &mut cache) {
            Ok(Some(fact)) => {
                facts.insert(pid, fact);
            }
            Ok(None) | Err(ProbeError::Absent) => {}
            Err(ProbeError::Unavailable) => {
                unavailable = true;
                break;
            }
        }
    }
    cache.retain_only_pids(&enumerated_pids);
    if unavailable {
        return None;
    }
    facts.contains_key(&std::process::id()).then_some(facts)
}

pub(super) fn try_group_has_non_zombie(pgid: i32) -> Option<bool> {
    let processes = list_kinfo_processes().ok()?;
    Some(
        processes
            .iter()
            .any(|info| info.kp_eproc.e_pgid == pgid && info.kp_proc.p_stat as u32 != libc::SZOMB),
    )
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

    let mut facts = HashMap::with_capacity(requested.len());
    for pid in requested {
        match process_fact(pid, false) {
            Ok(Some(fact)) => {
                facts.insert(pid, fact);
            }
            Ok(None) | Err(ProbeError::Absent) => {}
            Err(ProbeError::Unavailable) => return None,
        }
    }
    Some(facts)
}

pub(super) fn try_collect_process_start_keys(pids: &[u32]) -> Option<HashMap<u32, String>> {
    let requested: std::collections::HashSet<u32> =
        pids.iter().copied().filter(|pid| *pid > 1).collect();
    if requested.is_empty() {
        return Some(HashMap::new());
    }
    let mut starts = HashMap::with_capacity(requested.len());
    for info in list_kinfo_processes().ok()? {
        let Ok(pid) = u32::try_from(info.kp_proc.p_pid) else {
            continue;
        };
        if !requested.contains(&pid) {
            continue;
        }
        let seconds = u64::try_from(info.kp_proc.p_starttime.tv_sec).ok()?;
        starts.insert(pid, format_lstart(seconds)?);
    }
    Some(starts)
}

pub(super) fn try_collect_process_tree() -> Option<Vec<super::ProcessTreeEntry>> {
    let processes = list_kinfo_processes().ok()?;
    let mut entries = Vec::with_capacity(processes.len());
    for info in processes {
        let pid = u32::try_from(info.kp_proc.p_pid).ok()?;
        if pid == 0 {
            continue;
        }
        entries.push(super::ProcessTreeEntry {
            pid,
            ppid: u32::try_from(info.kp_eproc.e_ppid).ok()?,
            pgid: info.kp_eproc.e_pgid,
        });
    }
    entries
        .iter()
        .any(|entry| entry.pid == std::process::id())
        .then_some(entries)
}

/// Locate the same-user Longhouse menu bar process without reading argv.
///
/// A cached PID is checked directly first; the process table is enumerated only
/// when that PID has exited or its start time no longer matches.
pub(super) fn try_resolve_longhouse_app(cached: Option<(u32, i64)>) -> Option<(u32, i64)> {
    let uid = unsafe { libc::getuid() };
    if let Some((pid, start_time_micros)) = cached {
        if let Some(info) = kinfo_for_pid(pid).ok().flatten() {
            if longhouse_app_identity(&info, uid) == Some((pid, start_time_micros)) {
                return cached;
            }
        }
    }

    list_kinfo_processes()
        .ok()?
        .iter()
        .filter_map(|info| longhouse_app_identity(info, uid))
        .min_by_key(|(pid, _)| *pid)
}

fn longhouse_app_identity(info: &KinfoProc, uid: libc::uid_t) -> Option<(u32, i64)> {
    const LONGHOUSE_COMM: &[u8] = b"Longhouse";
    let pid = u32::try_from(info.kp_proc.p_pid).ok()?;
    let comm = &info.kp_proc.p_comm;
    let name_matches = comm[..LONGHOUSE_COMM.len()]
        .iter()
        .zip(LONGHOUSE_COMM)
        .all(|(byte, expected)| *byte as u8 == *expected)
        && comm[LONGHOUSE_COMM.len()] == 0;
    if pid == 0 || info.kp_eproc.e_ucred.uid != uid || !name_matches {
        return None;
    }
    let start_time = &info.kp_proc.p_starttime;
    let start_time_micros = u64::try_from(start_time.tv_sec)
        .ok()?
        .checked_mul(1_000_000)?
        .checked_add(u64::try_from(start_time.tv_usec).ok()?)?;
    Some((pid, i64::try_from(start_time_micros).ok()?))
}

pub(super) fn try_collect_process_lineage() -> Option<Vec<ProcessLineage>> {
    let processes = list_kinfo_processes().ok()?;
    let mut entries = Vec::with_capacity(processes.len());
    for info in processes {
        let pid = u32::try_from(info.kp_proc.p_pid).ok()?;
        if pid == 0 {
            continue;
        }
        match process_lineage_with_info(info) {
            Ok(Some(entry)) => entries.push(entry),
            Ok(None) | Err(ProbeError::Absent) => {}
            Err(ProbeError::Unavailable) => return None,
        }
    }
    entries
        .iter()
        .any(|entry| entry.pid == std::process::id())
        .then_some(entries)
}

pub(super) fn process_comm_and_parent(pid: u32) -> Option<(String, u32)> {
    let info = kinfo_for_pid(pid).ok()??;
    let name = c_array_string(&info.kp_proc.p_comm);
    (!name.is_empty()).then_some((name, info.kp_eproc.e_ppid as u32))
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
        match process_cwd(pid) {
            Ok(Some(cwd)) => {
                cwds.insert(pid, cwd);
            }
            Ok(None) | Err(ProbeError::Absent) => {}
            Err(ProbeError::Unavailable) => return None,
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
        match process_open_files(pid) {
            Ok(Some(paths)) => {
                files.insert(pid, paths);
            }
            Ok(None) | Err(ProbeError::Absent) => {}
            Err(ProbeError::Unavailable) => return None,
        }
    }
    Some(files)
}

fn process_fact(pid: u32, parse_all_start_times: bool) -> Result<Option<ProcessFact>, ProbeError> {
    let Some(info) = kinfo_for_pid(pid)? else {
        return Ok(None);
    };
    process_fact_with_info(info, parse_all_start_times)
}

fn process_fact_with_info(
    initial: KinfoProc,
    parse_all_start_times: bool,
) -> Result<Option<ProcessFact>, ProbeError> {
    let pid = u32::try_from(initial.kp_proc.p_pid).map_err(|_| ProbeError::Unavailable)?;
    let (info, command) = process_command_with_identity(pid, Some(initial))?;
    process_fact_from_command(info, command, parse_all_start_times)
}

fn process_fact_with_info_cached(
    initial: KinfoProc,
    parse_all_start_times: bool,
    cache: &mut ProcessCommandCache,
) -> Result<Option<ProcessFact>, ProbeError> {
    let pid = u32::try_from(initial.kp_proc.p_pid).map_err(|_| ProbeError::Unavailable)?;
    let is_zombie = initial.kp_proc.p_stat as u8 as u32 == libc::SZOMB;
    let mut info = initial;
    let command = match ProcessCommandIdentity::from_info(&initial) {
        Some(identity) => cache.command_for(identity, is_zombie, || {
            let (resolved_info, command) = process_command_with_identity(pid, Some(initial))?;
            info = resolved_info;
            Ok(command)
        })?,
        None => {
            let (resolved_info, command) = process_command_with_identity(pid, Some(initial))?;
            info = resolved_info;
            command
        }
    };
    process_fact_from_command(info, command, parse_all_start_times)
}

fn process_fact_from_command(
    info: KinfoProc,
    command: String,
    parse_all_start_times: bool,
) -> Result<Option<ProcessFact>, ProbeError> {
    let pid = u32::try_from(info.kp_proc.p_pid).map_err(|_| ProbeError::Unavailable)?;
    if command.is_empty() {
        return Err(ProbeError::Unavailable);
    }
    let seconds =
        u64::try_from(info.kp_proc.p_starttime.tv_sec).map_err(|_| ProbeError::Unavailable)?;
    let lstart = format_lstart(seconds).ok_or(ProbeError::Unavailable)?;
    let start_time = if parse_all_start_times || super::command_needs_start_time(&command) {
        Some(start_time(&info).ok_or(ProbeError::Unavailable)?)
    } else {
        None
    };
    let tty = tty_name(info.kp_eproc.e_tdev);
    let mut stat = state_letter(info.kp_proc.p_stat as u8 as u32).to_string();
    if tty != "??" && info.kp_eproc.e_tpgid == info.kp_eproc.e_pgid {
        stat.push('+');
    }
    Ok(Some(ProcessFact {
        pid,
        tty,
        stat,
        lstart,
        command,
        start_time,
    }))
}

fn process_lineage_with_info(info: KinfoProc) -> Result<Option<ProcessLineage>, ProbeError> {
    let pid = u32::try_from(info.kp_proc.p_pid).map_err(|_| ProbeError::Unavailable)?;
    let (info, command) = process_command_with_identity(pid, Some(info))?;
    if command.is_empty() {
        return Err(ProbeError::Unavailable);
    }
    let mut stat = state_letter(info.kp_proc.p_stat as u8 as u32).to_string();
    if has_controlling_tty(info.kp_eproc.e_tdev) && info.kp_eproc.e_tpgid == info.kp_eproc.e_pgid {
        stat.push('+');
    }
    Ok(Some(ProcessLineage {
        pid,
        ppid: info.kp_eproc.e_ppid as u32,
        pgid: info.kp_eproc.e_pgid,
        stat,
        command,
    }))
}

fn process_command_with_identity(
    pid: u32,
    initial: Option<KinfoProc>,
) -> Result<(KinfoProc, String), ProbeError> {
    let mut before = match initial {
        Some(info) => info,
        None => kinfo_for_pid(pid)?.ok_or(ProbeError::Absent)?,
    };
    // A process mid-exec briefly exposes a torn argv area (observed on
    // short-lived `git` children), and a PID can be reused between reads.
    // Both clear within microseconds, so retry a few times before reporting
    // the process as unreadable.
    const ATTEMPTS: usize = 4;
    let euid = unsafe { libc::geteuid() };
    for _ in 0..ATTEMPTS {
        let is_zombie = before.kp_proc.p_stat as u8 as u32 == libc::SZOMB;
        // The kernel only shows argv to the same effective uid (or root);
        // skip the doomed KERN_PROCARGS2 call and print `(comm)` like ps
        // does for an unreadable process.
        if !is_zombie && euid != 0 && before.kp_eproc.e_ucred.uid != euid {
            return Ok((
                before,
                format!("({})", c_array_string(&before.kp_proc.p_comm)),
            ));
        }
        let read = read_process_command(pid, &before.kp_proc.p_comm, is_zombie);
        let (command, args_read) = match read {
            Ok(read) => read,
            Err(ProbeError::Absent) => return Err(ProbeError::Absent),
            Err(ProbeError::Unavailable) => {
                before = kinfo_for_pid(pid)?.ok_or(ProbeError::Absent)?;
                continue;
            }
        };
        if !args_read {
            return Ok((before, command));
        }
        let after = kinfo_for_pid(pid)?.ok_or(ProbeError::Absent)?;
        if same_process(&before, &after) {
            return Ok((after, command));
        }
        before = after;
    }
    Err(ProbeError::Unavailable)
}

fn process_cwd(pid: u32) -> Result<Option<String>, ProbeError> {
    let mut info = MaybeUninit::<libc::proc_vnodepathinfo>::zeroed();
    let expected = std::mem::size_of::<libc::proc_vnodepathinfo>();
    clear_errno();
    let got = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDVNODEPATHINFO,
            0,
            info.as_mut_ptr() as *mut libc::c_void,
            expected as libc::c_int,
        )
    };
    if got == 0 {
        return Err(error_from_errno());
    }
    if got < 0 || got as usize != expected {
        return Err(ProbeError::Unavailable);
    }
    let info = unsafe { info.assume_init() };
    let cwd = vnode_path_string(&info.pvi_cdir).ok_or(ProbeError::Unavailable)?;
    if cwd.is_empty() {
        Ok(None)
    } else {
        Ok(Some(cwd))
    }
}

fn process_open_files(pid: u32) -> Result<Option<Vec<PathBuf>>, ProbeError> {
    for attempt in 0..2 {
        let Some(before) = kinfo_for_pid(pid)? else {
            return Ok(None);
        };
        let fds = match list_fd_info(pid, INITIAL_FD_CAPACITY) {
            Ok(fds) => fds,
            Err(ProbeError::Absent) => return Ok(None),
            Err(error) => return Err(error),
        };
        let mut paths = Vec::new();
        for fd in fds {
            if fd.proc_fdtype != libc::PROX_FDTYPE_VNODE as u32 {
                continue;
            }
            let mut info = MaybeUninit::<VnodeFdInfoWithPath>::zeroed();
            let expected = std::mem::size_of::<VnodeFdInfoWithPath>();
            clear_errno();
            let got = unsafe {
                libc::proc_pidfdinfo(
                    pid as libc::c_int,
                    fd.proc_fd,
                    PROC_PIDFDVNODEPATHINFO,
                    info.as_mut_ptr() as *mut libc::c_void,
                    expected as libc::c_int,
                )
            };
            if got <= 0 && matches!(unsafe { *libc::__error() }, libc::ENOENT | libc::EBADF) {
                // EBADF: closed since PROC_PIDLISTFDS. ENOENT: the vnode was
                // revoked (a Codex left on a closed terminal holds its tty this
                // way). Neither names a file, so neither can be a transcript;
                // failing here would hide every other process's files.
                continue;
            }
            if got < 0 || got as usize != expected {
                return Err(ProbeError::Unavailable);
            }
            let info = unsafe { info.assume_init() };
            if info.pfi.fi_type != libc::PROX_FDTYPE_VNODE {
                return Err(ProbeError::Unavailable);
            }
            let path = vnode_path_string(&info.pvip).ok_or(ProbeError::Unavailable)?;
            if path.starts_with('/') {
                paths.push(PathBuf::from(path));
            }
        }
        let Some(after) = kinfo_for_pid(pid)? else {
            return Ok(None);
        };
        if same_process(&before, &after) {
            return Ok(Some(paths));
        }
        if attempt == 1 {
            return Err(ProbeError::Unavailable);
        }
    }
    Err(ProbeError::Unavailable)
}

fn list_fd_info(pid: u32, file_count_hint: usize) -> Result<Vec<libc::proc_fdinfo>, ProbeError> {
    let item_size = std::mem::size_of::<libc::proc_fdinfo>();
    let mut capacity = file_count_hint.saturating_add(8).max(INITIAL_FD_CAPACITY);
    for _ in 0..MAX_ENUMERATION_RETRIES {
        let Some(bytes) = capacity.checked_mul(item_size) else {
            return Err(ProbeError::Unavailable);
        };
        let bytes_c = libc::c_int::try_from(bytes).map_err(|_| ProbeError::Unavailable)?;
        let mut storage = Vec::<MaybeUninit<libc::proc_fdinfo>>::new();
        storage
            .try_reserve_exact(capacity)
            .map_err(|_| ProbeError::Unavailable)?;
        unsafe { storage.set_len(capacity) };
        let got = unsafe {
            libc::proc_pidinfo(
                pid as libc::c_int,
                libc::PROC_PIDLISTFDS,
                0,
                storage.as_mut_ptr() as *mut libc::c_void,
                bytes_c,
            )
        };
        if got == 0 {
            return Err(error_from_errno());
        }
        if got < 0 {
            return Err(error_from_errno());
        }
        let got = got as usize;
        if got > bytes {
            capacity = got.div_ceil(item_size).max(capacity + 1);
            continue;
        }
        if got == bytes {
            capacity = capacity.checked_mul(2).ok_or(ProbeError::Unavailable)?;
            continue;
        }
        if got % item_size != 0 {
            return Err(ProbeError::Unavailable);
        }
        let count = got / item_size;
        return Ok(storage
            .into_iter()
            .take(count)
            .map(|entry| unsafe { entry.assume_init() })
            .collect());
    }
    Err(ProbeError::Unavailable)
}

fn read_process_command(
    pid: u32,
    comm: &[libc::c_char],
    is_zombie: bool,
) -> Result<(String, bool), ProbeError> {
    if is_zombie {
        return Ok(("<defunct>".to_string(), false));
    }
    let fallback_comm = || format!("({})", c_array_string(comm));
    let fallback = || Ok((fallback_comm(), false));

    // Same shape as ps(1): one KERN_PROCARGS2 read into a KERN_ARGMAX-sized
    // buffer. A NULL-buffer size query first is a second syscall that the
    // kernel answers with EINVAL for some live processes, which would turn a
    // readable argv into the `(comm)` fallback.
    let Some(size) = kern_argmax() else {
        return Err(ProbeError::Unavailable);
    };
    // One KERN_ARGMAX (1 MiB) reservation per thread, reused and never
    // zeroed: a fresh 1 MiB allocation per process is an mmap, page faults
    // and an munmap each time, and zeroing touched every page. Only the pages
    // the kernel copies argv into are ever made resident.
    thread_local! {
        static PROCARGS: std::cell::RefCell<Vec<u8>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    PROCARGS.with_borrow_mut(|buffer| {
        buffer.clear();
        buffer.reserve(size);
        let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid as libc::c_int];
        let mut returned = size;
        clear_errno();
        let rc = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                mib.len() as libc::c_uint,
                buffer.as_mut_ptr() as *mut libc::c_void,
                &mut returned,
                ptr::null_mut(),
                0,
            )
        };
        if rc != 0 {
            let error = unsafe { *libc::__error() };
            return if error == libc::ESRCH {
                Err(ProbeError::Absent)
            } else if matches!(error, libc::EPERM | libc::EACCES | libc::EINVAL) {
                fallback()
            } else {
                Err(ProbeError::Unavailable)
            };
        }
        if returned > size || returned < std::mem::size_of::<libc::c_int>() {
            return Err(ProbeError::Unavailable);
        }
        // SAFETY: capacity is at least `size` and the kernel initialized the
        // first `returned` bytes.
        unsafe { buffer.set_len(returned) };
        parse_procargs(buffer, &fallback_comm)
    })
}

/// Render a KERN_PROCARGS2 area (argc, exec path, argv, env) as ps prints
/// `command`.
fn parse_procargs(
    buffer: &[u8],
    fallback_comm: &dyn Fn() -> String,
) -> Result<(String, bool), ProbeError> {
    let argc = libc::c_int::from_ne_bytes(buffer[..4].try_into().unwrap());
    if argc < 0 || argc as usize > buffer.len() {
        return Err(ProbeError::Unavailable);
    }
    if argc == 0 {
        return Ok((fallback_comm(), true));
    }

    let mut cursor = std::mem::size_of::<libc::c_int>();
    let executable_end = buffer[cursor..]
        .iter()
        .position(|byte| *byte == 0)
        .map(|length| cursor + length)
        .ok_or(ProbeError::Unavailable)?;
    cursor = executable_end + 1;
    while cursor < buffer.len() && buffer[cursor] == 0 {
        cursor += 1;
    }

    let mut command = String::new();
    for arg_index in 0..argc {
        if cursor >= buffer.len() {
            return Err(ProbeError::Unavailable);
        }
        let end = buffer[cursor..]
            .iter()
            .position(|byte| *byte == 0)
            .map(|length| cursor + length)
            .ok_or(ProbeError::Unavailable)?;
        if arg_index > 0 {
            command.push(' ');
        }
        push_ps_visible(&mut command, &String::from_utf8_lossy(&buffer[cursor..end]));
        cursor = end + 1;
    }
    // ps prints argv with trailing blanks dropped (setproctitle pads with
    // spaces), and the line-oriented parser it fed trimmed them too.
    let trimmed = command.trim_end().len();
    command.truncate(trimmed);
    if command.is_empty() {
        Ok((fallback_comm(), true))
    } else {
        Ok((command, true))
    }
}

/// Append `arg` the way ps(1) prints it: control characters as `\ooo`
/// octal escapes, so one process is always one line of `command`.
fn push_ps_visible(command: &mut String, arg: &str) {
    for ch in arg.chars() {
        if (ch as u32) < 0x20 || ch == '\u{7f}' {
            command.push_str(&format!("\\{:03o}", ch as u32));
        } else {
            command.push(ch);
        }
    }
}

/// `kern.argmax`, the largest argv+env area KERN_PROCARGS2 can return.
static KERN_ARGMAX: std::sync::LazyLock<Option<usize>> = std::sync::LazyLock::new(|| {
    let mut mib = [libc::CTL_KERN, libc::KERN_ARGMAX];
    let mut value: libc::c_int = 0;
    let mut size = std::mem::size_of::<libc::c_int>();
    let rc = unsafe {
        libc::sysctl(
            mib.as_mut_ptr(),
            mib.len() as libc::c_uint,
            (&mut value as *mut libc::c_int).cast(),
            &mut size,
            ptr::null_mut(),
            0,
        )
    };
    (rc == 0 && value > 0).then_some(value as usize)
});

fn kern_argmax() -> Option<usize> {
    *KERN_ARGMAX
}

fn list_kinfo_processes() -> Result<Vec<KinfoProc>, ProbeError> {
    let mut mib = [libc::CTL_KERN, libc::KERN_PROC, libc::KERN_PROC_ALL];
    let mut size = 0usize;
    clear_errno();
    let rc = unsafe {
        libc::sysctl(
            mib.as_mut_ptr(),
            mib.len() as libc::c_uint,
            ptr::null_mut(),
            &mut size,
            ptr::null_mut(),
            0,
        )
    };
    if rc != 0 {
        return Err(error_from_errno());
    }
    if size == 0 {
        return Ok(Vec::new());
    }

    let item_size = std::mem::size_of::<KinfoProc>();
    let mut capacity = size.div_ceil(item_size).max(1);
    for _ in 0..MAX_ENUMERATION_RETRIES {
        let bytes = capacity
            .checked_mul(item_size)
            .ok_or(ProbeError::Unavailable)?;
        let mut storage = Vec::<MaybeUninit<KinfoProc>>::new();
        storage
            .try_reserve_exact(capacity)
            .map_err(|_| ProbeError::Unavailable)?;
        unsafe { storage.set_len(capacity) };
        let mut returned = bytes;
        clear_errno();
        let rc = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                mib.len() as libc::c_uint,
                storage.as_mut_ptr() as *mut libc::c_void,
                &mut returned,
                ptr::null_mut(),
                0,
            )
        };
        if rc != 0 {
            let error = unsafe { *libc::__error() };
            if error == libc::ENOMEM {
                capacity = returned.max(bytes.saturating_mul(2)).div_ceil(item_size);
                continue;
            }
            return Err(error_from_errno());
        }
        if returned > bytes {
            capacity = returned.max(bytes.saturating_mul(2)).div_ceil(item_size);
            continue;
        }
        if returned % item_size != 0 {
            return Err(ProbeError::Unavailable);
        }
        let count = returned / item_size;
        return Ok(storage
            .into_iter()
            .take(count)
            .map(|entry| unsafe { entry.assume_init() })
            .collect());
    }
    Err(ProbeError::Unavailable)
}

fn kinfo_for_pid(pid: u32) -> Result<Option<KinfoProc>, ProbeError> {
    let pid_c = libc::c_int::try_from(pid).map_err(|_| ProbeError::Absent)?;
    if pid_c <= 0 {
        return Err(ProbeError::Absent);
    }
    let mut mib = [libc::CTL_KERN, libc::KERN_PROC, libc::KERN_PROC_PID, pid_c];
    let mut size = 0usize;
    clear_errno();
    let rc = unsafe {
        libc::sysctl(
            mib.as_mut_ptr(),
            mib.len() as libc::c_uint,
            ptr::null_mut(),
            &mut size,
            ptr::null_mut(),
            0,
        )
    };
    if rc != 0 {
        return Err(error_from_errno());
    }
    let item_size = std::mem::size_of::<KinfoProc>();
    if size % item_size != 0 {
        return Err(ProbeError::Unavailable);
    }
    let count = size / item_size;
    let mut storage = Vec::<MaybeUninit<KinfoProc>>::new();
    storage
        .try_reserve_exact(count)
        .map_err(|_| ProbeError::Unavailable)?;
    unsafe { storage.set_len(count) };
    let mut returned = size;
    clear_errno();
    let rc = unsafe {
        libc::sysctl(
            mib.as_mut_ptr(),
            mib.len() as libc::c_uint,
            storage.as_mut_ptr() as *mut libc::c_void,
            &mut returned,
            ptr::null_mut(),
            0,
        )
    };
    if rc != 0 {
        return Err(error_from_errno());
    }
    if returned > size || returned % item_size != 0 {
        return Err(ProbeError::Unavailable);
    }
    let returned_count = returned / item_size;
    for entry in storage.into_iter().take(returned_count) {
        let info = unsafe { entry.assume_init() };
        if info.kp_proc.p_pid == pid_c {
            return Ok(Some(info));
        }
    }
    Ok(None)
}

fn same_process(left: &KinfoProc, right: &KinfoProc) -> bool {
    left.kp_proc.p_pid == right.kp_proc.p_pid
        && left.kp_proc.p_starttime.tv_sec == right.kp_proc.p_starttime.tv_sec
        && left.kp_proc.p_starttime.tv_usec == right.kp_proc.p_starttime.tv_usec
}

/// Whole seconds, exactly what parsing `ps -o lstart=` produced: the value is
/// emitted as `process_start_time` evidence and compared with earlier
/// observations, so sub-second precision would read as a different process.
fn start_time(info: &KinfoProc) -> Option<DateTime<Utc>> {
    Utc.timestamp_opt(info.kp_proc.p_starttime.tv_sec, 0)
        .single()
}

fn error_from_errno() -> ProbeError {
    if unsafe { *libc::__error() } == libc::ESRCH {
        ProbeError::Absent
    } else {
        ProbeError::Unavailable
    }
}

fn clear_errno() {
    unsafe { *libc::__error() = 0 }
}

fn c_array_string(bytes: &[libc::c_char]) -> String {
    let length = bytes
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(bytes.len());
    let bytes = unsafe { std::slice::from_raw_parts(bytes.as_ptr() as *const u8, length) };
    String::from_utf8_lossy(bytes).into_owned()
}

fn vnode_path_string(path: &libc::vnode_info_path) -> Option<String> {
    let bytes = unsafe {
        std::slice::from_raw_parts(
            path.vip_path.as_ptr() as *const libc::c_char,
            std::mem::size_of_val(&path.vip_path) / std::mem::size_of::<libc::c_char>(),
        )
    };
    let length = bytes.iter().position(|byte| *byte == 0)?;
    Some(c_array_string(&bytes[..length]))
}

fn state_letter(status: u32) -> char {
    match status {
        libc::SIDL => 'I',
        libc::SRUN => 'R',
        libc::SSLEEP => 'S',
        libc::SSTOP => 'T',
        libc::SZOMB => 'Z',
        _ => 'U',
    }
}

fn has_controlling_tty(device: u32) -> bool {
    device != 0 && device != u32::MAX
}

/// Resolve a controlling TTY `dev_t` to its `/dev` name with thread-safe
/// `devname_r`, memoized for 60 seconds.
///
/// Uncached `devname()` scans `/dev` with `readdir` + `lstat` on every call
/// (and uses a non-thread-safe static buffer). PTY minor numbers map 1:1 to
/// `ttysNNN`, so memoizing by `dev_t` removes the dominant kernel leaf under
/// macOS process inventory scans.
fn tty_name(device: u32) -> String {
    if !has_controlling_tty(device) {
        return "??".to_string();
    }
    static MEMO: std::sync::Mutex<Option<(std::time::Instant, HashMap<u32, String>)>> =
        std::sync::Mutex::new(None);
    if let Ok(mut memo) = MEMO.lock() {
        let (since, names) =
            memo.get_or_insert_with(|| (std::time::Instant::now(), HashMap::new()));
        if since.elapsed() >= std::time::Duration::from_secs(60) || names.len() >= 512 {
            *since = std::time::Instant::now();
            names.clear();
        }
        if let Some(name) = names.get(&device) {
            return name.clone();
        }
        let name = tty_name_uncached(device);
        names.insert(device, name.clone());
        return name;
    }
    tty_name_uncached(device)
}

fn tty_name_uncached(device: u32) -> String {
    extern "C" {
        fn devname_r(
            dev: libc::dev_t,
            mode: libc::mode_t,
            buf: *mut libc::c_char,
            len: libc::c_int,
        ) -> *mut libc::c_char;
    }
    let mut buf = [0 as libc::c_char; 64];
    let name = unsafe {
        devname_r(
            device as libc::dev_t,
            libc::S_IFCHR as libc::mode_t,
            buf.as_mut_ptr(),
            buf.len() as libc::c_int,
        )
    };
    if name.is_null() {
        "??".to_string()
    } else {
        unsafe { CStr::from_ptr(name) }
            .to_string_lossy()
            .into_owned()
    }
}

/// `ps -o lstart=` text for a start time, memoized for one second.
///
/// `localtime_r` re-reads the time zone (an lstat of /etc/localtime) on
/// every call, and the 100 ms Console and status-owner polls format the same
/// few start times over and over: it was the largest leaf under process
/// facts. The memo is dropped every second, so a time-zone change (a
/// travelling laptop) reaches every caller within a second, as with `ps`.
fn format_lstart(seconds: u64) -> Option<String> {
    static MEMO: std::sync::Mutex<Option<(std::time::Instant, HashMap<u64, String>)>> =
        std::sync::Mutex::new(None);
    if let Ok(mut memo) = MEMO.lock() {
        let (since, formatted) =
            memo.get_or_insert_with(|| (std::time::Instant::now(), HashMap::new()));
        if since.elapsed() >= std::time::Duration::from_secs(1) || formatted.len() >= 8192 {
            *since = std::time::Instant::now();
            formatted.clear();
        }
        if let Some(text) = formatted.get(&seconds) {
            return Some(text.clone());
        }
        let text = format_lstart_uncached(seconds)?;
        formatted.insert(seconds, text.clone());
        return Some(text);
    }
    format_lstart_uncached(seconds)
}

fn format_lstart_uncached(seconds: u64) -> Option<String> {
    let seconds = libc::time_t::try_from(seconds).ok()?;
    let mut tm = MaybeUninit::<libc::tm>::zeroed();
    if unsafe { libc::localtime_r(&seconds, tm.as_mut_ptr()) }.is_null() {
        return None;
    }
    let mut output = [0u8; 64];
    let format = b"%a %b %e %H:%M:%S %Y\0";
    let written = unsafe {
        libc::strftime(
            output.as_mut_ptr() as *mut libc::c_char,
            output.len(),
            format.as_ptr() as *const libc::c_char,
            tm.as_ptr(),
        )
    };
    (written > 0).then(|| String::from_utf8_lossy(&output[..written]).into_owned())
}
