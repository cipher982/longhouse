//! Path-keyed scheduler for daemon shipping work.
//!
//! Ensures at most one in-flight task per session file path while allowing
//! bounded concurrency across unrelated files. Ready work is weighted so live
//! watcher events get more slots without starving retry/scan work.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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
const SCAN_IN_FLIGHT_CAP: usize = 1;

/// Target SLO for server-side ingest queue wait. AIMD increases the cap when
/// the EWMA stays below this and halves it when the EWMA crosses above.
const TARGET_QUEUE_WAIT_MS: f64 = 200.0;
const LIVE_LATENCY_WARN_MS: u64 = 5_000;
const LIVE_LATENCY_SLA_MS: u64 = 10_000;
const LIVE_ENQUEUE_WARN_MS: u64 = 1_000;
const LIVE_ENQUEUE_CRITICAL_MS: u64 = 2_000;
const LIVE_PRESSURE_COOLDOWN: Duration = Duration::from_secs(30);
const LIVE_PRESSURE_CRITICAL_COOLDOWN: Duration = Duration::from_secs(60);

/// Archive replay request size controller. The lane starts conservatively,
/// shrinks hard under host pressure, and expands only when the direct Runtime
/// Host timing signals are comfortably below the interactive-write target.
pub const ARCHIVE_BATCH_TARGET_MIN_BYTES: u64 = 64 * 1024;
pub const ARCHIVE_BATCH_TARGET_BASE_BYTES: u64 = 256 * 1024;
pub const ARCHIVE_BATCH_TARGET_MAX_BYTES: u64 = 1024 * 1024;

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
const BACKPRESSURE_DEFAULT_COOLDOWN: Duration = Duration::from_secs(5);
const BACKPRESSURE_MAX_COOLDOWN: Duration = Duration::from_secs(60);

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
    ewma_exec_ms: Option<f64>,
    ewma_commit_ms: Option<f64>,
    samples_since_adjust: u32,
    last_adjust: Option<Instant>,
    last_direction: LimiterDirection,
    total_observations: u64,
    total_increases: u64,
    total_decreases: u64,
    total_backpressure: u64,
    last_observed_queue_wait_ms: Option<f64>,
    last_observed_exec_ms: Option<f64>,
    last_observed_commit_count: Option<u64>,
    last_observed_commit_ms: Option<f64>,
    last_observed_chunk_size: Option<u64>,
    last_observed_store_stage_ms: Option<BTreeMap<String, f64>>,
    last_backpressure_retry_after_ms: Option<u64>,
    backpressure_cooldown_until: Option<Instant>,
    last_live_latency_p95_ms: Option<u64>,
    last_live_enqueue_to_job_p95_ms: Option<u64>,
    live_pressure_cooldown_until: Option<Instant>,
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
    pub ewma_exec_ms: Option<f64>,
    pub last_observed_exec_ms: Option<f64>,
    pub ewma_commit_ms: Option<f64>,
    pub last_observed_commit_count: Option<u64>,
    pub last_observed_commit_ms: Option<f64>,
    pub last_observed_chunk_size: Option<u64>,
    pub last_observed_store_stage_ms: Option<BTreeMap<String, f64>>,
    pub pressure_state: &'static str,
    pub huge_range_eligible: bool,
    pub huge_range_suppressed_reason: Option<&'static str>,
    pub archive_target_batch_bytes: u64,
    pub live_latency_guard_state: &'static str,
    pub last_live_latency_p95_ms: Option<u64>,
    pub last_live_enqueue_to_job_p95_ms: Option<u64>,
    pub live_pressure_cooldown_remaining_ms: Option<u64>,
    pub last_direction: &'static str,
    pub total_observations: u64,
    pub total_increases: u64,
    pub total_decreases: u64,
    pub total_backpressure: u64,
    pub last_backpressure_retry_after_ms: Option<u64>,
    pub backpressure_cooldown_remaining_ms: Option<u64>,
}

/// Snapshot of scheduler pressure for engine status JSON / local health.
#[derive(Debug, Clone, serde::Serialize)]
pub struct SchedulerSnapshot {
    pub max_in_flight: usize,
    pub live_reserved: usize,
    pub live_in_flight_cap: usize,
    pub backlog_cap: usize,
    pub ready_live: usize,
    pub ready_retry: usize,
    pub ready_scan: usize,
    pub in_flight_live: usize,
    pub in_flight_retry: usize,
    pub in_flight_scan: usize,
    pub ready_backlog: usize,
    pub in_flight_backlog: usize,
    pub ready_retry_bytes: u64,
    pub ready_scan_bytes: u64,
    pub in_flight_retry_bytes: u64,
    pub in_flight_scan_bytes: u64,
    pub ready_backlog_bytes: u64,
    pub in_flight_backlog_bytes: u64,
}

impl AdaptiveLimiter {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            current_cap: AtomicUsize::new(BACKLOG_CAP_FLOOR),
            state: Mutex::new(AdaptiveLimiterState {
                ewma_queue_wait_ms: None,
                ewma_exec_ms: None,
                ewma_commit_ms: None,
                samples_since_adjust: 0,
                last_adjust: None,
                last_direction: LimiterDirection::Held,
                total_observations: 0,
                total_increases: 0,
                total_decreases: 0,
                total_backpressure: 0,
                last_observed_queue_wait_ms: None,
                last_observed_exec_ms: None,
                last_observed_commit_count: None,
                last_observed_commit_ms: None,
                last_observed_chunk_size: None,
                last_observed_store_stage_ms: None,
                last_backpressure_retry_after_ms: None,
                backpressure_cooldown_until: None,
                last_live_latency_p95_ms: None,
                last_live_enqueue_to_job_p95_ms: None,
                live_pressure_cooldown_until: None,
                missing_signal_logged: false,
            }),
        })
    }

    pub fn current_cap(&self) -> usize {
        self.current_cap.load(Ordering::Relaxed)
    }

    /// Test helper for queue-wait-only observations.
    #[cfg(test)]
    pub fn observe(&self, queue_wait_ms: f64) {
        self.observe_ingest_timing(queue_wait_ms, None, None, None, None, None);
    }

    /// Feed successful ingest timing into the controller.
    ///
    /// Queue wait drives the archive concurrency AIMD loop. Exec time is kept
    /// as first-class telemetry so local health can separate "queued behind
    /// the writer" from "writer itself is slow" before later controller slices
    /// tune batch sizing.
    pub fn observe_ingest_timing(
        &self,
        queue_wait_ms: f64,
        exec_ms: Option<f64>,
        commit_count: Option<u64>,
        commit_ms: Option<f64>,
        chunk_size: Option<u64>,
        store_stage_ms: Option<BTreeMap<String, f64>>,
    ) {
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
        if let Some(exec_ms) = exec_ms.filter(|value| value.is_finite() && *value >= 0.0) {
            state.last_observed_exec_ms = Some(exec_ms);
            state.ewma_exec_ms = Some(match state.ewma_exec_ms {
                Some(prev) => EWMA_ALPHA * exec_ms + (1.0 - EWMA_ALPHA) * prev,
                None => exec_ms,
            });
        }
        if let Some(commit_count) = commit_count {
            state.last_observed_commit_count = Some(commit_count);
        }
        if let Some(commit_ms) = commit_ms.filter(|value| value.is_finite() && *value >= 0.0) {
            state.last_observed_commit_ms = Some(commit_ms);
            state.ewma_commit_ms = Some(match state.ewma_commit_ms {
                Some(prev) => EWMA_ALPHA * commit_ms + (1.0 - EWMA_ALPHA) * prev,
                None => commit_ms,
            });
        }
        if let Some(chunk_size) = chunk_size {
            state.last_observed_chunk_size = Some(chunk_size);
        }
        if let Some(store_stage_ms) = store_stage_ms.filter(|stages| !stages.is_empty()) {
            state.last_observed_store_stage_ms = Some(store_stage_ms);
        }
        state.samples_since_adjust = state.samples_since_adjust.saturating_add(1);
        state.missing_signal_logged = false;
        self.try_adjust(&mut state);
    }

    /// Feed an explicit Runtime Host archive-admission backpressure signal.
    ///
    /// This is stronger than a high queue-wait sample: the host rejected
    /// reconstructable backlog work before write execution. Cut the backlog
    /// cap immediately, remember the retry-after window, and suppress cap
    /// increases until the cooldown expires.
    pub fn observe_backpressure(&self, retry_after: Option<Duration>) {
        let now = Instant::now();
        let retry_after = retry_after
            .unwrap_or(BACKPRESSURE_DEFAULT_COOLDOWN)
            .min(BACKPRESSURE_MAX_COOLDOWN);
        let retry_after_ms = retry_after.as_millis().min(u128::from(u64::MAX)) as u64;
        let mut state = self.state.lock().expect("limiter state poisoned");
        state.total_observations = state.total_observations.saturating_add(1);
        state.total_backpressure = state.total_backpressure.saturating_add(1);
        state.last_backpressure_retry_after_ms = Some(retry_after_ms);
        state.backpressure_cooldown_until = now.checked_add(retry_after);
        state.ewma_queue_wait_ms = Some(match state.ewma_queue_wait_ms {
            Some(prev) => prev.max(TARGET_QUEUE_WAIT_MS * 2.0),
            None => TARGET_QUEUE_WAIT_MS * 2.0,
        });
        state.samples_since_adjust = 0;
        state.last_adjust = Some(now);
        state.missing_signal_logged = false;

        let cap = self.current_cap.load(Ordering::Relaxed);
        let new_cap = (cap / 2).max(BACKLOG_CAP_FLOOR);
        if new_cap < cap {
            state.total_decreases = state.total_decreases.saturating_add(1);
            state.last_direction = LimiterDirection::Decreased;
            self.current_cap.store(new_cap, Ordering::Relaxed);
        } else {
            state.last_direction = LimiterDirection::Held;
        }
        tracing::info!(
            target: "longhouse_engine::adaptive_limiter",
            from_cap = cap,
            to_cap = self.current_cap.load(Ordering::Relaxed),
            retry_after_ms,
            "archive backpressure observed; backlog limiter cooled down"
        );
    }

    /// Feed the live-lane SLA guard. Archive work should consume only leftover
    /// capacity; when live p95 degrades, cut archive pressure before waiting
    /// for host backpressure.
    pub fn observe_live_latency(
        &self,
        latency_p95_ms: Option<u64>,
        enqueue_to_job_p95_ms: Option<u64>,
    ) {
        if latency_p95_ms.is_none() && enqueue_to_job_p95_ms.is_none() {
            return;
        }

        let now = Instant::now();
        let mut state = self.state.lock().expect("limiter state poisoned");
        state.last_live_latency_p95_ms = latency_p95_ms;
        state.last_live_enqueue_to_job_p95_ms = enqueue_to_job_p95_ms;

        let latency_warn = latency_p95_ms.is_some_and(|value| value >= LIVE_LATENCY_WARN_MS);
        let latency_critical = latency_p95_ms.is_some_and(|value| value >= LIVE_LATENCY_SLA_MS);
        let enqueue_warn = enqueue_to_job_p95_ms.is_some_and(|value| value >= LIVE_ENQUEUE_WARN_MS);
        let enqueue_critical =
            enqueue_to_job_p95_ms.is_some_and(|value| value >= LIVE_ENQUEUE_CRITICAL_MS);

        if !(latency_warn || enqueue_warn) {
            return;
        }

        let critical = latency_critical || enqueue_critical;
        let cooldown = if critical {
            LIVE_PRESSURE_CRITICAL_COOLDOWN
        } else {
            LIVE_PRESSURE_COOLDOWN
        };
        state.live_pressure_cooldown_until = now.checked_add(cooldown);
        state.samples_since_adjust = 0;
        state.last_adjust = Some(now);

        let cap = self.current_cap.load(Ordering::Relaxed);
        let new_cap = if critical {
            BACKLOG_CAP_FLOOR
        } else {
            (cap / 2).max(BACKLOG_CAP_FLOOR)
        };
        if new_cap < cap {
            state.total_decreases = state.total_decreases.saturating_add(1);
            state.last_direction = LimiterDirection::Decreased;
            self.current_cap.store(new_cap, Ordering::Relaxed);
        } else {
            state.last_direction = LimiterDirection::Held;
        }

        tracing::info!(
            target: "longhouse_engine::adaptive_limiter",
            from_cap = cap,
            to_cap = self.current_cap.load(Ordering::Relaxed),
            live_latency_p95_ms = ?latency_p95_ms,
            live_enqueue_to_job_p95_ms = ?enqueue_to_job_p95_ms,
            critical,
            "live lane latency guard reduced archive pressure"
        );
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

    fn huge_range_policy(
        state: &AdaptiveLimiterState,
        now: Instant,
    ) -> (bool, &'static str, Option<&'static str>) {
        if state
            .live_pressure_cooldown_until
            .is_some_and(|until| until > now)
        {
            return (
                false,
                "live_latency_pressure",
                Some("live_latency_pressure"),
            );
        }
        if state
            .backpressure_cooldown_until
            .is_some_and(|until| until > now)
        {
            return (
                false,
                "backpressure_cooldown",
                Some("backpressure_cooldown"),
            );
        }
        if state
            .ewma_queue_wait_ms
            .is_some_and(|ewma| ewma > TARGET_QUEUE_WAIT_MS)
        {
            // Queue pressure already forces one archive worker and the minimum
            // batch size. Do not also suppress paths whose remaining range is
            // large: if only large paths remain, that creates a closed loop
            // with no future observation capable of clearing the EWMA. The
            // explicit retry-after cooldown above is the admission gate.
            return (true, "host_queue_pressure", None);
        }
        (true, "normal", None)
    }

    pub fn huge_range_eligible(&self) -> bool {
        let state = self.state.lock().expect("limiter state poisoned");
        let (eligible, _, _) = Self::huge_range_policy(&state, Instant::now());
        eligible
    }

    fn archive_target_batch_bytes_for_state(state: &AdaptiveLimiterState, now: Instant) -> u64 {
        if state
            .live_pressure_cooldown_until
            .is_some_and(|until| until > now)
        {
            return ARCHIVE_BATCH_TARGET_MIN_BYTES;
        }

        if state
            .backpressure_cooldown_until
            .is_some_and(|until| until > now)
        {
            return ARCHIVE_BATCH_TARGET_MIN_BYTES;
        }

        let queue_wait = state.ewma_queue_wait_ms;
        if queue_wait.is_some_and(|ewma| ewma > TARGET_QUEUE_WAIT_MS) {
            return ARCHIVE_BATCH_TARGET_MIN_BYTES;
        }

        let exec_ms = state.ewma_exec_ms;
        if exec_ms.is_some_and(|ewma| ewma > TARGET_QUEUE_WAIT_MS * 2.0) {
            return ARCHIVE_BATCH_TARGET_BASE_BYTES;
        }

        if queue_wait.is_some_and(|ewma| ewma <= TARGET_QUEUE_WAIT_MS / 4.0)
            && exec_ms.map_or(true, |ewma| ewma <= TARGET_QUEUE_WAIT_MS)
        {
            return ARCHIVE_BATCH_TARGET_MAX_BYTES;
        }

        ARCHIVE_BATCH_TARGET_BASE_BYTES
    }

    pub fn archive_target_batch_bytes(&self) -> u64 {
        let state = self.state.lock().expect("limiter state poisoned");
        Self::archive_target_batch_bytes_for_state(&state, Instant::now())
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
        let now = Instant::now();
        if ewma <= TARGET_QUEUE_WAIT_MS
            && state
                .backpressure_cooldown_until
                .is_some_and(|until| until > now)
        {
            state.last_direction = LimiterDirection::Held;
            return;
        }
        if ewma <= TARGET_QUEUE_WAIT_MS
            && state
                .live_pressure_cooldown_until
                .is_some_and(|until| until > now)
        {
            state.last_direction = LimiterDirection::Held;
            return;
        }
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
        let now = Instant::now();
        let (huge_range_eligible, pressure_state, huge_range_suppressed_reason) =
            Self::huge_range_policy(&state, now);
        let archive_target_batch_bytes = Self::archive_target_batch_bytes_for_state(&state, now);
        let live_pressure_cooldown_remaining_ms =
            state.live_pressure_cooldown_until.and_then(|until| {
                until
                    .checked_duration_since(now)
                    .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
            });
        LimiterSnapshot {
            current_cap: cap,
            floor: BACKLOG_CAP_FLOOR,
            ceiling: BACKLOG_CAP_CEILING,
            target_queue_wait_ms: TARGET_QUEUE_WAIT_MS,
            ewma_queue_wait_ms: state.ewma_queue_wait_ms,
            last_observed_queue_wait_ms: state.last_observed_queue_wait_ms,
            ewma_exec_ms: state.ewma_exec_ms,
            last_observed_exec_ms: state.last_observed_exec_ms,
            ewma_commit_ms: state.ewma_commit_ms,
            last_observed_commit_count: state.last_observed_commit_count,
            last_observed_commit_ms: state.last_observed_commit_ms,
            last_observed_chunk_size: state.last_observed_chunk_size,
            last_observed_store_stage_ms: state.last_observed_store_stage_ms.clone(),
            pressure_state,
            huge_range_eligible,
            huge_range_suppressed_reason,
            archive_target_batch_bytes,
            live_latency_guard_state: if live_pressure_cooldown_remaining_ms.is_some() {
                "pressure"
            } else {
                "healthy"
            },
            last_live_latency_p95_ms: state.last_live_latency_p95_ms,
            last_live_enqueue_to_job_p95_ms: state.last_live_enqueue_to_job_p95_ms,
            live_pressure_cooldown_remaining_ms,
            last_direction: state.last_direction.as_str(),
            total_observations: state.total_observations,
            total_increases: state.total_increases,
            total_decreases: state.total_decreases,
            total_backpressure: state.total_backpressure,
            last_backpressure_retry_after_ms: state.last_backpressure_retry_after_ms,
            backpressure_cooldown_remaining_ms: state.backpressure_cooldown_until.and_then(
                |until| {
                    until
                        .checked_duration_since(now)
                        .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
                },
            ),
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
    estimated_bytes: Option<u64>,
}

#[derive(Clone, Debug)]
struct InFlightJob {
    provider: &'static str,
    priority: WorkPriority,
    observation: ObservationTrace,
    estimated_bytes: Option<u64>,
    rerun_priority: Option<WorkPriority>,
    rerun_observation: Option<ObservationTrace>,
    rerun_estimated_bytes: Option<u64>,
}

/// FIFO within each provider, round-robin across whichever providers currently
/// have work. Discovery already supplies each provider's paths newest-first;
/// this queue preserves that order without letting a large provider hide a
/// smaller one behind thousands of files.
#[derive(Default)]
struct ProviderFairQueue {
    queues: BTreeMap<&'static str, VecDeque<PathBuf>>,
    rotation: VecDeque<&'static str>,
}

impl ProviderFairQueue {
    fn push_back(&mut self, provider: &'static str, path: PathBuf) {
        let queue = self.queues.entry(provider).or_default();
        if queue.is_empty() {
            self.rotation.push_back(provider);
        }
        queue.push_back(path);
    }

    fn pop_front(&mut self) -> Option<PathBuf> {
        while let Some(provider) = self.rotation.pop_front() {
            let (path, still_active) = match self.queues.get_mut(provider) {
                Some(queue) => {
                    let path = queue.pop_front();
                    (path, !queue.is_empty())
                }
                None => (None, false),
            };

            if still_active {
                self.rotation.push_back(provider);
            } else {
                self.queues.remove(provider);
            }

            if path.is_some() {
                return path;
            }
        }
        None
    }

    /// Put a continuation ahead of later work from the same provider while
    /// leaving that provider's global rotation position unchanged.
    fn prioritize_within_provider(&mut self, provider: &'static str, path: &Path) {
        let queue = self.queues.entry(provider).or_default();
        queue.retain(|candidate| candidate != path);
        queue.push_front(path.to_path_buf());
        if !self.rotation.contains(&provider) {
            self.rotation.push_back(provider);
        }
    }

    fn remove(&mut self, provider: &'static str, path: &Path) {
        let emptied = match self.queues.get_mut(provider) {
            Some(queue) => {
                queue.retain(|candidate| candidate != path);
                queue.is_empty()
            }
            None => false,
        };
        if emptied {
            self.queues.remove(provider);
            self.rotation.retain(|candidate| candidate != &provider);
        }
    }
}

/// Bounded scheduler that dedupes work by file path.
pub struct PathScheduler {
    max_in_flight: usize,
    fairness_cursor: usize,
    ready_live: VecDeque<PathBuf>,
    ready_retry: ProviderFairQueue,
    ready_scan: ProviderFairQueue,
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
            ready_retry: ProviderFairQueue::default(),
            ready_scan: ProviderFairQueue::default(),
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
        self.enqueue_observed_with_estimated_bytes(
            path,
            provider,
            priority,
            observation_source,
            observed_at_ms,
            None,
        );
    }

    pub fn enqueue_observed_with_estimated_bytes(
        &mut self,
        path: PathBuf,
        provider: &'static str,
        priority: WorkPriority,
        observation_source: &'static str,
        observed_at_ms: i64,
        estimated_bytes: Option<u64>,
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
        self.enqueue_observation_with_estimated_bytes(
            path,
            provider,
            priority,
            observation,
            estimated_bytes,
        );
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
        observation: ObservationTrace,
    ) {
        self.enqueue_observation_with_estimated_bytes(path, provider, priority, observation, None);
    }

    pub fn enqueue_observation_with_estimated_bytes(
        &mut self,
        path: PathBuf,
        provider: &'static str,
        priority: WorkPriority,
        mut observation: ObservationTrace,
        estimated_bytes: Option<u64>,
    ) {
        observation.enqueued_at_ms = now_ms();
        let urgent_managed_wake =
            priority == WorkPriority::Live && observation.source == "wake_socket";
        if let Some(ready) = self.ready_jobs.get_mut(&path) {
            let mut reposition_from = None;
            let mut reposition_same_lane = false;
            if priority < ready.priority {
                reposition_from = Some((ready.provider, ready.priority));
                ready.priority = priority;
                ready.observation = observation;
                ready.estimated_bytes = estimated_bytes;
            } else if priority == ready.priority
                && should_replace_observation(&ready.observation, &observation)
            {
                ready.observation = observation;
                if estimated_bytes.is_some() {
                    ready.estimated_bytes = estimated_bytes;
                }
                reposition_same_lane = urgent_managed_wake;
            } else if estimated_bytes.is_some() {
                ready.estimated_bytes = estimated_bytes;
            }
            if let Some((provider, previous_priority)) = reposition_from {
                self.remove_ready_path(provider, &path, previous_priority);
                self.push_ready_path(path, priority, urgent_managed_wake);
            } else if reposition_same_lane {
                self.push_ready_path(path, priority, urgent_managed_wake);
            }
            return;
        }

        if let Some(in_flight) = self.in_flight.get_mut(&path) {
            in_flight.rerun_priority = merge_priority(in_flight.rerun_priority, Some(priority));
            if estimated_bytes.is_some() {
                in_flight.rerun_estimated_bytes = estimated_bytes;
            }
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
                estimated_bytes,
            },
        );
        self.push_ready_path(path, priority, urgent_managed_wake);
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
            let prioritize_opencode_scan_continuation = priority == WorkPriority::Scan
                && task_rerun == Some(WorkPriority::Scan)
                && in_flight.provider == "opencode";
            let observation = in_flight.rerun_observation.unwrap_or(in_flight.observation);
            let estimated_bytes = in_flight
                .rerun_estimated_bytes
                .or(in_flight.estimated_bytes);
            self.enqueue_observation_with_estimated_bytes(
                path.to_path_buf(),
                in_flight.provider,
                priority,
                observation,
                estimated_bytes,
            );
            if prioritize_opencode_scan_continuation {
                self.ready_scan
                    .prioritize_within_provider(in_flight.provider, path);
            }
        }
    }

    pub fn has_in_flight(&self) -> bool {
        !self.in_flight.is_empty()
    }

    pub fn has_pending_work(&self) -> bool {
        !self.ready_jobs.is_empty() || !self.in_flight.is_empty()
    }

    pub fn has_pending_priority(&self, priority: WorkPriority) -> bool {
        self.ready_jobs.values().any(|job| job.priority == priority)
            || self
                .in_flight
                .values()
                .any(|job| job.priority == priority || job.rerun_priority == Some(priority))
    }

    pub fn snapshot(&self) -> SchedulerSnapshot {
        let ready_live = self.ready_count(WorkPriority::Live);
        let ready_retry = self.ready_count(WorkPriority::Retry);
        let ready_scan = self.ready_count(WorkPriority::Scan);
        let in_flight_live = self.in_flight_count(WorkPriority::Live);
        let in_flight_retry = self.in_flight_count(WorkPriority::Retry);
        let in_flight_scan = self.in_flight_count(WorkPriority::Scan);
        let ready_retry_bytes = self.ready_bytes(WorkPriority::Retry);
        let ready_scan_bytes = self.ready_bytes(WorkPriority::Scan);
        let in_flight_retry_bytes = self.in_flight_bytes(WorkPriority::Retry);
        let in_flight_scan_bytes = self.in_flight_bytes(WorkPriority::Scan);
        SchedulerSnapshot {
            max_in_flight: self.max_in_flight,
            live_reserved: LIVE_RESERVED,
            live_in_flight_cap: LIVE_IN_FLIGHT_CAP,
            backlog_cap: self.backlog_cap(),
            ready_live,
            ready_retry,
            ready_scan,
            in_flight_live,
            in_flight_retry,
            in_flight_scan,
            ready_backlog: ready_retry + ready_scan,
            in_flight_backlog: in_flight_retry + in_flight_scan,
            ready_retry_bytes,
            ready_scan_bytes,
            in_flight_retry_bytes,
            in_flight_scan_bytes,
            ready_backlog_bytes: ready_retry_bytes.saturating_add(ready_scan_bytes),
            in_flight_backlog_bytes: in_flight_retry_bytes.saturating_add(in_flight_scan_bytes),
        }
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
        loop {
            let path = match expected_priority {
                WorkPriority::Live => self.ready_live.pop_front(),
                WorkPriority::Retry => self.ready_retry.pop_front(),
                WorkPriority::Scan => self.ready_scan.pop_front(),
            }?;
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
                    estimated_bytes: ready.estimated_bytes,
                    rerun_priority: None,
                    rerun_observation: None,
                    rerun_estimated_bytes: None,
                },
            );

            return Some(PathJob {
                path,
                provider: ready.provider,
                priority: ready.priority,
                observation: ready.observation,
            });
        }
    }

    fn push_ready_path(
        &mut self,
        path: PathBuf,
        priority: WorkPriority,
        urgent_managed_wake: bool,
    ) {
        match priority {
            WorkPriority::Live if urgent_managed_wake => {
                self.ready_live.retain(|candidate| candidate != &path);
                self.ready_live.push_front(path);
            }
            WorkPriority::Live => self.ready_live.push_back(path),
            WorkPriority::Retry => {
                let provider = self.ready_jobs[&path].provider;
                self.ready_retry.push_back(provider, path);
            }
            WorkPriority::Scan => {
                let provider = self.ready_jobs[&path].provider;
                self.ready_scan.push_back(provider, path);
            }
        }
    }

    fn remove_ready_path(&mut self, provider: &'static str, path: &Path, priority: WorkPriority) {
        match priority {
            WorkPriority::Live => self.ready_live.retain(|candidate| candidate != path),
            WorkPriority::Retry => self.ready_retry.remove(provider, path),
            WorkPriority::Scan => self.ready_scan.remove(provider, path),
        }
    }

    fn can_launch_priority(&self, priority: WorkPriority) -> bool {
        let backlog_has_room = self.in_flight_backlog_count() < self.backlog_cap();
        match priority {
            WorkPriority::Live => self.in_flight_count(WorkPriority::Live) < LIVE_IN_FLIGHT_CAP,
            WorkPriority::Retry => backlog_has_room,
            // Scan shares the adaptive backlog budget with Retry and keeps a
            // one-job subcap so reconciliation cannot crowd out spool replay.
            WorkPriority::Scan => {
                backlog_has_room && self.in_flight_count(WorkPriority::Scan) < SCAN_IN_FLIGHT_CAP
            }
        }
    }

    fn backlog_cap(&self) -> usize {
        // `LIVE_RESERVED` is a hard floor for live-latency safety. The
        // adaptive limiter controls Retry+Scan as one background workload.
        self.max_in_flight
            .saturating_sub(LIVE_RESERVED)
            .max(1)
            .min(self.limiter.current_cap())
    }

    fn in_flight_backlog_count(&self) -> usize {
        self.in_flight_count(WorkPriority::Retry) + self.in_flight_count(WorkPriority::Scan)
    }

    fn in_flight_count(&self, priority: WorkPriority) -> usize {
        self.in_flight
            .values()
            .filter(|job| job.priority == priority)
            .count()
    }

    fn ready_bytes(&self, priority: WorkPriority) -> u64 {
        self.ready_jobs
            .values()
            .filter(|job| job.priority == priority)
            .filter_map(|job| job.estimated_bytes)
            .fold(0u64, u64::saturating_add)
    }

    fn in_flight_bytes(&self, priority: WorkPriority) -> u64 {
        self.in_flight
            .values()
            .filter(|job| job.priority == priority)
            .filter_map(|job| job.estimated_bytes)
            .fold(0u64, u64::saturating_add)
    }

    fn ready_count(&self, priority: WorkPriority) -> usize {
        self.ready_jobs
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
    fn test_retry_and_reconciliation_share_the_adaptive_backlog_cap() {
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
        assert_eq!(first.priority, WorkPriority::Retry);
        assert!(
            scheduler.pop_launchable().is_none(),
            "the floor cap of one must bound Retry+Scan together"
        );

        scheduler.complete(&first.path, None);
        let second = scheduler.pop_launchable().unwrap();
        assert_eq!(second.priority, WorkPriority::Scan);
        assert_eq!(scheduler.snapshot().in_flight_backlog, 1);
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
    fn test_scheduler_snapshot_counts_ready_and_in_flight_by_lane() {
        let limiter = AdaptiveLimiter::new();
        limiter.observe(0.0);
        limiter.observe(0.0);
        limiter.observe(0.0);
        limiter.observe(0.0);
        limiter.clear_adjust_cooldown();
        limiter.observe(0.0);
        let mut scheduler = PathScheduler::with_limiter(32, limiter);

        scheduler.enqueue(
            PathBuf::from("/tmp/live-a.jsonl"),
            "codex",
            WorkPriority::Live,
        );
        scheduler.enqueue_observed_with_estimated_bytes(
            PathBuf::from("/tmp/retry-a.jsonl"),
            "codex",
            WorkPriority::Retry,
            "spool_pending",
            now_ms(),
            Some(1_000),
        );
        scheduler.enqueue_observed_with_estimated_bytes(
            PathBuf::from("/tmp/retry-b.jsonl"),
            "codex",
            WorkPriority::Retry,
            "spool_pending",
            now_ms(),
            Some(2_000),
        );
        scheduler.enqueue_observed_with_estimated_bytes(
            PathBuf::from("/tmp/scan-a.jsonl"),
            "codex",
            WorkPriority::Scan,
            "reconciliation_scan",
            now_ms(),
            Some(4_000),
        );

        let live = scheduler.pop_launchable().unwrap();
        assert_eq!(live.priority, WorkPriority::Live);
        let retry = scheduler.pop_launchable().unwrap();
        assert_eq!(retry.priority, WorkPriority::Retry);

        let snapshot = scheduler.snapshot();
        assert_eq!(snapshot.ready_live, 0);
        assert_eq!(snapshot.ready_retry, 1);
        assert_eq!(snapshot.ready_scan, 1);
        assert_eq!(snapshot.in_flight_live, 1);
        assert_eq!(snapshot.in_flight_retry, 1);
        assert_eq!(snapshot.in_flight_scan, 0);
        assert_eq!(snapshot.ready_backlog, 2);
        assert_eq!(snapshot.in_flight_backlog, 1);
        assert_eq!(snapshot.ready_retry_bytes, 2_000);
        assert_eq!(snapshot.ready_scan_bytes, 4_000);
        assert_eq!(snapshot.in_flight_retry_bytes, 1_000);
        assert_eq!(snapshot.in_flight_scan_bytes, 0);
        assert_eq!(snapshot.ready_backlog_bytes, 6_000);
        assert_eq!(snapshot.in_flight_backlog_bytes, 1_000);
        assert!(snapshot.backlog_cap >= 2);
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
    fn test_managed_wake_jumps_a_generic_live_backlog() {
        let mut scheduler = PathScheduler::new(2);
        for index in 0..100 {
            scheduler.enqueue_observed(
                PathBuf::from(format!("/tmp/backlog-{index}.db")),
                "cursor",
                WorkPriority::Live,
                "fsevent",
                index,
            );
        }
        let managed = PathBuf::from("/tmp/managed.db");
        scheduler.enqueue_observation(
            managed.clone(),
            "cursor",
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
                file_len_hint: Some(4096),
            },
        );

        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.path, managed);
        assert_eq!(job.observation.source, "wake_socket");
    }

    #[test]
    fn test_managed_wake_repositions_an_already_queued_live_path() {
        let mut scheduler = PathScheduler::new(2);
        let managed = PathBuf::from("/tmp/managed.db");
        scheduler.enqueue_observed(
            managed.clone(),
            "cursor",
            WorkPriority::Live,
            "fsevent",
            100,
        );
        scheduler.enqueue_observed(
            PathBuf::from("/tmp/other.db"),
            "cursor",
            WorkPriority::Live,
            "fsevent",
            101,
        );
        scheduler.enqueue_observation(
            managed.clone(),
            "cursor",
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
                file_len_hint: Some(4096),
            },
        );

        assert_eq!(scheduler.pop_launchable().unwrap().path, managed);
        assert_eq!(scheduler.ready_len(), 1);
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

        scheduler.enqueue(path.clone(), "antigravity", WorkPriority::Retry);
        let _ = scheduler.pop_launchable().unwrap();

        scheduler.enqueue(path.clone(), "antigravity", WorkPriority::Scan);
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
    fn test_pending_priority_includes_inflight_reruns() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/retry-then-scan.jsonl");
        scheduler.enqueue(path.clone(), "codex", WorkPriority::Retry);
        let _job = scheduler.pop_launchable().unwrap();
        scheduler.enqueue(path, "codex", WorkPriority::Scan);

        assert!(scheduler.has_pending_priority(WorkPriority::Retry));
        assert!(scheduler.has_pending_priority(WorkPriority::Scan));
        assert!(!scheduler.has_pending_priority(WorkPriority::Live));
    }

    #[test]
    fn test_opencode_scan_continuation_yields_across_providers_but_stays_local_first() {
        let mut scheduler = PathScheduler::new(4);
        let opencode = PathBuf::from("/tmp/opencode.db");
        let later_opencode = PathBuf::from("/tmp/opencode-later.db");
        let codex = PathBuf::from("/tmp/codex.jsonl");
        scheduler.enqueue(opencode.clone(), "opencode", WorkPriority::Scan);
        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, opencode);

        scheduler.enqueue(codex.clone(), "codex", WorkPriority::Scan);
        scheduler.enqueue(later_opencode.clone(), "opencode", WorkPriority::Scan);
        scheduler.complete(&opencode, Some(WorkPriority::Scan));

        let other_provider = scheduler.pop_launchable().unwrap();
        assert_eq!(other_provider.path, codex);
        scheduler.complete(&other_provider.path, None);

        let continuation = scheduler.pop_launchable().unwrap();
        assert_eq!(continuation.path, opencode);
        assert_eq!(continuation.priority, WorkPriority::Scan);
        scheduler.complete(&continuation.path, None);
        assert_eq!(scheduler.pop_launchable().unwrap().path, later_opencode);
    }

    #[test]
    fn test_scan_round_robins_providers_and_preserves_provider_order() {
        let mut scheduler = PathScheduler::new(4);
        let codex_new = PathBuf::from("/tmp/codex-new.jsonl");
        let codex_old = PathBuf::from("/tmp/codex-old.jsonl");
        let claude_new = PathBuf::from("/tmp/claude-new.jsonl");
        let claude_old = PathBuf::from("/tmp/claude-old.jsonl");

        for (path, provider) in [
            (codex_new.clone(), "codex"),
            (codex_old.clone(), "codex"),
            (claude_new.clone(), "claude"),
            (claude_old.clone(), "claude"),
        ] {
            scheduler.enqueue(path, provider, WorkPriority::Scan);
        }

        for expected in [codex_new, claude_new, codex_old, claude_old] {
            let job = scheduler.pop_launchable().unwrap();
            assert_eq!(job.path, expected);
            scheduler.complete(&job.path, None);
        }
    }

    #[test]
    fn test_retry_round_robins_dynamically_discovered_providers() {
        let mut scheduler = PathScheduler::new(4);
        let codex_a = PathBuf::from("/tmp/codex-a.jsonl");
        let codex_b = PathBuf::from("/tmp/codex-b.jsonl");
        let future_provider = PathBuf::from("/tmp/future-provider.jsonl");

        scheduler.enqueue(codex_a.clone(), "codex", WorkPriority::Retry);
        scheduler.enqueue(codex_b.clone(), "codex", WorkPriority::Retry);
        scheduler.enqueue(
            future_provider.clone(),
            "provider-added-later",
            WorkPriority::Retry,
        );

        for expected in [codex_a, future_provider, codex_b] {
            let job = scheduler.pop_launchable().unwrap();
            assert_eq!(job.path, expected);
            scheduler.complete(&job.path, None);
        }
    }

    #[test]
    fn test_priority_promotion_cannot_resurrect_a_stale_queue_position() {
        let mut scheduler = PathScheduler::new(4);
        let newest = PathBuf::from("/tmp/newest.jsonl");
        let middle = PathBuf::from("/tmp/middle.jsonl");
        let oldest = PathBuf::from("/tmp/oldest.jsonl");

        for path in [&newest, &middle, &oldest] {
            scheduler.enqueue(path.clone(), "codex", WorkPriority::Scan);
        }
        scheduler.enqueue(newest.clone(), "codex", WorkPriority::Retry);

        let promoted = scheduler.pop_launchable().unwrap();
        assert_eq!(promoted.path, newest);
        assert_eq!(promoted.priority, WorkPriority::Retry);
        scheduler.complete(&promoted.path, Some(WorkPriority::Scan));

        for expected in [middle, oldest, newest] {
            let job = scheduler.pop_launchable().unwrap();
            assert_eq!(job.path, expected);
            scheduler.complete(&job.path, None);
        }
    }

    #[test]
    fn test_snapshot_backlog_cap_is_the_actual_combined_launch_limit() {
        let limiter = AdaptiveLimiter::new();
        limiter.current_cap.store(16, Ordering::Relaxed);
        let mut scheduler = PathScheduler::with_limiter(10, limiter);

        scheduler.enqueue(
            PathBuf::from("/tmp/retry-a.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/retry-b.jsonl"),
            "claude",
            WorkPriority::Retry,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/scan.jsonl"),
            "cursor",
            WorkPriority::Scan,
        );

        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Retry
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );
        assert!(scheduler.pop_launchable().is_none());

        let snapshot = scheduler.snapshot();
        assert_eq!(snapshot.backlog_cap, 2);
        assert_eq!(snapshot.in_flight_backlog, snapshot.backlog_cap);
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

        // Replay and reconciliation share the one-slot cold-start backlog cap.
        let retry_job = scheduler.pop_launchable().unwrap();
        assert_eq!(retry_job.priority, WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());
        scheduler.complete(&retry_job.path, None);
        let scan_job = scheduler.pop_launchable().unwrap();
        assert_eq!(scan_job.priority, WorkPriority::Scan);
        assert!(scheduler.pop_launchable().is_none());
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
        scheduler.complete(Path::new("/tmp/retry.jsonl"), None);
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );
        assert!(scheduler.pop_launchable().is_none());
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

    #[test]
    fn adaptive_limiter_cuts_cap_on_backpressure_and_records_cooldown() {
        let limiter = AdaptiveLimiter::new();
        for _ in 0..6 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        let pre_backpressure = limiter.current_cap();
        assert!(pre_backpressure > BACKLOG_CAP_FLOOR);

        limiter.observe_backpressure(Some(Duration::from_secs(5)));

        let post_backpressure = limiter.current_cap();
        assert!(post_backpressure < pre_backpressure);
        let snap = limiter.snapshot();
        assert_eq!(snap.total_backpressure, 1);
        assert_eq!(snap.last_backpressure_retry_after_ms, Some(5_000));
        assert!(snap.backpressure_cooldown_remaining_ms.is_some());
        assert_eq!(snap.last_direction, "decreased");
        assert!(!snap.huge_range_eligible);
        assert_eq!(snap.pressure_state, "backpressure_cooldown");
        assert_eq!(
            snap.huge_range_suppressed_reason,
            Some("backpressure_cooldown")
        );
    }

    #[test]
    fn adaptive_limiter_cuts_archive_cap_on_live_latency_pressure() {
        let limiter = AdaptiveLimiter::new();
        for _ in 0..6 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        let pre_pressure = limiter.current_cap();
        assert!(pre_pressure > BACKLOG_CAP_FLOOR);

        limiter.observe_live_latency(Some(LIVE_LATENCY_WARN_MS), Some(100));

        let post_pressure = limiter.current_cap();
        assert!(
            post_pressure < pre_pressure,
            "live p95 pressure should reduce archive cap: {pre_pressure} -> {post_pressure}"
        );
        let snap = limiter.snapshot();
        assert_eq!(snap.live_latency_guard_state, "pressure");
        assert_eq!(snap.last_live_latency_p95_ms, Some(LIVE_LATENCY_WARN_MS));
        assert_eq!(snap.last_live_enqueue_to_job_p95_ms, Some(100));
        assert!(snap.live_pressure_cooldown_remaining_ms.is_some());
        assert_eq!(snap.pressure_state, "live_latency_pressure");
        assert_eq!(
            snap.huge_range_suppressed_reason,
            Some("live_latency_pressure")
        );
        assert_eq!(
            snap.archive_target_batch_bytes,
            ARCHIVE_BATCH_TARGET_MIN_BYTES
        );
    }

    #[test]
    fn adaptive_limiter_floors_archive_cap_on_critical_live_pressure() {
        let limiter = AdaptiveLimiter::new();
        for _ in 0..6 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(10.0);
            }
            limiter.clear_adjust_cooldown();
        }
        assert!(limiter.current_cap() > BACKLOG_CAP_FLOOR);

        limiter.observe_live_latency(Some(500), Some(LIVE_ENQUEUE_CRITICAL_MS));

        let snap = limiter.snapshot();
        assert_eq!(limiter.current_cap(), BACKLOG_CAP_FLOOR);
        assert_eq!(snap.live_latency_guard_state, "pressure");
        assert_eq!(snap.last_live_latency_p95_ms, Some(500));
        assert_eq!(
            snap.last_live_enqueue_to_job_p95_ms,
            Some(LIVE_ENQUEUE_CRITICAL_MS)
        );
        assert!(!snap.huge_range_eligible);
        assert_eq!(snap.pressure_state, "live_latency_pressure");
    }

    #[test]
    fn adaptive_limiter_holds_increase_during_backpressure_cooldown() {
        let limiter = AdaptiveLimiter::new();
        limiter.observe_backpressure(Some(Duration::from_secs(5)));
        let cooled_cap = limiter.current_cap();

        for _ in 0..16 {
            for _ in 0..DAMP_MIN_SAMPLES {
                limiter.observe(1.0);
            }
            limiter.clear_adjust_cooldown();
        }

        assert_eq!(
            limiter.current_cap(),
            cooled_cap,
            "low queue waits must not ramp archive while host retry-after cooldown is active"
        );
        let snap = limiter.snapshot();
        assert_eq!(snap.total_backpressure, 1);
        assert!(snap.backpressure_cooldown_remaining_ms.is_some());
        assert_eq!(snap.last_direction, "held");
    }

    #[test]
    fn adaptive_limiter_probes_huge_ranges_after_pressure_cooldown() {
        let limiter = AdaptiveLimiter::new();
        assert!(limiter.huge_range_eligible());

        for _ in 0..DAMP_MIN_SAMPLES {
            limiter.observe(1_000.0);
        }

        let snap = limiter.snapshot();
        assert!(limiter.huge_range_eligible());
        assert!(snap.huge_range_eligible);
        assert_eq!(snap.pressure_state, "host_queue_pressure");
        assert_eq!(snap.huge_range_suppressed_reason, None);
        assert_eq!(
            limiter.archive_target_batch_bytes(),
            ARCHIVE_BATCH_TARGET_MIN_BYTES
        );
    }

    #[test]
    fn adaptive_limiter_allows_huge_ranges_when_pressure_is_below_target() {
        let limiter = AdaptiveLimiter::new();
        for _ in 0..DAMP_MIN_SAMPLES {
            limiter.observe(10.0);
        }

        let snap = limiter.snapshot();
        assert!(limiter.huge_range_eligible());
        assert!(snap.huge_range_eligible);
        assert_eq!(snap.pressure_state, "normal");
        assert_eq!(snap.huge_range_suppressed_reason, None);
    }

    #[test]
    fn adaptive_limiter_tunes_archive_batch_target_from_host_pressure() {
        let limiter = AdaptiveLimiter::new();
        assert_eq!(
            limiter.archive_target_batch_bytes(),
            ARCHIVE_BATCH_TARGET_BASE_BYTES
        );

        for _ in 0..DAMP_MIN_SAMPLES {
            limiter.observe_ingest_timing(10.0, Some(50.0), None, None, None, None);
        }
        assert_eq!(
            limiter.archive_target_batch_bytes(),
            ARCHIVE_BATCH_TARGET_MAX_BYTES
        );

        for _ in 0..DAMP_MIN_SAMPLES {
            limiter.observe_ingest_timing(2_000.0, Some(50.0), None, None, None, None);
        }
        assert_eq!(
            limiter.archive_target_batch_bytes(),
            ARCHIVE_BATCH_TARGET_MIN_BYTES
        );

        limiter.observe_backpressure(Some(Duration::from_secs(5)));
        assert_eq!(
            limiter.snapshot().archive_target_batch_bytes,
            ARCHIVE_BATCH_TARGET_MIN_BYTES
        );
    }

    #[test]
    fn adaptive_limiter_records_host_exec_timing_without_driving_cap() {
        let limiter = AdaptiveLimiter::new();
        let stages = BTreeMap::from([
            ("provider_event_observations".to_string(), 40.0),
            ("total".to_string(), 120.0),
        ]);
        limiter.observe_ingest_timing(
            10.0,
            Some(500.0),
            Some(3),
            Some(450.0),
            Some(100),
            Some(stages),
        );
        limiter.observe_ingest_timing(10.0, Some(100.0), Some(1), Some(80.0), Some(100), None);

        let snap = limiter.snapshot();
        assert_eq!(snap.last_observed_queue_wait_ms, Some(10.0));
        assert_eq!(snap.last_observed_exec_ms, Some(100.0));
        assert_eq!(snap.last_observed_commit_count, Some(1));
        assert_eq!(snap.last_observed_commit_ms, Some(80.0));
        assert_eq!(snap.last_observed_chunk_size, Some(100));
        assert!(snap
            .last_observed_store_stage_ms
            .as_ref()
            .is_some_and(|stages| stages.get("total") == Some(&120.0)));
        assert!(snap.ewma_exec_ms.is_some());
        assert!(snap.ewma_commit_ms.is_some());
        assert_eq!(snap.current_cap, BACKLOG_CAP_FLOOR);
        assert!(snap.huge_range_eligible);
    }
}
