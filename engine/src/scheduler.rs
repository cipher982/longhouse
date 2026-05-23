//! Path-keyed scheduler for daemon shipping work.
//!
//! Ensures at most one in-flight task per session file path while allowing
//! bounded concurrency across unrelated files. Ready work is weighted so live
//! watcher events get more slots without starving retry/scan work.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// Ready-work priority, ordered from highest urgency to lowest.
#[derive(Copy, Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum WorkPriority {
    Live,
    Retry,
    Scan,
}

const FAIR_SEQUENCE: [WorkPriority; 3] =
    [WorkPriority::Live, WorkPriority::Retry, WorkPriority::Scan];

/// Per-priority concurrency cap for Live work. Live can always burst up to
/// this number even when backlog work is hot.
const LIVE_IN_FLIGHT_CAP: usize = 8;

/// Number of `max_in_flight` slots reserved for Live work. Retry+Scan combined
/// cannot take more than `max_in_flight - LIVE_RESERVED` slots, so a backlog
/// burst (e.g. reconciliation scan after a long sleep) cannot drain all worker
/// slots and stall live shipping. On hosts where `max_in_flight` is smaller
/// than `LIVE_RESERVED`, Retry+Scan still get one slot so they can drain.
const LIVE_RESERVED: usize = LIVE_IN_FLIGHT_CAP;

/// Backlog cap floor — used as both the cold-start cap and the AIMD floor.
const BACKLOG_CAP_FLOOR: usize = 1;

/// Backlog cap ceiling. The Runtime Host serializes SQLite writes per tenant
/// behind a single mutex, so concurrency past ~16 mostly adds queue wait
/// without raising goodput. Capped here to bound the AIMD search space.
const BACKLOG_CAP_CEILING: usize = 16;

/// Target SLO for server-side ingest queue wait. AIMD increases the cap when
/// the EWMA stays below this and halves it when the EWMA crosses above.
const TARGET_QUEUE_WAIT_MS: f64 = 200.0;

/// EWMA smoothing factor for `queue_wait_ms`. Hand-picked to give a roughly
/// 4-sample memory: a single spike does not flip the cap, but a sustained
/// pattern moves the EWMA decisively.
const EWMA_ALPHA: f64 = 0.3;

/// Damping window. AIMD only adjusts the cap when *both* counters elapse:
/// at least N observations and at least M ms since the last adjust. This
/// keeps the cap from oscillating on noise while still allowing fast cold
/// ramp-up (each successful ship is one observation).
const DAMP_MIN_SAMPLES: u32 = 4;
const DAMP_MIN_INTERVAL_MS: u64 = 500;

/// Last direction the limiter moved the cap.
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum LimiterDirection {
    Held,
    Increased,
    Decreased,
}

impl LimiterDirection {
    pub fn as_str(self) -> &'static str {
        match self {
            LimiterDirection::Held => "held",
            LimiterDirection::Increased => "increased",
            LimiterDirection::Decreased => "decreased",
        }
    }
}

/// Adaptive concurrency limiter for the backlog (Retry+Scan) lane.
///
/// Drives an AIMD controller off `X-Ingest-Queue-Wait-Ms` returned by the
/// Runtime Host. The signal is direct (we measure how long the request waited
/// behind the SQLite write serializer), so plain AIMD beats inferred-capacity
/// schemes like Gradient2 / Vegas for this workload.
///
/// Concurrency model: `current_cap` is `AtomicUsize` for hot reads on the
/// scheduler path. State that only matters for adjustment decisions
/// (EWMA, damping, observation counters) is behind a single `Mutex` and is
/// only touched on `observe()` / `note_missing_signal()`.
pub struct AdaptiveLimiter {
    current_cap: AtomicUsize,
    state: Mutex<AdaptiveLimiterState>,
}

#[derive(Debug)]
struct AdaptiveLimiterState {
    ewma_queue_wait_ms: Option<f64>,
    samples_since_adjust: u32,
    last_adjust: Option<Instant>,
    last_direction: LimiterDirection,
    total_observations: u64,
    total_increases: u64,
    total_decreases: u64,
    last_observed_queue_wait_ms: Option<f64>,
    missing_signal_logged: bool,
}

/// Snapshot of limiter state for engine status JSON / flight recorder.
#[derive(Debug, Clone, serde::Serialize)]
pub struct LimiterSnapshot {
    pub current_cap: usize,
    pub floor: usize,
    pub ceiling: usize,
    pub target_queue_wait_ms: f64,
    pub ewma_queue_wait_ms: Option<f64>,
    pub last_observed_queue_wait_ms: Option<f64>,
    pub last_direction: &'static str,
    pub total_observations: u64,
    pub total_increases: u64,
    pub total_decreases: u64,
}

impl AdaptiveLimiter {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            current_cap: AtomicUsize::new(BACKLOG_CAP_FLOOR),
            state: Mutex::new(AdaptiveLimiterState {
                ewma_queue_wait_ms: None,
                samples_since_adjust: 0,
                last_adjust: None,
                last_direction: LimiterDirection::Held,
                total_observations: 0,
                total_increases: 0,
                total_decreases: 0,
                last_observed_queue_wait_ms: None,
                missing_signal_logged: false,
            }),
        })
    }

    pub fn current_cap(&self) -> usize {
        self.current_cap.load(Ordering::Relaxed)
    }

    /// Feed a successful ship's observed `queue_wait_ms` into the controller.
    /// Only called for `ShipResult::Ok` with a populated server timing header;
    /// missing-header successes go through [`Self::note_missing_signal`].
    pub fn observe(&self, queue_wait_ms: f64) {
        if !queue_wait_ms.is_finite() || queue_wait_ms < 0.0 {
            return;
        }
        let mut state = self.state.lock().expect("limiter state poisoned");
        state.total_observations = state.total_observations.saturating_add(1);
        state.last_observed_queue_wait_ms = Some(queue_wait_ms);
        state.ewma_queue_wait_ms = Some(match state.ewma_queue_wait_ms {
            Some(prev) => EWMA_ALPHA * queue_wait_ms + (1.0 - EWMA_ALPHA) * prev,
            None => queue_wait_ms,
        });
        state.samples_since_adjust = state.samples_since_adjust.saturating_add(1);
        state.missing_signal_logged = false;
        self.try_adjust(&mut state);
    }

    /// Successful ship but no server timing header (older / bridged Runtime
    /// Host). Per phase-2 review: freeze the cap at its current value and log
    /// once. We still bump observation counters so debugging telemetry shows
    /// the controller is alive.
    pub fn note_missing_signal(&self) {
        let mut state = self.state.lock().expect("limiter state poisoned");
        state.total_observations = state.total_observations.saturating_add(1);
        if !state.missing_signal_logged {
            state.missing_signal_logged = true;
            tracing::info!(
                target: "longhouse_engine::adaptive_limiter",
                current_cap = self.current_cap.load(Ordering::Relaxed),
                "no X-Ingest-Queue-Wait-Ms header on successful ship; freezing adaptive cap"
            );
        }
    }

    fn try_adjust(&self, state: &mut AdaptiveLimiterState) {
        if state.samples_since_adjust < DAMP_MIN_SAMPLES {
            return;
        }
        if let Some(last) = state.last_adjust {
            if last.elapsed().as_millis() < u128::from(DAMP_MIN_INTERVAL_MS) {
                return;
            }
        }
        let Some(ewma) = state.ewma_queue_wait_ms else {
            return;
        };
        let cap = self.current_cap.load(Ordering::Relaxed);
        let new_cap = if ewma > TARGET_QUEUE_WAIT_MS {
            (cap / 2).max(BACKLOG_CAP_FLOOR)
        } else {
            cap.saturating_add(1).min(BACKLOG_CAP_CEILING)
        };
        let direction = match new_cap.cmp(&cap) {
            std::cmp::Ordering::Greater => {
                state.total_increases = state.total_increases.saturating_add(1);
                LimiterDirection::Increased
            }
            std::cmp::Ordering::Less => {
                state.total_decreases = state.total_decreases.saturating_add(1);
                LimiterDirection::Decreased
            }
            std::cmp::Ordering::Equal => LimiterDirection::Held,
        };
        state.last_direction = direction;
        state.last_adjust = Some(Instant::now());
        state.samples_since_adjust = 0;
        if direction != LimiterDirection::Held {
            self.current_cap.store(new_cap, Ordering::Relaxed);
            tracing::debug!(
                target: "longhouse_engine::adaptive_limiter",
                from_cap = cap,
                to_cap = new_cap,
                ewma_queue_wait_ms = ewma,
                target_ms = TARGET_QUEUE_WAIT_MS,
                direction = direction.as_str(),
                "adaptive limiter adjusted"
            );
        }
    }

    /// Test-only: clear the wall-clock damping cooldown so the next observation
    /// burst can trigger another adjustment without waiting `DAMP_MIN_INTERVAL_MS`.
    /// Production code never calls this; the controller relies on real elapsed
    /// time to keep the cap from oscillating on noise.
    #[cfg(test)]
    pub fn clear_adjust_cooldown(&self) {
        self.state
            .lock()
            .expect("limiter state poisoned")
            .last_adjust = None;
    }

    pub fn snapshot(&self) -> LimiterSnapshot {
        let cap = self.current_cap.load(Ordering::Relaxed);
        let state = self.state.lock().expect("limiter state poisoned");
        LimiterSnapshot {
            current_cap: cap,
            floor: BACKLOG_CAP_FLOOR,
            ceiling: BACKLOG_CAP_CEILING,
            target_queue_wait_ms: TARGET_QUEUE_WAIT_MS,
            ewma_queue_wait_ms: state.ewma_queue_wait_ms,
            last_observed_queue_wait_ms: state.last_observed_queue_wait_ms,
            last_direction: state.last_direction.as_str(),
            total_observations: state.total_observations,
            total_increases: state.total_increases,
            total_decreases: state.total_decreases,
        }
    }
}

/// One unit of daemon work keyed to a single file path.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PathJob {
    pub path: PathBuf,
    pub provider: &'static str,
    pub priority: WorkPriority,
    pub observation: ObservationTrace,
}

/// Timing metadata for the code path that noticed work before a ship job ran.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObservationTrace {
    pub source: &'static str,
    pub observed_at_ms: i64,
    pub latest_observed_at_ms: Option<i64>,
    pub wake_received_at_ms: Option<i64>,
    pub enqueued_at_ms: i64,
    pub session_id: Option<String>,
    pub turn_id: Option<String>,
    pub wake_reason: Option<String>,
    pub file_len_hint: Option<u64>,
}

#[derive(Clone, Debug)]
struct ReadyJob {
    provider: &'static str,
    priority: WorkPriority,
    observation: ObservationTrace,
}

#[derive(Clone, Debug)]
struct InFlightJob {
    provider: &'static str,
    priority: WorkPriority,
    observation: ObservationTrace,
    rerun_priority: Option<WorkPriority>,
    rerun_observation: Option<ObservationTrace>,
}

/// Bounded scheduler that dedupes work by file path.
pub struct PathScheduler {
    max_in_flight: usize,
    fairness_cursor: usize,
    ready_live: VecDeque<PathBuf>,
    ready_retry: VecDeque<PathBuf>,
    ready_scan: VecDeque<PathBuf>,
    ready_jobs: HashMap<PathBuf, ReadyJob>,
    in_flight: HashMap<PathBuf, InFlightJob>,
    limiter: Arc<AdaptiveLimiter>,
}

impl PathScheduler {
    #[cfg(test)]
    pub fn new(max_in_flight: usize) -> Self {
        Self::with_limiter(max_in_flight, AdaptiveLimiter::new())
    }

    pub fn with_limiter(max_in_flight: usize, limiter: Arc<AdaptiveLimiter>) -> Self {
        Self {
            max_in_flight: max_in_flight.max(1),
            fairness_cursor: 0,
            ready_live: VecDeque::new(),
            ready_retry: VecDeque::new(),
            ready_scan: VecDeque::new(),
            ready_jobs: HashMap::new(),
            in_flight: HashMap::new(),
            limiter,
        }
    }

    /// Enqueue work for a path.
    ///
    /// If the path is already queued, the highest-priority source wins.
    /// If the path is already in flight, a rerun is recorded for completion.
    #[cfg(test)]
    pub fn enqueue(&mut self, path: PathBuf, provider: &'static str, priority: WorkPriority) {
        self.enqueue_observed(path, provider, priority, "unspecified", now_ms());
    }

    /// Enqueue work with the source/timestamp that first observed it.
    pub fn enqueue_observed(
        &mut self,
        path: PathBuf,
        provider: &'static str,
        priority: WorkPriority,
        observation_source: &'static str,
        observed_at_ms: i64,
    ) {
        let observation = ObservationTrace {
            source: observation_source,
            observed_at_ms,
            latest_observed_at_ms: None,
            wake_received_at_ms: None,
            enqueued_at_ms: now_ms(),
            session_id: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        };
        self.enqueue_observation(path, provider, priority, observation);
    }

    /// Enqueue work observed through a coalesced filesystem watcher batch.
    pub fn enqueue_observed_window(
        &mut self,
        path: PathBuf,
        provider: &'static str,
        priority: WorkPriority,
        observation_source: &'static str,
        observed_at_ms: i64,
        latest_observed_at_ms: i64,
    ) {
        let observation = ObservationTrace {
            source: observation_source,
            observed_at_ms,
            latest_observed_at_ms: Some(latest_observed_at_ms.max(observed_at_ms)),
            wake_received_at_ms: None,
            enqueued_at_ms: now_ms(),
            session_id: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        };
        self.enqueue_observation(path, provider, priority, observation);
    }

    pub fn enqueue_observation(
        &mut self,
        path: PathBuf,
        provider: &'static str,
        priority: WorkPriority,
        mut observation: ObservationTrace,
    ) {
        observation.enqueued_at_ms = now_ms();
        if let Some(ready) = self.ready_jobs.get_mut(&path) {
            if priority < ready.priority {
                ready.priority = priority;
                ready.observation = observation;
                self.push_ready_path(path, priority);
            } else if priority == ready.priority
                && should_replace_observation(&ready.observation, &observation)
            {
                ready.observation = observation;
            }
            return;
        }

        if let Some(in_flight) = self.in_flight.get_mut(&path) {
            in_flight.rerun_priority = merge_priority(in_flight.rerun_priority, Some(priority));
            match in_flight.rerun_observation.as_mut() {
                Some(current) if should_replace_observation(current, &observation) => {
                    *current = observation;
                }
                None => {
                    in_flight.rerun_observation = Some(observation);
                }
                Some(_) => {}
            }
            return;
        }

        self.ready_jobs.insert(
            path.clone(),
            ReadyJob {
                provider,
                priority,
                observation,
            },
        );
        self.push_ready_path(path, priority);
    }

    /// Return the next job that can start, respecting both the configured
    /// in-flight concurrency cap and a simple weighted round-robin policy.
    pub fn pop_launchable(&mut self) -> Option<PathJob> {
        if self.in_flight_count(WorkPriority::Live) < LIVE_IN_FLIGHT_CAP {
            if let Some(job) = self.pop_ready_queue(WorkPriority::Live) {
                return Some(job);
            }
        }

        if self.in_flight.len() >= self.max_in_flight {
            return None;
        }

        for step in 0..FAIR_SEQUENCE.len() {
            let idx = (self.fairness_cursor + step) % FAIR_SEQUENCE.len();
            let priority = FAIR_SEQUENCE[idx];
            if !self.can_launch_priority(priority) {
                continue;
            }
            if let Some(job) = self.pop_ready_queue(priority) {
                self.fairness_cursor = (idx + 1) % FAIR_SEQUENCE.len();
                return Some(job);
            }
        }

        None
    }

    /// Return live work only. Used while a foreground session is active so
    /// reconciliation and retry work cannot steal launch slots between polls.
    pub fn pop_launchable_live(&mut self) -> Option<PathJob> {
        if self.in_flight_count(WorkPriority::Live) < LIVE_IN_FLIGHT_CAP {
            return self.pop_ready_queue(WorkPriority::Live);
        }
        None
    }

    /// Mark a path as completed. If the path was re-enqueued while running, or
    /// the task requests a follow-up pass, the path is queued again.
    pub fn complete(&mut self, path: &Path, task_rerun: Option<WorkPriority>) {
        let Some(in_flight) = self.in_flight.remove(path) else {
            return;
        };

        if let Some(priority) = merge_priority(in_flight.rerun_priority, task_rerun) {
            let observation = in_flight.rerun_observation.unwrap_or(in_flight.observation);
            self.enqueue_observation(
                path.to_path_buf(),
                in_flight.provider,
                priority,
                observation,
            );
        }
    }

    pub fn has_in_flight(&self) -> bool {
        !self.in_flight.is_empty()
    }

    pub fn has_pending_work(&self) -> bool {
        !self.ready_jobs.is_empty() || !self.in_flight.is_empty()
    }

    #[cfg(test)]
    fn ready_len(&self) -> usize {
        self.ready_jobs.len()
    }

    #[cfg(test)]
    fn in_flight_len(&self) -> usize {
        self.in_flight.len()
    }

    fn pop_ready_queue(&mut self, expected_priority: WorkPriority) -> Option<PathJob> {
        let queue = match expected_priority {
            WorkPriority::Live => &mut self.ready_live,
            WorkPriority::Retry => &mut self.ready_retry,
            WorkPriority::Scan => &mut self.ready_scan,
        };

        while let Some(path) = queue.pop_front() {
            let Some(ready) = self.ready_jobs.get(&path).cloned() else {
                continue;
            };

            if ready.priority != expected_priority {
                continue;
            }

            self.ready_jobs.remove(&path);
            self.in_flight.insert(
                path.clone(),
                InFlightJob {
                    provider: ready.provider,
                    priority: ready.priority,
                    observation: ready.observation.clone(),
                    rerun_priority: None,
                    rerun_observation: None,
                },
            );

            return Some(PathJob {
                path,
                provider: ready.provider,
                priority: ready.priority,
                observation: ready.observation,
            });
        }

        None
    }

    fn push_ready_path(&mut self, path: PathBuf, priority: WorkPriority) {
        match priority {
            WorkPriority::Live => self.ready_live.push_back(path),
            WorkPriority::Retry => self.ready_retry.push_back(path),
            WorkPriority::Scan => self.ready_scan.push_back(path),
        }
    }

    fn can_launch_priority(&self, priority: WorkPriority) -> bool {
        match priority {
            WorkPriority::Live => self.in_flight_count(WorkPriority::Live) < LIVE_IN_FLIGHT_CAP,
            WorkPriority::Retry | WorkPriority::Scan => {
                let backlog_in_flight = self.in_flight_count(WorkPriority::Retry)
                    + self.in_flight_count(WorkPriority::Scan);
                // `LIVE_RESERVED` is kept as a hard floor for live latency
                // safety. The adaptive limiter chooses how aggressive to be
                // *within* the remaining backlog budget; defense in depth.
                let backlog_room = self.max_in_flight.saturating_sub(LIVE_RESERVED).max(1);
                let backlog_cap = backlog_room.min(self.limiter.current_cap());
                backlog_in_flight < backlog_cap
            }
        }
    }

    fn in_flight_count(&self, priority: WorkPriority) -> usize {
        self.in_flight
            .values()
            .filter(|job| job.priority == priority)
            .count()
    }
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn merge_priority(a: Option<WorkPriority>, b: Option<WorkPriority>) -> Option<WorkPriority> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }
}

fn should_replace_observation(current: &ObservationTrace, candidate: &ObservationTrace) -> bool {
    (candidate.source == "wake_socket" && current.source != "wake_socket")
        || (candidate.source == "wake_socket"
            && current.source == "wake_socket"
            && candidate.observed_at_ms > current.observed_at_ms)
        || (current.session_id.is_none() && candidate.session_id.is_some())
        || (current.wake_received_at_ms.is_none() && candidate.wake_received_at_ms.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ready_path_promotes_to_higher_priority_without_dup_launch() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue(path.clone(), "claude", WorkPriority::Scan);
        scheduler.enqueue(path.clone(), "claude", WorkPriority::Live);

        assert_eq!(scheduler.ready_len(), 1);
        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Live);
        assert!(scheduler.pop_launchable().is_none());
    }

    #[test]
    fn test_background_archive_work_is_capped_to_one_slot() {
        let mut scheduler = PathScheduler::new(32);

        scheduler.enqueue(
            PathBuf::from("/tmp/retry-a.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/retry-b.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/scan-c.jsonl"),
            "codex",
            WorkPriority::Scan,
        );

        let first = scheduler.pop_launchable().unwrap();
        assert_ne!(first.priority, WorkPriority::Live);
        assert!(
            scheduler.pop_launchable().is_none(),
            "background retry/scan work should not fan out even when many workers are configured"
        );
    }

    #[test]
    fn test_live_work_can_launch_while_background_archive_work_is_running() {
        let mut scheduler = PathScheduler::new(32);

        scheduler.enqueue(
            PathBuf::from("/tmp/retry.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        let retry = scheduler.pop_launchable().unwrap();
        assert_eq!(retry.priority, WorkPriority::Retry);

        scheduler.enqueue(
            PathBuf::from("/tmp/live.jsonl"),
            "codex",
            WorkPriority::Live,
        );
        let live = scheduler.pop_launchable().unwrap();
        assert_eq!(live.priority, WorkPriority::Live);
    }

    #[test]
    fn test_ready_path_keeps_wake_observation_for_same_priority() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observed(
            path.clone(),
            "codex",
            WorkPriority::Live,
            "manual_flush",
            100,
        );
        scheduler.enqueue_observation(
            path.clone(),
            "codex",
            WorkPriority::Live,
            ObservationTrace {
                source: "wake_socket",
                observed_at_ms: 110,
                latest_observed_at_ms: None,
                wake_received_at_ms: Some(111),
                enqueued_at_ms: 0,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("progress".to_string()),
                file_len_hint: Some(456),
            },
        );

        assert_eq!(scheduler.ready_len(), 1);
        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.observation.source, "wake_socket");
        assert_eq!(job.observation.session_id.as_deref(), Some("session-123"));
        assert_eq!(job.observation.wake_reason.as_deref(), Some("progress"));
    }

    #[test]
    fn test_ready_path_keeps_newest_wake_observation_for_same_priority() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observation(
            path.clone(),
            "codex",
            WorkPriority::Live,
            ObservationTrace {
                source: "wake_socket",
                observed_at_ms: 100,
                latest_observed_at_ms: None,
                wake_received_at_ms: Some(150),
                enqueued_at_ms: 0,
                session_id: Some("session-123".to_string()),
                turn_id: None,
                wake_reason: Some("binding".to_string()),
                file_len_hint: Some(100),
            },
        );
        scheduler.enqueue_observation(
            path,
            "codex",
            WorkPriority::Live,
            ObservationTrace {
                source: "wake_socket",
                observed_at_ms: 200,
                latest_observed_at_ms: None,
                wake_received_at_ms: Some(201),
                enqueued_at_ms: 0,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("turn_completed".to_string()),
                file_len_hint: Some(456),
            },
        );

        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.observation.source, "wake_socket");
        assert_eq!(job.observation.observed_at_ms, 200);
        assert_eq!(
            job.observation.wake_reason.as_deref(),
            Some("turn_completed")
        );
        assert_eq!(job.observation.file_len_hint, Some(456));
    }

    #[test]
    fn test_inflight_path_is_rerun_after_completion() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observed(path.clone(), "codex", WorkPriority::Scan, "scan", 100);
        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.priority, WorkPriority::Scan);
        assert_eq!(scheduler.in_flight_len(), 1);

        scheduler.enqueue_observed(path.clone(), "codex", WorkPriority::Live, "fsevent", 200);
        assert_eq!(scheduler.ready_len(), 0);

        scheduler.complete(&path, None);
        assert_eq!(scheduler.in_flight_len(), 0);
        assert_eq!(scheduler.ready_len(), 1);

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.priority, WorkPriority::Live);
        assert_eq!(rerun.observation.source, "fsevent");
        assert_eq!(rerun.observation.observed_at_ms, 200);
    }

    #[test]
    fn test_filesystem_observation_preserves_coalesced_window() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observed_window(path, "codex", WorkPriority::Live, "fsevent", 100, 150);

        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.observation.source, "fsevent");
        assert_eq!(job.observation.observed_at_ms, 100);
        assert_eq!(job.observation.latest_observed_at_ms, Some(150));
    }

    #[test]
    fn test_inflight_path_keeps_wake_observation_for_rerun() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observed(
            path.clone(),
            "codex",
            WorkPriority::Live,
            "outbox_signal",
            100,
        );
        let _ = scheduler.pop_launchable().unwrap();

        scheduler.enqueue_observation(
            path.clone(),
            "codex",
            WorkPriority::Live,
            ObservationTrace {
                source: "wake_socket",
                observed_at_ms: 110,
                latest_observed_at_ms: None,
                wake_received_at_ms: Some(111),
                enqueued_at_ms: 0,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("turn_completed".to_string()),
                file_len_hint: Some(456),
            },
        );
        scheduler.complete(&path, None);

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.priority, WorkPriority::Live);
        assert_eq!(rerun.observation.source, "wake_socket");
        assert_eq!(rerun.observation.session_id.as_deref(), Some("session-123"));
        assert_eq!(
            rerun.observation.wake_reason.as_deref(),
            Some("turn_completed")
        );
        assert_eq!(rerun.observation.file_len_hint, Some(456));
    }

    #[test]
    fn test_inflight_path_preserves_wake_observation_over_outbox_rerun() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue(path.clone(), "codex", WorkPriority::Live);
        let _ = scheduler.pop_launchable().unwrap();

        scheduler.enqueue_observation(
            path.clone(),
            "codex",
            WorkPriority::Live,
            ObservationTrace {
                source: "wake_socket",
                observed_at_ms: 110,
                latest_observed_at_ms: None,
                wake_received_at_ms: Some(111),
                enqueued_at_ms: 0,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("progress".to_string()),
                file_len_hint: Some(456),
            },
        );
        scheduler.enqueue_observed(
            path.clone(),
            "codex",
            WorkPriority::Live,
            "outbox_signal",
            120,
        );
        scheduler.complete(&path, None);

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.observation.source, "wake_socket");
        assert_eq!(rerun.observation.session_id.as_deref(), Some("session-123"));
        assert_eq!(rerun.observation.wake_reason.as_deref(), Some("progress"));
    }

    #[test]
    fn test_complete_merges_task_and_external_rerun_priority() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue(path.clone(), "gemini", WorkPriority::Retry);
        let _ = scheduler.pop_launchable().unwrap();

        scheduler.enqueue(path.clone(), "gemini", WorkPriority::Scan);
        scheduler.complete(&path, Some(WorkPriority::Live));

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.priority, WorkPriority::Live);
    }

    #[test]
    fn test_scheduler_respects_in_flight_cap() {
        let mut scheduler = PathScheduler::new(1);
        let path_a = PathBuf::from("/tmp/a.jsonl");
        let path_b = PathBuf::from("/tmp/b.jsonl");

        scheduler.enqueue(path_a.clone(), "claude", WorkPriority::Retry);
        scheduler.enqueue(path_b.clone(), "claude", WorkPriority::Retry);

        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, path_a);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.complete(&path_a, None);
        let second = scheduler.pop_launchable().unwrap();
        assert_eq!(second.path, path_b);
    }

    #[test]
    fn test_has_pending_work_covers_ready_and_inflight() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        assert!(!scheduler.has_pending_work());

        scheduler.enqueue(path.clone(), "codex", WorkPriority::Live);
        assert!(scheduler.has_pending_work());

        let _job = scheduler.pop_launchable().unwrap();
        assert!(scheduler.has_pending_work());

        scheduler.complete(&path, None);
        assert!(!scheduler.has_pending_work());
    }

    #[test]
    fn test_background_scan_is_capped_even_with_capacity_available() {
        let mut scheduler = PathScheduler::new(4);
        let scan_a = PathBuf::from("/tmp/scan-a.jsonl");
        let scan_b = PathBuf::from("/tmp/scan-b.jsonl");
        let live = PathBuf::from("/tmp/live.jsonl");

        scheduler.enqueue(scan_a.clone(), "codex", WorkPriority::Scan);
        scheduler.enqueue(scan_b.clone(), "codex", WorkPriority::Scan);

        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, scan_a);
        assert_eq!(first.priority, WorkPriority::Scan);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(live.clone(), "codex", WorkPriority::Live);
        let urgent = scheduler.pop_launchable().unwrap();
        assert_eq!(urgent.path, live);
        assert_eq!(urgent.priority, WorkPriority::Live);

        scheduler.complete(&scan_a, None);
        let second_scan = scheduler.pop_launchable().unwrap();
        assert_eq!(second_scan.path, scan_b);
        assert_eq!(second_scan.priority, WorkPriority::Scan);
    }

    #[test]
    fn test_retry_replay_is_capped_even_with_capacity_available() {
        let mut scheduler = PathScheduler::new(4);
        let retry_a = PathBuf::from("/tmp/retry-a.jsonl");
        let retry_b = PathBuf::from("/tmp/retry-b.jsonl");
        let live = PathBuf::from("/tmp/live.jsonl");

        scheduler.enqueue(retry_a.clone(), "codex", WorkPriority::Retry);
        scheduler.enqueue(retry_b.clone(), "codex", WorkPriority::Retry);

        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, retry_a);
        assert_eq!(first.priority, WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(live.clone(), "codex", WorkPriority::Live);
        let urgent = scheduler.pop_launchable().unwrap();
        assert_eq!(urgent.path, live);
        assert_eq!(urgent.priority, WorkPriority::Live);

        scheduler.complete(&retry_a, None);
        let second_retry = scheduler.pop_launchable().unwrap();
        assert_eq!(second_retry.path, retry_b);
        assert_eq!(second_retry.priority, WorkPriority::Retry);
    }

    #[test]
    fn test_retry_waits_when_archive_pool_is_full() {
        let mut scheduler = PathScheduler::new(1);
        let scan = PathBuf::from("/tmp/scan.jsonl");
        let retry = PathBuf::from("/tmp/retry.jsonl");

        scheduler.enqueue(scan.clone(), "codex", WorkPriority::Scan);
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );

        scheduler.enqueue(retry.clone(), "codex", WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.complete(&scan, None);
        let next = scheduler.pop_launchable().unwrap();
        assert_eq!(next.path, retry);
        assert_eq!(next.priority, WorkPriority::Retry);
    }

    #[test]
    fn test_live_work_can_overflow_full_archive_pool() {
        let mut scheduler = PathScheduler::new(1);

        scheduler.enqueue(
            PathBuf::from("/tmp/scan.jsonl"),
            "codex",
            WorkPriority::Scan,
        );
        assert!(scheduler.pop_launchable().is_some());

        for idx in 0..3 {
            scheduler.enqueue(
                PathBuf::from(format!("/tmp/live-{idx}.jsonl")),
                "codex",
                WorkPriority::Live,
            );
            assert_eq!(
                scheduler.pop_launchable().unwrap().priority,
                WorkPriority::Live
            );
        }
    }

    #[test]
    fn test_live_work_has_dedicated_lane_beyond_full_archive_pool() {
        let mut scheduler = PathScheduler::new(1);

        scheduler.enqueue(
            PathBuf::from("/tmp/scan.jsonl"),
            "codex",
            WorkPriority::Scan,
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );

        scheduler.enqueue(
            PathBuf::from("/tmp/retry.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(
            PathBuf::from("/tmp/live.jsonl"),
            "codex",
            WorkPriority::Live,
        );
        let live = scheduler.pop_launchable().unwrap();
        assert_eq!(live.priority, WorkPriority::Live);
        assert_eq!(live.path, PathBuf::from("/tmp/live.jsonl"));
    }

    #[test]
    fn test_live_only_launch_skips_background_work() {
        let mut scheduler = PathScheduler::new(4);
        let scan_path = PathBuf::from("/tmp/scan.jsonl");
        let retry_path = PathBuf::from("/tmp/retry.jsonl");
        scheduler.enqueue(scan_path.clone(), "codex", WorkPriority::Scan);
        scheduler.enqueue(retry_path.clone(), "codex", WorkPriority::Retry);

        assert!(scheduler.pop_launchable_live().is_none());

        scheduler.enqueue(
            PathBuf::from("/tmp/live.jsonl"),
            "codex",
            WorkPriority::Live,
        );
        let live = scheduler.pop_launchable_live().unwrap();
        assert_eq!(live.priority, WorkPriority::Live);
        assert_eq!(live.path, PathBuf::from("/tmp/live.jsonl"));

        // With combined backlog cap (= max_in_flight - LIVE_RESERVED, floored
        // at 1), retry and scan share one slot. Drain one before the other.
        let retry_job = scheduler.pop_launchable().unwrap();
        assert_eq!(retry_job.priority, WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.complete(&retry_path, None);
        let scan_job = scheduler.pop_launchable().unwrap();
        assert_eq!(scan_job.priority, WorkPriority::Scan);
    }

    #[test]
    fn test_scheduler_gives_retry_and_scan_turns_after_live_burst() {
        let mut scheduler = PathScheduler::new(10);

        for idx in 0..LIVE_IN_FLIGHT_CAP {
            scheduler.enqueue(
                PathBuf::from(format!("/tmp/live-{idx}.jsonl")),
                "claude",
                WorkPriority::Live,
            );
        }
        scheduler.enqueue(
            PathBuf::from("/tmp/retry.jsonl"),
            "claude",
            WorkPriority::Retry,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/scan.jsonl"),
            "claude",
            WorkPriority::Scan,
        );

        for _ in 0..LIVE_IN_FLIGHT_CAP {
            assert_eq!(
                scheduler.pop_launchable().unwrap().priority,
                WorkPriority::Live
            );
        }
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Retry
        );
        assert!(scheduler.pop_launchable().is_none());

        scheduler.complete(&PathBuf::from("/tmp/retry.jsonl"), None);
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );
    }

    /// Steady well-below-target queue waits should ramp the cap monotonically
    /// from the floor up to the ceiling.
    #[test]
    fn adaptive_limiter_ramps_up_under_low_queue_wait() {
        let limiter = AdaptiveLimiter::new();
        assert_eq!(limiter.current_cap(), BACKLOG_CAP_FLOOR);
        let mut last_cap = limiter.current_cap();
        for _ in 0..(BACKLOG_CAP_CEILING * 8) {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0); // well under TARGET_QUEUE_WAIT_MS
            }
            limiter.clear_adjust_cooldown();
            let now = limiter.current_cap();
            assert!(now >= last_cap, "cap regressed: {last_cap} -> {now}");
            last_cap = now;
            if now == BACKLOG_CAP_CEILING {
                break;
            }
        }
        assert_eq!(limiter.current_cap(), BACKLOG_CAP_CEILING);
        let snap = limiter.snapshot();
        assert!(snap.total_increases > 0);
        assert_eq!(snap.total_decreases, 0);
    }

    /// A sustained spike past the SLO must halve the cap, and a return to good
    /// queue waits must let it ramp back up again.
    #[test]
    fn adaptive_limiter_halves_on_spike_then_recovers() {
        let limiter = AdaptiveLimiter::new();
        // Climb to a non-floor cap first.
        for _ in 0..6 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(20.0);
            }
            limiter.clear_adjust_cooldown();
        }
        let pre_spike = limiter.current_cap();
        assert!(pre_spike > BACKLOG_CAP_FLOOR, "expected ramp-up first");

        // Sustained spike: feed enough above-target samples to dominate the EWMA.
        for _ in 0..16 {
            limiter.observe(2_000.0);
        }
        limiter.clear_adjust_cooldown();
        // Force one more adjust cycle on top of the spike.
        for _ in 0..DAMP_MIN_SAMPLES {
            limiter.observe(2_000.0);
        }
        let post_spike = limiter.current_cap();
        assert!(
            post_spike < pre_spike,
            "spike should drop cap: {pre_spike} -> {post_spike}"
        );
        assert!(post_spike >= BACKLOG_CAP_FLOOR);

        // Recovery: low queue waits must ramp back up.
        for _ in 0..32 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        assert!(
            limiter.current_cap() > post_spike,
            "expected recovery above {post_spike}, got {}",
            limiter.current_cap()
        );
        let snap = limiter.snapshot();
        assert!(snap.total_decreases > 0);
        assert!(snap.total_increases > 0);
    }

    /// Alternating below-target signal should keep the cap monotonically
    /// non-decreasing. The controller only halves when the *EWMA* crosses the
    /// SLO, so noise that averages below it must not cause oscillation.
    #[test]
    fn adaptive_limiter_holds_under_below_target_noise() {
        let limiter = AdaptiveLimiter::new();
        // Ramp to a steady cap with low queue waits.
        for _ in 0..4 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        let stable_cap = limiter.current_cap();

        // 50 samples alternating between 20ms and 150ms — both below target,
        // EWMA can never cross 200ms.
        for i in 0..50 {
            let sample = if i % 2 == 0 { 20.0 } else { 150.0 };
            limiter.observe(sample);
            if i % 4 == 3 {
                limiter.clear_adjust_cooldown();
            }
        }
        let snap = limiter.snapshot();
        assert_eq!(
            snap.total_decreases, 0,
            "below-target noise must not halve the cap"
        );
        assert!(
            limiter.current_cap() >= stable_cap,
            "cap regressed under benign noise: {stable_cap} -> {}",
            limiter.current_cap()
        );
    }

    /// Successful ships without a server timing header must freeze the cap
    /// rather than drifting it. The log-once flag prevents spam.
    #[test]
    fn adaptive_limiter_freezes_on_missing_signal() {
        let limiter = AdaptiveLimiter::new();
        // Climb to a non-floor cap.
        for _ in 0..3 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        let frozen_cap = limiter.current_cap();
        for _ in 0..50 {
            limiter.note_missing_signal();
        }
        assert_eq!(limiter.current_cap(), frozen_cap);
        let snap = limiter.snapshot();
        assert!(snap.total_observations >= 50);
    }
}
