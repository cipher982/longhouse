//! Host disk guard: slow agents down before a full disk takes the machine down.
//!
//! macOS does not fail politely when the data volume fills. On 2026-09-25
//! parallel agent builds took the last 19 GiB of a laptop in about two minutes;
//! instead of writers getting ENOSPC, launchd, WindowServer and the terminal
//! wedged and the host needed a hard power-off. Nothing on the machine was
//! watching the slope, only absolute thresholds that had been "red" for days.
//!
//! The guard keys on time-to-full as well as free space, and acts in steps:
//!
//! - `watch` / `limp`: tell the Helm sessions that are measurably writing, by
//!   steering text into their live turn. Idle sessions are never messaged;
//!   they are not the problem and a message would start a turn.
//! - `freeze`: SIGSTOP every command tree running under a Helm session (never
//!   the agent process itself, which launchers would resume anyway), so the
//!   host keeps enough room to stay usable. Trees are resumed with SIGCONT once
//!   free space has recovered past a margin.
//!
//! Cost: one `statvfs` per tick. The process table is read from libproc
//! counters only while the disk is under pressure; nothing walks the
//! filesystem. Short-lived compilers can finish between ticks, so byte
//! attribution under-counts builds: it decides who is messaged, never who is
//! spared at `freeze`.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub(crate) const TICK: Duration = Duration::from_secs(15);

const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
const MIB: u64 = 1024 * 1024;

/// Entry conditions, most severe first: below `free_gib` free, or fewer than
/// `eta_minutes` to full at the current burn rate.
const LEVELS: [(Level, f64, f64); 3] = [
    (Level::Freeze, 10.0, 4.0),
    (Level::Limp, 20.0, 15.0),
    (Level::Watch, 40.0, 60.0),
];
/// Slower than this, a projected time-to-full is noise (logs, caches, swap
/// breathing) rather than a runaway writer.
const MIN_PROJECTED_RATE_GIB_PER_MIN: f64 = 0.25;
/// Step down only when the lower level would still hold with this much less
/// free space, for `RECOVERY_TICKS` consecutive ticks. Without it a freeze
/// resumes the moment writers stop, and they refill the gap immediately.
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
    Watch,
    Limp,
    Freeze,
}

impl Level {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Watch => "watch",
            Level::Limp => "limp",
            Level::Freeze => "freeze",
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
/// windows. Negative (space returning) is reported as zero.
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

/// A live Helm session and the process ids that belong to the agent itself.
/// Everything below those pids is the session's command trees.
#[derive(Clone, Debug)]
pub(crate) struct SessionRoot {
    pub session_id: String,
    pub provider: String,
    /// Pids whose identity the provider scan verified (pid plus start time).
    pub pids: Vec<u32>,
    /// Recorded but unverified agent pids (e.g. Cursor's TUI). One counts as
    /// agent only while it descends from a verified pid of the same session,
    /// so a recycled pid can never become a root whose descendants get paused.
    pub dependent_pids: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ProcInfo {
    pub pid: u32,
    pub ppid: u32,
    /// Process start time (mach absolute). Paired with the pid so a recycled
    /// pid is never signalled.
    pub start: u64,
    pub written: u64,
    pub name: String,
    /// Already stopped (job control, a debugger, or a prior freeze).
    pub stopped: bool,
}

type ProcKey = (u32, u64);

/// One command tree: the direct child of an agent process and everything
/// under it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct CommandTree {
    pub top_pid: u32,
    pub top_name: String,
    pub members: Vec<ProcKey>,
    pub bytes: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct SessionActivity {
    pub session_id: String,
    pub provider: String,
    pub bytes: u64,
    pub trees: Vec<CommandTree>,
}

/// Group the process table into per-session command trees, with bytes
/// written over the attribution window.
pub(crate) fn attribute(
    procs: &[ProcInfo],
    window_bytes: &HashMap<ProcKey, u64>,
    sessions: &[SessionRoot],
) -> Vec<SessionActivity> {
    let parent: HashMap<u32, u32> = procs.iter().map(|p| (p.pid, p.ppid)).collect();
    let own_pid = std::process::id();
    let mut root_owner: HashMap<u32, usize> = sessions
        .iter()
        .enumerate()
        .flat_map(|(index, session)| session.pids.iter().map(move |pid| (*pid, index)))
        .collect();
    for (index, session) in sessions.iter().enumerate() {
        for pid in &session.dependent_pids {
            let mut cursor = parent.get(pid).copied();
            for _ in 0..64 {
                match cursor {
                    Some(ancestor) if root_owner.get(&ancestor) == Some(&index) => {
                        root_owner.insert(*pid, index);
                        break;
                    }
                    Some(ancestor) if ancestor > 1 => cursor = parent.get(&ancestor).copied(),
                    _ => break,
                }
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
    let mut tree_index: HashMap<(usize, u32), usize> = HashMap::new();
    let names: HashMap<u32, &str> = procs.iter().map(|p| (p.pid, p.name.as_str())).collect();

    for proc in procs {
        if root_owner.contains_key(&proc.pid) || proc.pid == own_pid {
            continue;
        }
        // Climb to the nearest agent process; the step below it is the tree top.
        let (mut child, mut cursor) = (proc.pid, proc.ppid);
        let mut owner = None;
        for _ in 0..64 {
            if let Some(index) = root_owner.get(&cursor) {
                owner = Some(*index);
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
        let Some(index) = owner else { continue };
        let bytes = window_bytes
            .get(&(proc.pid, proc.start))
            .copied()
            .unwrap_or(0);
        let session = &mut activity[index];
        let slot = *tree_index.entry((index, child)).or_insert_with(|| {
            session.trees.push(CommandTree {
                top_pid: child,
                top_name: names.get(&child).copied().unwrap_or_default().to_string(),
                ..Default::default()
            });
            session.trees.len() - 1
        });
        let tree = &mut session.trees[slot];
        tree.members.push((proc.pid, proc.start));
        tree.bytes += bytes;
        session.bytes += bytes;
    }
    for session in &mut activity {
        session.trees.sort_by(|a, b| b.bytes.cmp(&a.bytes));
    }
    activity
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct PausedProcess {
    pub pid: u32,
    pub start: u64,
    pub name: String,
    pub session_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct WriterReport {
    pub session_id: String,
    pub provider: String,
    pub bytes_written_recently: u64,
    pub top_command: String,
}

/// What `longhouse-engine disk-guard status` and the app read.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct GuardState {
    pub level: Level,
    pub free_bytes: u64,
    pub burn_gib_per_min: f64,
    pub eta_minutes: Option<f64>,
    pub updated_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub writers: Vec<WriterReport>,
    #[serde(default)]
    pub paused: Vec<PausedProcess>,
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
    /// Desktop notification (title, body) for a level change.
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
    paused: Vec<PausedProcess>,
    /// Processes stopped at the last process-table read.
    stopped: HashSet<ProcKey>,
    last_steer: HashMap<String, (Level, Instant)>,
}

impl DiskGuard {
    /// Watch the volume holding `volume`. Processes a previous engine left
    /// paused are adopted, not resumed: the disk may still be critical. They
    /// resume through the normal recovery path, which starts from `freeze`.
    pub(crate) fn new(volume: PathBuf, state_path: Option<PathBuf>) -> Self {
        Self::with_free_source(volume, state_path, Box::new(free_bytes))
    }

    fn with_free_source(
        volume: PathBuf,
        state_path: Option<PathBuf>,
        free_source: FreeSource,
    ) -> Self {
        let adopted: Vec<PausedProcess> = state_path
            .as_deref()
            .and_then(read_state)
            .map(|previous| previous.paused)
            .unwrap_or_default()
            .into_iter()
            .filter(|p| process_start(p.pid) == Some(p.start))
            .collect();
        if !adopted.is_empty() {
            tracing::warn!(
                adopted = adopted.len(),
                "Disk guard adopted processes a previous engine left paused"
            );
        }
        let policy = if adopted.is_empty() {
            Policy::default()
        } else {
            Policy {
                level: Level::Freeze,
                calm_ticks: 0,
            }
        };
        let level = policy.level;
        Self {
            volume,
            free_source,
            state_path,
            samples: VecDeque::new(),
            policy,
            level,
            previous_counters: None,
            window: VecDeque::new(),
            paused: adopted,
            stopped: HashSet::new(),
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
        let previous_level = self.level;
        self.level = self.policy.update(free_gib, burn);
        let level = self.level;

        let pressured =
            level > Level::Ok || burn >= SNAPSHOT_RATE_GIB_PER_MIN || !self.paused.is_empty();
        let activity = if pressured {
            self.observe_processes(sessions)
        } else {
            self.previous_counters = None;
            self.window.clear();
            Vec::new()
        };

        let mut outcome = TickOutcome::default();
        let eta = eta_minutes(free_gib, burn);

        if level == Level::Freeze {
            let newly = self.pause_command_trees(&activity);
            if !newly.is_empty() {
                let sessions_hit: HashSet<&str> =
                    newly.iter().map(|p| p.session_id.as_str()).collect();
                for session in activity
                    .iter()
                    .filter(|s| sessions_hit.contains(s.session_id.as_str()))
                {
                    let names: Vec<&str> = newly
                        .iter()
                        .filter(|p| p.session_id == session.session_id)
                        .map(|p| p.name.as_str())
                        .collect();
                    outcome
                        .steers
                        .push(steer(session, freeze_text(free_gib, burn, eta, &names)));
                    self.last_steer
                        .insert(session.session_id.clone(), (level, now));
                }
            }
        } else if !self.paused.is_empty() {
            let resumed = resume_processes(&self.paused);
            tracing::info!(
                resumed,
                level = level.as_str(),
                "Disk guard resumed paused command trees"
            );
            self.paused.clear();
            outcome.notify = Some((
                "Longhouse: disk recovered".to_string(),
                format!("{free_gib:.0} GiB free. Paused agent commands were resumed."),
            ));
        }

        if level >= Level::Watch && level < Level::Freeze {
            for session in &activity {
                if session.bytes < STEER_MIN_BYTES {
                    continue;
                }
                let due = match self.last_steer.get(&session.session_id) {
                    Some((sent_level, sent_at)) => {
                        level > *sent_level || now.duration_since(*sent_at) >= RESTEER_INTERVAL
                    }
                    None => true,
                };
                if due {
                    outcome.steers.push(steer(
                        session,
                        pressure_text(level, free_gib, burn, eta, session),
                    ));
                    self.last_steer
                        .insert(session.session_id.clone(), (level, now));
                }
            }
        }
        if level == Level::Ok {
            self.last_steer.clear();
        }

        if level > previous_level && level >= Level::Limp {
            outcome.notify = Some((
                format!("Longhouse: disk {}", level.as_str()),
                match eta {
                    Some(eta) => format!("{free_gib:.0} GiB free, falling {burn:.1} GiB/min (~{eta:.0} min to full)."),
                    None => format!("{free_gib:.0} GiB free."),
                } + if level == Level::Freeze { " Agent commands paused." } else { "" },
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
                    top_command: s
                        .trees
                        .first()
                        .map(|t| t.top_name.clone())
                        .unwrap_or_default(),
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
                    paused: self.paused.clone(),
                },
            );
        }
        outcome
    }

    fn observe_processes(&mut self, sessions: &[SessionRoot]) -> Vec<SessionActivity> {
        let procs = read_processes();
        self.stopped = procs
            .iter()
            .filter(|p| p.stopped)
            .map(|p| (p.pid, p.start))
            .collect();
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

    fn pause_command_trees(&mut self, activity: &[SessionActivity]) -> Vec<PausedProcess> {
        let ours: HashSet<ProcKey> = self.paused.iter().map(|p| (p.pid, p.start)).collect();
        let mut planned = Vec::new();
        let mut restop = Vec::new();
        for session in activity {
            for tree in &session.trees {
                // Top first, so the tree cannot spawn new work mid-pause.
                let mut members = tree.members.clone();
                members.sort_by_key(|(pid, _)| *pid != tree.top_pid);
                for key in members {
                    if self.stopped.contains(&key) {
                        // Stopped by someone else (never ours to resume), or
                        // still paused by us.
                        continue;
                    }
                    if ours.contains(&key) {
                        // Ours, but continued behind our back (`disk-guard
                        // resume` while still critical): stop it again.
                        restop.push(key);
                        continue;
                    }
                    planned.push(PausedProcess {
                        pid: key.0,
                        start: key.1,
                        name: if key.0 == tree.top_pid {
                            tree.top_name.clone()
                        } else {
                            String::new()
                        },
                        session_id: session.session_id.clone(),
                    });
                }
            }
        }
        for (pid, start) in restop {
            signal_if_same(pid, start, libc::SIGSTOP);
        }
        if planned.is_empty() {
            return Vec::new();
        }
        // Record before signalling, so an engine that dies right after still
        // leaves a list to resume. On a full disk this write may fail; the
        // pause still happens, because keeping the host alive comes first.
        self.paused.extend(planned.iter().cloned());
        self.persist_paused();
        let mut newly: Vec<PausedProcess> = Vec::new();
        let mut failed: HashSet<ProcKey> = HashSet::new();
        for process in planned {
            if signal_if_same(process.pid, process.start, libc::SIGSTOP) {
                newly.push(process);
            } else {
                failed.insert((process.pid, process.start));
            }
        }
        self.paused.retain(|p| !failed.contains(&(p.pid, p.start)));
        if !newly.is_empty() {
            tracing::warn!(
                paused = newly.len(),
                "Disk guard paused agent command trees"
            );
        }
        newly.retain(|p| !p.name.is_empty());
        newly
    }

    fn persist_paused(&self) {
        let Some(path) = self.state_path.as_deref() else {
            return;
        };
        let mut state = read_state(path).unwrap_or_default();
        state.level = Level::Freeze;
        state.paused = self.paused.clone();
        state.updated_at = Some(Utc::now());
        write_state(path, &state);
    }
}

fn steer(session: &SessionActivity, text: String) -> Steer {
    Steer {
        session_id: session.session_id.clone(),
        provider: session.provider.clone(),
        text,
    }
}

fn describe(free_gib: f64, burn: f64, eta: Option<f64>) -> String {
    match eta {
        Some(eta) => format!(
            "{free_gib:.0} GiB free and falling {burn:.1} GiB/min (about {eta:.0} min to full)"
        ),
        None => format!("{free_gib:.0} GiB free"),
    }
}

fn pressure_text(
    level: Level,
    free_gib: f64,
    burn: f64,
    eta: Option<f64>,
    session: &SessionActivity,
) -> String {
    let top = session
        .trees
        .first()
        .map(|t| format!(" (mostly `{}`)", t.top_name))
        .unwrap_or_default();
    format!(
        "[Longhouse disk guard: {level}] This machine's disk is at {state}. Your commands wrote {mib} MiB in the last two minutes{top}. \
Do not start new builds, installs, worktrees, Docker images or simulator runs until space recovers. \
Delete scratch output you created and no longer need, prefer a shared build cache over a fresh target or DerivedData directory, \
then continue with low-write work. `longhouse-engine disk-guard status` shows the current state.",
        level = level.as_str(),
        state = describe(free_gib, burn, eta),
        mib = session.bytes / MIB,
    )
}

fn freeze_text(free_gib: f64, burn: f64, eta: Option<f64>, paused: &[&str]) -> String {
    let list = if paused.is_empty() {
        String::new()
    } else {
        format!(" (`{}`)", paused.join("`, `"))
    };
    format!(
        "[Longhouse disk guard: freeze] This machine's disk is at {state}. To keep it from locking up, your running commands{list} \
are paused (SIGSTOP), not killed. They resume on their own once free space recovers. When you can act, free space first: \
delete scratch output you created; do not retry or restart the paused command. `longhouse-engine disk-guard status` shows the current state.",
        state = describe(free_gib, burn, eta),
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

/// Signal `pid` only if it is still the process that started at `start`, and
/// is never the engine itself.
fn signal_if_same(pid: u32, start: u64, signal: libc::c_int) -> bool {
    if pid <= 1 || pid == std::process::id() || process_start(pid) != Some(start) {
        return false;
    }
    // SAFETY: kill has no memory effects; identity was checked just above.
    unsafe { libc::kill(pid as libc::pid_t, signal) == 0 }
}

pub(crate) fn resume_processes(paused: &[PausedProcess]) -> usize {
    paused
        .iter()
        .filter(|p| signal_if_same(p.pid, p.start, libc::SIGCONT))
        .count()
}

#[cfg(target_os = "macos")]
fn rusage(pid: u32) -> Option<libc::rusage_info_v4> {
    let mut info = std::mem::MaybeUninit::<libc::rusage_info_v4>::zeroed();
    // SAFETY: proc_pid_rusage writes a rusage_info_v4 for flavor V4.
    let rc = unsafe {
        libc::proc_pid_rusage(
            pid as libc::c_int,
            libc::RUSAGE_INFO_V4,
            info.as_mut_ptr() as *mut libc::rusage_info_t,
        )
    };
    // SAFETY: zero-initialised and filled on success.
    (rc == 0).then(|| unsafe { info.assume_init() })
}

#[cfg(target_os = "macos")]
fn process_start(pid: u32) -> Option<u64> {
    rusage(pid).map(|info| info.ri_proc_start_abstime)
}

#[cfg(not(target_os = "macos"))]
fn process_start(_pid: u32) -> Option<u64> {
    None
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
            let info = rusage(pid as u32)?;
            let name = c_name(&bsd.pbi_name)
                .filter(|n| !n.is_empty())
                .or_else(|| c_name(&bsd.pbi_comm))?;
            Some(ProcInfo {
                pid: pid as u32,
                ppid: bsd.pbi_ppid,
                start: info.ri_proc_start_abstime,
                written: info.ri_diskio_byteswritten,
                name,
                // SSTOP in <sys/proc.h>.
                stopped: bsd.pbi_status == 4,
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
        assert_eq!(classify(35.0, 0.0), Level::Watch);
        assert_eq!(classify(18.0, 0.0), Level::Limp);
        assert_eq!(classify(9.0, 0.0), Level::Freeze);
        // 2026-09-25: 18.8 GiB free, losing ~9.5 GiB/min -> two minutes left.
        assert_eq!(classify(18.8, 9.5), Level::Freeze);
        // 60 GiB free but a sustained 2 GiB/min build: 30 min to full.
        assert_eq!(classify(60.0, 2.0), Level::Watch);
        assert_eq!(classify(25.0, 2.0), Level::Limp);
        // Slow drift is not projected.
        assert_eq!(classify(45.0, 0.1), Level::Ok);
    }

    #[test]
    fn policy_escalates_at_once_and_recovers_with_margin() {
        let mut policy = Policy::default();
        assert_eq!(policy.update(8.0, 0.0), Level::Freeze);
        // Writers stopped at 11 GiB: still inside the margin, stay frozen.
        assert_eq!(policy.update(11.0, 0.0), Level::Freeze);
        assert_eq!(policy.update(11.0, 0.0), Level::Freeze);
        // Clear of the margin, but recovery needs consecutive calm ticks.
        assert_eq!(policy.update(16.0, 0.0), Level::Freeze);
        assert_eq!(policy.update(16.0, 0.0), Level::Limp);
        // A new burst escalates immediately.
        assert_eq!(policy.update(16.0, 5.0), Level::Freeze);
    }

    #[test]
    fn burn_rate_takes_the_steeper_window() {
        // Flat for two minutes, then 9 GiB in the last minute.
        let s = samples(&[(0, 40.0), (60, 40.0), (120, 40.0), (150, 35.0), (180, 31.0)]);
        let rate = burn_rate(&s);
        assert!((rate - 9.0).abs() < 0.1, "rate {rate}");
        // Space coming back is not a burn.
        assert_eq!(burn_rate(&samples(&[(0, 20.0), (60, 30.0)])), 0.0);
        // Too short a span says nothing.
        assert_eq!(burn_rate(&samples(&[(0, 20.0), (10, 10.0)])), 0.0);
    }

    fn proc(pid: u32, ppid: u32, name: &str) -> ProcInfo {
        ProcInfo {
            pid,
            ppid,
            start: pid as u64 * 10,
            written: 0,
            name: name.into(),
            stopped: false,
        }
    }

    #[test]
    fn attribute_groups_descendants_into_command_trees() {
        // launcher 100 -> omp 101 -> zsh 200 -> make 201 -> xcodebuild 202
        //                         -> mcp 300
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
        let window = HashMap::from([((202, 2020), 900 * MIB), ((900, 9000), 5000 * MIB)]);
        let sessions = vec![SessionRoot {
            session_id: "s1".into(),
            provider: "omp".into(),
            pids: vec![100, 101],
            dependent_pids: vec![],
        }];
        let activity = attribute(&procs, &window, &sessions);
        assert_eq!(activity.len(), 1);
        let s1 = &activity[0];
        assert_eq!(
            s1.bytes,
            900 * MIB,
            "unrelated writers are not charged to the session"
        );
        assert_eq!(s1.trees.len(), 2);
        assert_eq!(s1.trees[0].top_pid, 200);
        assert_eq!(s1.trees[0].top_name, "zsh");
        assert_eq!(s1.trees[0].members.len(), 3);
        assert_eq!(s1.trees[1].top_pid, 300);
        assert!(
            s1.trees
                .iter()
                .all(|t| !t.members.iter().any(|(pid, _)| *pid == 101)),
            "the agent process itself is never part of a command tree"
        );
    }

    #[test]
    fn signal_refuses_mismatched_identity() {
        let me = std::process::id();
        assert!(!signal_if_same(me, u64::MAX, 0));
        assert!(!signal_if_same(1, 0, 0));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn freeze_pauses_real_command_trees_and_recovery_resumes_them() {
        use std::sync::{Arc, Mutex};
        fn stat(pid: u32) -> String {
            let out = std::process::Command::new("ps")
                .args(["-o", "stat=", "-p", &pid.to_string()])
                .output()
                .unwrap();
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        }
        let mut child = std::process::Command::new("/bin/sh")
            .args(["-c", "sleep 60 & wait"])
            .spawn()
            .unwrap();
        std::thread::sleep(Duration::from_millis(300));
        // Stopped by the user (job control) before the freeze: never ours.
        let mut user_stopped = std::process::Command::new("/bin/sleep")
            .arg("60")
            .spawn()
            .unwrap();
        unsafe { libc::kill(user_stopped.id() as libc::pid_t, libc::SIGSTOP) };
        std::thread::sleep(Duration::from_millis(100));
        let free = Arc::new(Mutex::new(5.0 * GIB));
        let source = free.clone();
        let dir = std::env::temp_dir().join(format!("disk-guard-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let state_path = dir.join("disk-guard.json");
        let mut guard = DiskGuard::with_free_source(
            dir.clone(),
            Some(state_path.clone()),
            Box::new(move |_| Some(*source.lock().unwrap() as u64)),
        );
        let sessions = vec![SessionRoot {
            session_id: "s1".into(),
            provider: "omp".into(),
            pids: vec![std::process::id()],
            dependent_pids: vec![],
        }];

        let outcome = guard.tick(&sessions);
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            assert!(
                stat(child.id()).starts_with('T'),
                "shell stopped, got {:?}",
                stat(child.id())
            );
            let state = read_state(&state_path).unwrap();
            assert_eq!(state.level, Level::Freeze);
            assert!(state.paused.iter().any(|p| p.pid == child.id()));
            assert!(
                state.paused.iter().all(|p| p.pid != user_stopped.id()),
                "an already-stopped process is not recorded as ours"
            );

            // A restarted engine adopts the paused set instead of resuming
            // it while the disk is still critical.
            let adopt_free = free.clone();
            let mut restarted = DiskGuard::with_free_source(
                dir.clone(),
                Some(state_path.clone()),
                Box::new(move |_| Some(*adopt_free.lock().unwrap() as u64)),
            );
            assert!(
                stat(child.id()).starts_with('T'),
                "adoption does not resume"
            );
            restarted.tick(&sessions);
            assert!(
                stat(child.id()).starts_with('T'),
                "still critical, still paused"
            );
            guard = restarted;
            assert!(
                state.paused.iter().all(|p| p.pid != std::process::id()),
                "the agent itself is never paused"
            );
            assert_eq!(outcome.steers.len(), 1, "the paused session is told why");
            assert!(outcome.steers[0]
                .text
                .contains("paused (SIGSTOP), not killed"));

            *free.lock().unwrap() = 50.0 * GIB;
            guard.tick(&sessions);
            assert!(
                stat(child.id()).starts_with('T'),
                "recovery needs consecutive calm ticks"
            );
            let outcome = guard.tick(&sessions);
            assert!(
                !stat(child.id()).starts_with('T'),
                "resumed, got {:?}",
                stat(child.id())
            );
            assert!(outcome.notify.is_some());
            assert!(read_state(&state_path).unwrap().paused.is_empty());
            assert!(
                stat(user_stopped.id()).starts_with('T'),
                "the user's stop is left alone"
            );
        }));
        unsafe { libc::kill(user_stopped.id() as libc::pid_t, libc::SIGCONT) };
        let _ = user_stopped.kill();
        let _ = user_stopped.wait();
        let _ = child.kill();
        let _ = child.wait();
        // `sleep` was stopped with its shell; make sure it is not left behind.
        let _ = std::process::Command::new("pkill")
            .args(["-CONT", "-P", &child.id().to_string()])
            .status();
        let _ = std::process::Command::new("pkill")
            .args(["-P", &child.id().to_string()])
            .status();
        let _ = std::fs::remove_dir_all(&dir);
        if let Err(panic) = result {
            std::panic::resume_unwind(panic);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn reads_own_process_with_counters() {
        let me = std::process::id();
        let procs = read_processes();
        let own = procs
            .iter()
            .find(|p| p.pid == me)
            .expect("own process listed");
        assert_eq!(Some(own.start), process_start(me));
        assert!(
            !signal_if_same(me, own.start, 0),
            "the engine never signals itself, even with a matching identity"
        );
    }

    #[test]
    fn dependent_pids_are_roots_only_under_a_verified_pid() {
        // launcher 100 (verified) -> cursor 101 (dependent) -> bash 102
        // recycled 500 (dependent, unrelated) -> vim 501
        let procs = vec![
            proc(100, 1, "longhouse-engine"),
            proc(101, 100, "cursor-agent"),
            proc(102, 101, "bash"),
            proc(500, 1, "zsh"),
            proc(501, 500, "vim"),
        ];
        let sessions = vec![SessionRoot {
            session_id: "c1".into(),
            provider: "cursor".into(),
            pids: vec![100],
            dependent_pids: vec![101, 500],
        }];
        let activity = attribute(&procs, &HashMap::new(), &sessions);
        let tops: Vec<u32> = activity[0].trees.iter().map(|t| t.top_pid).collect();
        assert_eq!(
            tops,
            vec![102],
            "the TUI is agent, not a command tree; the recycled pid owns nothing"
        );
    }
}
