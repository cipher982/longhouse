//! Host disk guard: warn before a full disk takes the machine down.
//!
//! macOS does not fail politely when the data volume fills. On 2026-09-25
//! parallel agent builds took the last 19 GiB of a laptop in about two minutes;
//! instead of writers getting ENOSPC, launchd, WindowServer and the terminal
//! wedged and the host needed a hard power-off. Nothing was watching the slope,
//! only absolute thresholds that had been "red" for days.
//!
//! The guard keys on time-to-full as well as free space. It observes and
//! notifies; it never signals, pauses or deletes anything:
//!
//! - `low`: the Helm sessions that are measurably writing are told, by
//!   steering text into their live turn. Idle sessions are never messaged;
//!   they are not the problem and a message would start a turn.
//! - `critical`: the same, plus a desktop notification.
//!
//! Pausing writers was tried and removed: a stopped process keeps its locks
//! (git index, cargo target dir), tool timeouts turn a pause into a kill that
//! agents answer by rebuilding, and the writers that cannot be reached from an
//! agent's process tree (swap, builds inside a Docker VM) are left untouched.
//!
//! Cost: one `statvfs` per tick. The process table is read from libproc
//! counters only while the disk is under pressure; nothing walks the
//! filesystem. Short-lived compilers can finish between ticks, so attribution
//! under-counts builds; it only decides who is told.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub(crate) const TICK: Duration = Duration::from_secs(15);

const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
const MIB: u64 = 1024 * 1024;

/// Entry conditions, most severe first: below `free_gib` free, or fewer than
/// `eta_minutes` to full at the current burn rate.
const LEVELS: [(Level, f64, f64); 2] = [(Level::Critical, 15.0, 10.0), (Level::Low, 40.0, 60.0)];
/// Slower than this, a projected time-to-full is noise (logs, caches, swap
/// breathing) rather than a runaway writer.
const MIN_PROJECTED_RATE_GIB_PER_MIN: f64 = 0.25;
/// Step down only when the lower level would still hold with this much less
/// free space, for `RECOVERY_TICKS` consecutive ticks, so a level does not
/// flap (and re-notify) around a threshold.
const RECOVERY_MARGIN_GIB: f64 = 5.0;
const RECOVERY_TICKS: u32 = 2;

const SAMPLE_RETENTION: Duration = Duration::from_secs(300);
/// The short window catches bursts; the long one catches sustained builds.
const SLOPE_WINDOWS: [Duration; 2] = [Duration::from_secs(60), Duration::from_secs(180)];
const MIN_SLOPE_SPAN: Duration = Duration::from_secs(25);
/// Read the process table at this burn rate even when free space looks fine,
/// so attribution has a baseline before the level escalates.
const SNAPSHOT_RATE_GIB_PER_MIN: f64 = 0.5;

const ATTRIBUTION_TICKS: usize = 8;
const STEER_MIN_BYTES: u64 = 256 * MIB;
const RESTEER_INTERVAL: Duration = Duration::from_secs(600);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Level {
    #[default]
    Ok,
    Low,
    Critical,
}

impl Level {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Low => "low",
            Level::Critical => "critical",
        }
    }
}

pub(crate) fn classify(free_gib: f64, burn_gib_per_min: f64) -> Level {
    let eta = eta_minutes(free_gib, burn_gib_per_min);
    for (level, free_below, eta_below) in LEVELS {
        if free_gib < free_below || eta.is_some_and(|eta| eta < eta_below) {
            return level;
        }
    }
    Level::Ok
}

fn eta_minutes(free_gib: f64, burn_gib_per_min: f64) -> Option<f64> {
    (burn_gib_per_min >= MIN_PROJECTED_RATE_GIB_PER_MIN)
        .then(|| free_gib.max(0.0) / burn_gib_per_min)
}

/// Escalates immediately, recovers only after sustained margin.
#[derive(Debug, Default)]
pub(crate) struct Policy {
    level: Level,
    calm_ticks: u32,
}

impl Policy {
    pub(crate) fn update(&mut self, free_gib: f64, burn_gib_per_min: f64) -> Level {
        let raw = classify(free_gib, burn_gib_per_min);
        if raw >= self.level {
            self.level = raw;
            self.calm_ticks = 0;
            return self.level;
        }
        let calm = classify(free_gib - RECOVERY_MARGIN_GIB, burn_gib_per_min);
        if calm < self.level {
            self.calm_ticks += 1;
            if self.calm_ticks >= RECOVERY_TICKS {
                self.level = calm;
                self.calm_ticks = 0;
            }
        } else {
            self.calm_ticks = 0;
        }
        self.level
    }
}

#[derive(Clone, Copy, Debug)]
struct Sample {
    at: Instant,
    free: u64,
}

/// GiB per minute the volume is losing; the steeper of the short and long
/// windows. Space coming back is reported as zero.
fn burn_rate(samples: &VecDeque<Sample>) -> f64 {
    let Some(latest) = samples.back() else {
        return 0.0;
    };
    let mut rate: f64 = 0.0;
    for window in SLOPE_WINDOWS {
        let Some(oldest) = samples
            .iter()
            .find(|sample| latest.at.duration_since(sample.at) <= window)
        else {
            continue;
        };
        let span = latest.at.duration_since(oldest.at);
        if span < MIN_SLOPE_SPAN {
            continue;
        }
        let lost = oldest.free as f64 - latest.free as f64;
        rate = rate.max(lost / GIB / (span.as_secs_f64() / 60.0));
    }
    rate
}

/// A live Helm session and the pids of the agent itself. Everything running
/// under those pids is work the session started.
#[derive(Clone, Debug)]
pub(crate) struct SessionRoot {
    pub session_id: String,
    pub provider: String,
    pub pids: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ProcInfo {
    pub pid: u32,
    pub ppid: u32,
    /// Process start time (mach absolute); keys counters so a recycled pid
    /// does not inherit another process's bytes.
    pub start: u64,
    pub written: u64,
    pub name: String,
}

type ProcKey = (u32, u64);

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct SessionActivity {
    pub session_id: String,
    pub provider: String,
    /// Bytes written over the attribution window by the session's commands.
    pub bytes: u64,
    /// Name of the top-level command (direct child of the agent) that wrote most.
    pub top_command: String,
}

/// Charge each process's recent writes to the session whose agent it runs
/// under. The agent process itself (transcripts, caches) is not charged.
pub(crate) fn attribute(
    procs: &[ProcInfo],
    window_bytes: &HashMap<ProcKey, u64>,
    sessions: &[SessionRoot],
) -> Vec<SessionActivity> {
    let parent: HashMap<u32, u32> = procs.iter().map(|p| (p.pid, p.ppid)).collect();
    let names: HashMap<u32, &str> = procs.iter().map(|p| (p.pid, p.name.as_str())).collect();
    let root_owner: HashMap<u32, usize> = sessions
        .iter()
        .enumerate()
        .flat_map(|(index, session)| session.pids.iter().map(move |pid| (*pid, index)))
        .collect();

    // (session index, top-level command pid) -> bytes
    let mut per_command: HashMap<(usize, u32), u64> = HashMap::new();
    for proc in procs {
        let bytes = window_bytes
            .get(&(proc.pid, proc.start))
            .copied()
            .unwrap_or(0);
        if bytes == 0 || root_owner.contains_key(&proc.pid) {
            continue;
        }
        // Climb to the nearest agent process; the step below it is the command.
        let (mut child, mut cursor) = (proc.pid, proc.ppid);
        for _ in 0..64 {
            if let Some(index) = root_owner.get(&cursor) {
                *per_command.entry((*index, child)).or_default() += bytes;
                break;
            }
            match parent.get(&cursor) {
                Some(next) if *next != cursor && cursor > 1 => {
                    child = cursor;
                    cursor = *next;
                }
                _ => break,
            }
        }
    }

    let mut activity: Vec<SessionActivity> = sessions
        .iter()
        .map(|session| SessionActivity {
            session_id: session.session_id.clone(),
            provider: session.provider.clone(),
            ..Default::default()
        })
        .collect();
    let mut top_bytes = vec![0u64; sessions.len()];
    for ((index, command), bytes) in per_command {
        activity[index].bytes += bytes;
        if bytes > top_bytes[index] {
            top_bytes[index] = bytes;
            activity[index].top_command =
                names.get(&command).copied().unwrap_or_default().to_string();
        }
    }
    activity
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct WriterReport {
    pub session_id: String,
    pub provider: String,
    pub bytes_written_recently: u64,
    pub top_command: String,
}

/// What `longhouse-engine disk-guard status` reads.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct GuardState {
    pub level: Level,
    pub free_bytes: u64,
    pub burn_gib_per_min: f64,
    pub eta_minutes: Option<f64>,
    pub updated_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub writers: Vec<WriterReport>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Steer {
    pub session_id: String,
    pub provider: String,
    pub text: String,
}

#[derive(Debug, Default)]
pub(crate) struct TickOutcome {
    pub steers: Vec<Steer>,
    /// Desktop notification (title, body).
    pub notify: Option<(String, String)>,
}

pub(crate) fn state_path() -> Option<PathBuf> {
    crate::config::get_longhouse_home()
        .ok()
        .map(|home| home.join("disk-guard.json"))
}

pub(crate) fn read_state(path: &Path) -> Option<GuardState> {
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

fn write_state(path: &Path, state: &GuardState) {
    let Ok(bytes) = serde_json::to_vec_pretty(state) else {
        return;
    };
    let tmp = path.with_extension("json.tmp");
    if std::fs::write(&tmp, bytes).is_ok() {
        let _ = std::fs::rename(&tmp, path);
    }
}

type FreeSource = Box<dyn FnMut(&Path) -> Option<u64> + Send>;

pub(crate) struct DiskGuard {
    volume: PathBuf,
    free_source: FreeSource,
    state_path: Option<PathBuf>,
    samples: VecDeque<Sample>,
    policy: Policy,
    level: Level,
    previous_counters: Option<HashMap<ProcKey, u64>>,
    window: VecDeque<HashMap<ProcKey, u64>>,
    last_steer: HashMap<String, (Level, Instant)>,
}

impl DiskGuard {
    /// Watch the volume holding `volume`.
    pub(crate) fn new(volume: PathBuf, state_path: Option<PathBuf>) -> Self {
        Self::with_free_source(volume, state_path, Box::new(free_bytes))
    }

    fn with_free_source(
        volume: PathBuf,
        state_path: Option<PathBuf>,
        free_source: FreeSource,
    ) -> Self {
        Self {
            volume,
            free_source,
            state_path,
            samples: VecDeque::new(),
            policy: Policy::default(),
            level: Level::Ok,
            previous_counters: None,
            window: VecDeque::new(),
            last_steer: HashMap::new(),
        }
    }

    pub(crate) fn tick(&mut self, sessions: &[SessionRoot]) -> TickOutcome {
        let now = Instant::now();
        let Some(free) = (self.free_source)(&self.volume) else {
            return TickOutcome::default();
        };
        self.samples.push_back(Sample { at: now, free });
        while self
            .samples
            .front()
            .is_some_and(|sample| now.duration_since(sample.at) > SAMPLE_RETENTION)
        {
            self.samples.pop_front();
        }
        let free_gib = free as f64 / GIB;
        let burn = burn_rate(&self.samples);
        let eta = eta_minutes(free_gib, burn);
        let previous_level = self.level;
        self.level = self.policy.update(free_gib, burn);
        let level = self.level;

        let activity = if level > Level::Ok || burn >= SNAPSHOT_RATE_GIB_PER_MIN {
            self.observe_processes(sessions)
        } else {
            self.previous_counters = None;
            self.window.clear();
            Vec::new()
        };

        let mut outcome = TickOutcome::default();
        if level > Level::Ok {
            outcome.steers = due_steers(level, &activity, &mut self.last_steer, now)
                .into_iter()
                .map(|session| Steer {
                    session_id: session.session_id.clone(),
                    provider: session.provider.clone(),
                    text: steer_text(level, free_gib, burn, eta, session),
                })
                .collect();
        } else {
            self.last_steer.clear();
        }

        if level == Level::Critical && previous_level < Level::Critical {
            outcome.notify = Some((
                "Longhouse: disk critical".to_string(),
                describe(free_gib, burn, eta) + ". Agents that are writing have been told.",
            ));
        } else if previous_level == Level::Critical && level < Level::Critical {
            outcome.notify = Some((
                "Longhouse: disk recovering".to_string(),
                format!("{free_gib:.0} GiB free."),
            ));
        }
        if level != previous_level {
            tracing::warn!(
                from = previous_level.as_str(),
                to = level.as_str(),
                free_gib = format!("{free_gib:.1}"),
                burn_gib_per_min = format!("{burn:.2}"),
                "Disk guard level changed"
            );
        }

        if let Some(path) = self.state_path.as_deref() {
            let mut writers: Vec<WriterReport> = activity
                .iter()
                .filter(|s| s.bytes > 0)
                .map(|s| WriterReport {
                    session_id: s.session_id.clone(),
                    provider: s.provider.clone(),
                    bytes_written_recently: s.bytes,
                    top_command: s.top_command.clone(),
                })
                .collect();
            writers.sort_by(|a, b| b.bytes_written_recently.cmp(&a.bytes_written_recently));
            write_state(
                path,
                &GuardState {
                    level,
                    free_bytes: free,
                    burn_gib_per_min: burn,
                    eta_minutes: eta,
                    updated_at: Some(Utc::now()),
                    writers,
                },
            );
        }
        outcome
    }

    fn observe_processes(&mut self, sessions: &[SessionRoot]) -> Vec<SessionActivity> {
        let procs = read_processes();
        let counters: HashMap<ProcKey, u64> = procs
            .iter()
            .map(|p| ((p.pid, p.start), p.written))
            .collect();
        if let Some(previous) = self.previous_counters.as_ref() {
            // A process born since the last read counts from zero.
            let deltas = counters
                .iter()
                .map(|(key, written)| {
                    (
                        *key,
                        written.saturating_sub(previous.get(key).copied().unwrap_or(0)),
                    )
                })
                .filter(|(_, delta)| *delta > 0)
                .collect();
            self.window.push_back(deltas);
            while self.window.len() > ATTRIBUTION_TICKS {
                self.window.pop_front();
            }
        }
        self.previous_counters = Some(counters);
        let mut window_bytes: HashMap<ProcKey, u64> = HashMap::new();
        for tick in &self.window {
            for (key, delta) in tick {
                *window_bytes.entry(*key).or_default() += delta;
            }
        }
        attribute(&procs, &window_bytes, sessions)
    }
}

/// Sessions to tell now: measurable writers not told at this level within
/// the re-send interval.
fn due_steers<'a>(
    level: Level,
    activity: &'a [SessionActivity],
    last_steer: &mut HashMap<String, (Level, Instant)>,
    now: Instant,
) -> Vec<&'a SessionActivity> {
    let mut due = Vec::new();
    for session in activity.iter().filter(|s| s.bytes >= STEER_MIN_BYTES) {
        let send = match last_steer.get(&session.session_id) {
            Some((sent_level, sent_at)) => {
                level > *sent_level || now.duration_since(*sent_at) >= RESTEER_INTERVAL
            }
            None => true,
        };
        if send {
            last_steer.insert(session.session_id.clone(), (level, now));
            due.push(session);
        }
    }
    due
}

fn describe(free_gib: f64, burn: f64, eta: Option<f64>) -> String {
    match eta {
        Some(eta) => format!(
            "{free_gib:.0} GiB free and falling {burn:.1} GiB/min (about {eta:.0} min to full)"
        ),
        None => format!("{free_gib:.0} GiB free"),
    }
}

fn steer_text(
    level: Level,
    free_gib: f64,
    burn: f64,
    eta: Option<f64>,
    session: &SessionActivity,
) -> String {
    let top = if session.top_command.is_empty() {
        String::new()
    } else {
        format!(" (mostly `{}`)", session.top_command)
    };
    format!(
        "[Longhouse disk guard: {level}] This machine's disk is at {state}. Your commands wrote {mib} MiB in the last two minutes{top}. \
Finish or stop the current heavy command, and do not start new builds, installs, worktrees, Docker images or simulator runs until space recovers. \
Do not delete files to make room; tell the user instead. `longhouse-engine disk-guard status` shows the current state.",
        level = level.as_str(),
        state = describe(free_gib, burn, eta),
        mib = session.bytes / MIB,
    )
}

pub(crate) fn free_bytes(path: &Path) -> Option<u64> {
    use std::ffi::CString;
    let path = CString::new(path.to_string_lossy().as_bytes()).ok()?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: statvfs fills the struct on success; it is read only then.
    unsafe {
        if libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) != 0 {
            return None;
        }
        let stat = stat.assume_init();
        // Field widths differ between macOS and Linux; the casts are no-ops on one.
        #[allow(clippy::unnecessary_cast)]
        Some(stat.f_bavail as u64 * stat.f_frsize as u64)
    }
}

/// This user's processes with parent, identity and lifetime bytes written.
#[cfg(target_os = "macos")]
pub(crate) fn read_processes() -> Vec<ProcInfo> {
    // SAFETY: a null buffer asks for the count; the second call fills at most
    // the buffer we allocated.
    let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    if count <= 0 {
        return Vec::new();
    }
    let mut pids = vec![0 as libc::pid_t; count as usize + 256];
    let filled = unsafe {
        libc::proc_listallpids(
            pids.as_mut_ptr() as *mut libc::c_void,
            (pids.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int,
        )
    };
    if filled <= 0 {
        return Vec::new();
    }
    pids.truncate(filled as usize);
    // SAFETY: getuid cannot fail.
    let uid = unsafe { libc::getuid() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    pids.into_iter()
        .filter(|pid| *pid > 1)
        .filter_map(|pid| {
            let mut bsd = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
            // SAFETY: PROC_PIDTBSDINFO fills a proc_bsdinfo of `size` bytes.
            let got = unsafe {
                libc::proc_pidinfo(
                    pid,
                    libc::PROC_PIDTBSDINFO,
                    0,
                    bsd.as_mut_ptr() as *mut libc::c_void,
                    size,
                )
            };
            if got != size {
                return None;
            }
            // SAFETY: filled above.
            let bsd = unsafe { bsd.assume_init() };
            if bsd.pbi_uid != uid {
                return None;
            }
            let mut info = std::mem::MaybeUninit::<libc::rusage_info_v4>::zeroed();
            // SAFETY: proc_pid_rusage writes a rusage_info_v4 for flavor V4.
            let rc = unsafe {
                libc::proc_pid_rusage(
                    pid,
                    libc::RUSAGE_INFO_V4,
                    info.as_mut_ptr() as *mut libc::rusage_info_t,
                )
            };
            if rc != 0 {
                return None;
            }
            // SAFETY: filled on success.
            let info = unsafe { info.assume_init() };
            let name = c_name(&bsd.pbi_name)
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| c_name(&bsd.pbi_comm).unwrap_or_default());
            Some(ProcInfo {
                pid: pid as u32,
                ppid: bsd.pbi_ppid,
                start: info.ri_proc_start_abstime,
                written: info.ri_diskio_byteswritten,
                name,
            })
        })
        .collect()
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn read_processes() -> Vec<ProcInfo> {
    Vec::new()
}

#[cfg(target_os = "macos")]
fn c_name(raw: &[libc::c_char]) -> Option<String> {
    let bytes: Vec<u8> = raw
        .iter()
        .take_while(|c| **c != 0)
        .map(|c| *c as u8)
        .collect();
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

/// Best-effort desktop notification; the guard never waits on it.
pub(crate) fn notify_desktop(title: &str, body: &str) {
    #[cfg(target_os = "macos")]
    {
        let escape = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"");
        let script = format!(
            "display notification \"{}\" with title \"{}\"",
            escape(body),
            escape(title)
        );
        let _ = std::process::Command::new("/usr/bin/osascript")
            .arg("-e")
            .arg(script)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
    }
    #[cfg(not(target_os = "macos"))]
    let _ = (title, body);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn samples(points: &[(u64, f64)]) -> VecDeque<Sample> {
        let base = Instant::now();
        points
            .iter()
            .map(|(secs, gib)| Sample {
                at: base + Duration::from_secs(*secs),
                free: (*gib * GIB) as u64,
            })
            .collect()
    }

    #[test]
    fn classify_uses_free_space_and_time_to_full() {
        assert_eq!(classify(100.0, 0.0), Level::Ok);
        assert_eq!(classify(35.0, 0.0), Level::Low);
        assert_eq!(classify(12.0, 0.0), Level::Critical);
        // 2026-09-25: 18.8 GiB free, losing ~9.5 GiB/min -> two minutes left.
        assert_eq!(classify(18.8, 9.5), Level::Critical);
        // 60 GiB free but a sustained 2 GiB/min build: 30 min to full.
        assert_eq!(classify(60.0, 2.0), Level::Low);
        // Slow drift is not projected.
        assert_eq!(classify(45.0, 0.1), Level::Ok);
    }

    #[test]
    fn policy_escalates_at_once_and_recovers_with_margin() {
        let mut policy = Policy::default();
        assert_eq!(policy.update(12.0, 0.0), Level::Critical);
        // Just over the line: inside the margin, no flapping.
        assert_eq!(policy.update(16.0, 0.0), Level::Critical);
        // Clear of the margin, but recovery needs consecutive calm ticks.
        assert_eq!(policy.update(21.0, 0.0), Level::Critical);
        assert_eq!(policy.update(21.0, 0.0), Level::Low);
        // A new burst escalates immediately.
        assert_eq!(policy.update(21.0, 5.0), Level::Critical);
    }

    #[test]
    fn burn_rate_takes_the_steeper_window() {
        // Flat for two minutes, then 9 GiB in the last minute.
        let s = samples(&[(0, 40.0), (60, 40.0), (120, 40.0), (150, 35.0), (180, 31.0)]);
        let rate = burn_rate(&s);
        assert!((rate - 9.0).abs() < 0.1, "rate {rate}");
        assert_eq!(burn_rate(&samples(&[(0, 20.0), (60, 30.0)])), 0.0);
        assert_eq!(burn_rate(&samples(&[(0, 20.0), (10, 10.0)])), 0.0);
    }

    fn proc(pid: u32, ppid: u32, name: &str) -> ProcInfo {
        ProcInfo {
            pid,
            ppid,
            start: pid as u64 * 10,
            written: 0,
            name: name.into(),
        }
    }

    #[test]
    fn attribute_charges_commands_to_the_session_they_run_under() {
        // launcher 100 -> omp 101 -> zsh 200 -> make 201 -> xcodebuild 202
        //                         -> node 300 (MCP server)
        // unrelated 900
        let procs = vec![
            proc(100, 1, "longhouse-engine"),
            proc(101, 100, "omp"),
            proc(200, 101, "zsh"),
            proc(201, 200, "make"),
            proc(202, 201, "xcodebuild"),
            proc(300, 101, "node"),
            proc(900, 1, "Finder"),
        ];
        let window = HashMap::from([
            ((101, 1010), 50 * MIB),
            ((202, 2020), 900 * MIB),
            ((300, 3000), 10 * MIB),
            ((900, 9000), 5000 * MIB),
        ]);
        let sessions = vec![SessionRoot {
            session_id: "s1".into(),
            provider: "omp".into(),
            pids: vec![100, 101],
        }];
        let activity = attribute(&procs, &window, &sessions);
        assert_eq!(
            activity[0].bytes,
            910 * MIB,
            "agent and unrelated writes are not charged"
        );
        assert_eq!(activity[0].top_command, "zsh");
    }

    #[test]
    fn only_writers_are_told_and_not_repeatedly() {
        let now = Instant::now();
        let activity = vec![
            SessionActivity {
                session_id: "busy".into(),
                provider: "omp".into(),
                bytes: 900 * MIB,
                top_command: "cargo".into(),
            },
            SessionActivity {
                session_id: "idle".into(),
                provider: "claude".into(),
                bytes: 0,
                top_command: String::new(),
            },
        ];
        let mut last = HashMap::new();
        let due: Vec<&str> = due_steers(Level::Low, &activity, &mut last, now)
            .iter()
            .map(|s| s.session_id.as_str())
            .collect();
        assert_eq!(due, vec!["busy"]);
        assert!(due_steers(
            Level::Low,
            &activity,
            &mut last,
            now + Duration::from_secs(60)
        )
        .is_empty());
        assert_eq!(
            due_steers(
                Level::Critical,
                &activity,
                &mut last,
                now + Duration::from_secs(60)
            )
            .len(),
            1,
            "escalation re-tells"
        );
        assert_eq!(
            due_steers(
                Level::Critical,
                &activity,
                &mut last,
                now + RESTEER_INTERVAL + Duration::from_secs(60)
            )
            .len(),
            1
        );
    }

    #[test]
    fn critical_notifies_once_and_recovery_notifies() {
        use std::sync::{Arc, Mutex};
        let free = Arc::new(Mutex::new(12.0 * GIB));
        let source = free.clone();
        let mut guard = DiskGuard::with_free_source(
            PathBuf::from("/"),
            None,
            Box::new(move |_| Some(*source.lock().unwrap() as u64)),
        );
        assert!(guard.tick(&[]).notify.is_some());
        assert!(guard.tick(&[]).notify.is_none(), "no repeat while critical");
        *free.lock().unwrap() = 50.0 * GIB;
        assert!(
            guard.tick(&[]).notify.is_none(),
            "recovery waits for calm ticks"
        );
        let recovered = guard.tick(&[]).notify.expect("recovery notified");
        assert!(recovered.0.contains("recovering"));
    }

    #[test]
    fn steer_text_never_asks_agents_to_delete() {
        let session = SessionActivity {
            session_id: "s".into(),
            provider: "omp".into(),
            bytes: 900 * MIB,
            top_command: "cargo".into(),
        };
        let text = steer_text(Level::Critical, 12.0, 3.0, Some(4.0), &session);
        assert!(text.contains("Do not delete files"));
        assert!(!text.to_lowercase().contains("delete scratch"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn reads_own_process_with_counters() {
        let me = std::process::id();
        let procs = read_processes();
        assert!(procs.iter().any(|p| p.pid == me), "own process listed");
    }
}
