//! Path-keyed scheduler for daemon shipping work.
//!
//! Ensures at most one in-flight task per session file path while allowing
//! bounded concurrency across unrelated files. Ready work is weighted so live
//! watcher events get more slots without starving retry/scan work.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};

/// Ready-work priority, ordered from highest urgency to lowest.
#[derive(Copy, Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum WorkPriority {
    Watch,
    Catchup,
    Retry,
    Scan,
}

const FAIR_SEQUENCE: [WorkPriority; 5] = [
    WorkPriority::Watch,
    WorkPriority::Watch,
    WorkPriority::Catchup,
    WorkPriority::Retry,
    WorkPriority::Scan,
];
const URGENT_SEQUENCE: [WorkPriority; 2] = [WorkPriority::Watch, WorkPriority::Catchup];
const URGENT_OVERFLOW_CAP: usize = 2;
const RETRY_IN_FLIGHT_CAP: usize = 1;
const SCAN_IN_FLIGHT_CAP: usize = 1;

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
    pub enqueued_at_ms: i64,
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
    ready_watch: VecDeque<PathBuf>,
    ready_catchup: VecDeque<PathBuf>,
    ready_retry: VecDeque<PathBuf>,
    ready_scan: VecDeque<PathBuf>,
    ready_jobs: HashMap<PathBuf, ReadyJob>,
    in_flight: HashMap<PathBuf, InFlightJob>,
}

impl PathScheduler {
    pub fn new(max_in_flight: usize) -> Self {
        Self {
            max_in_flight: max_in_flight.max(1),
            fairness_cursor: 0,
            ready_watch: VecDeque::new(),
            ready_catchup: VecDeque::new(),
            ready_retry: VecDeque::new(),
            ready_scan: VecDeque::new(),
            ready_jobs: HashMap::new(),
            in_flight: HashMap::new(),
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
            enqueued_at_ms: now_ms(),
        };

        if let Some(ready) = self.ready_jobs.get_mut(&path) {
            if priority < ready.priority {
                ready.priority = priority;
                ready.observation = observation;
                self.push_ready_path(path, priority);
            }
            return;
        }

        if let Some(in_flight) = self.in_flight.get_mut(&path) {
            in_flight.rerun_priority = merge_priority(in_flight.rerun_priority, Some(priority));
            in_flight.rerun_observation = Some(observation);
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
        if self.in_flight.len() >= self.max_in_flight {
            return self.pop_urgent_overflow();
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

    fn pop_urgent_overflow(&mut self) -> Option<PathJob> {
        if self.in_flight.len() >= self.max_in_flight + URGENT_OVERFLOW_CAP {
            return None;
        }

        for priority in URGENT_SEQUENCE {
            if !self.can_launch_priority(priority) {
                continue;
            }
            if let Some(job) = self.pop_ready_queue(priority) {
                return Some(job);
            }
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
            self.enqueue_observed(
                path.to_path_buf(),
                in_flight.provider,
                priority,
                observation.source,
                observation.observed_at_ms,
            );
        }
    }

    pub fn has_in_flight(&self) -> bool {
        !self.in_flight.is_empty()
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
            WorkPriority::Watch => &mut self.ready_watch,
            WorkPriority::Catchup => &mut self.ready_catchup,
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
            WorkPriority::Watch => self.ready_watch.push_back(path),
            WorkPriority::Catchup => self.ready_catchup.push_back(path),
            WorkPriority::Retry => self.ready_retry.push_back(path),
            WorkPriority::Scan => self.ready_scan.push_back(path),
        }
    }

    fn can_launch_priority(&self, priority: WorkPriority) -> bool {
        match priority {
            WorkPriority::Watch | WorkPriority::Catchup => true,
            WorkPriority::Retry => self.in_flight_count(WorkPriority::Retry) < RETRY_IN_FLIGHT_CAP,
            WorkPriority::Scan => self.in_flight_count(WorkPriority::Scan) < SCAN_IN_FLIGHT_CAP,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ready_path_promotes_to_higher_priority_without_dup_launch() {
        let mut scheduler = PathScheduler::new(2);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue(path.clone(), "claude", WorkPriority::Scan);
        scheduler.enqueue(path.clone(), "claude", WorkPriority::Watch);

        assert_eq!(scheduler.ready_len(), 1);
        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Watch);
        assert!(scheduler.pop_launchable().is_none());
    }

    #[test]
    fn test_inflight_path_is_rerun_after_completion() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue_observed(path.clone(), "codex", WorkPriority::Scan, "scan", 100);
        let job = scheduler.pop_launchable().unwrap();
        assert_eq!(job.priority, WorkPriority::Scan);
        assert_eq!(scheduler.in_flight_len(), 1);

        scheduler.enqueue_observed(path.clone(), "codex", WorkPriority::Watch, "fsevent", 200);
        assert_eq!(scheduler.ready_len(), 0);

        scheduler.complete(&path, None);
        assert_eq!(scheduler.in_flight_len(), 0);
        assert_eq!(scheduler.ready_len(), 1);

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.priority, WorkPriority::Watch);
        assert_eq!(rerun.observation.source, "fsevent");
        assert_eq!(rerun.observation.observed_at_ms, 200);
    }

    #[test]
    fn test_complete_merges_task_and_external_rerun_priority() {
        let mut scheduler = PathScheduler::new(1);
        let path = PathBuf::from("/tmp/a.jsonl");

        scheduler.enqueue(path.clone(), "gemini", WorkPriority::Retry);
        let _ = scheduler.pop_launchable().unwrap();

        scheduler.enqueue(path.clone(), "gemini", WorkPriority::Scan);
        scheduler.complete(&path, Some(WorkPriority::Watch));

        let rerun = scheduler.pop_launchable().unwrap();
        assert_eq!(rerun.priority, WorkPriority::Watch);
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
    fn test_background_scan_is_capped_even_with_capacity_available() {
        let mut scheduler = PathScheduler::new(4);
        let scan_a = PathBuf::from("/tmp/scan-a.jsonl");
        let scan_b = PathBuf::from("/tmp/scan-b.jsonl");
        let watch = PathBuf::from("/tmp/watch.jsonl");

        scheduler.enqueue(scan_a.clone(), "codex", WorkPriority::Scan);
        scheduler.enqueue(scan_b.clone(), "codex", WorkPriority::Scan);

        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, scan_a);
        assert_eq!(first.priority, WorkPriority::Scan);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(watch.clone(), "codex", WorkPriority::Watch);
        let urgent = scheduler.pop_launchable().unwrap();
        assert_eq!(urgent.path, watch);
        assert_eq!(urgent.priority, WorkPriority::Watch);

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
        let catchup = PathBuf::from("/tmp/catchup.jsonl");

        scheduler.enqueue(retry_a.clone(), "codex", WorkPriority::Retry);
        scheduler.enqueue(retry_b.clone(), "codex", WorkPriority::Retry);

        let first = scheduler.pop_launchable().unwrap();
        assert_eq!(first.path, retry_a);
        assert_eq!(first.priority, WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(catchup.clone(), "codex", WorkPriority::Catchup);
        let urgent = scheduler.pop_launchable().unwrap();
        assert_eq!(urgent.path, catchup);
        assert_eq!(urgent.priority, WorkPriority::Catchup);

        scheduler.complete(&retry_a, None);
        let second_retry = scheduler.pop_launchable().unwrap();
        assert_eq!(second_retry.path, retry_b);
        assert_eq!(second_retry.priority, WorkPriority::Retry);
    }

    #[test]
    fn test_urgent_work_can_overflow_full_scan_pool() {
        let mut scheduler = PathScheduler::new(1);
        let scan = PathBuf::from("/tmp/scan.jsonl");
        let catchup = PathBuf::from("/tmp/catchup.jsonl");
        let retry = PathBuf::from("/tmp/retry.jsonl");

        scheduler.enqueue(scan.clone(), "codex", WorkPriority::Scan);
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );

        scheduler.enqueue(retry, "codex", WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());

        scheduler.enqueue(catchup.clone(), "codex", WorkPriority::Catchup);
        let urgent = scheduler.pop_launchable().unwrap();
        assert_eq!(urgent.path, catchup);
        assert_eq!(urgent.priority, WorkPriority::Catchup);
    }

    #[test]
    fn test_urgent_overflow_is_bounded() {
        let mut scheduler = PathScheduler::new(1);

        scheduler.enqueue(
            PathBuf::from("/tmp/scan.jsonl"),
            "codex",
            WorkPriority::Scan,
        );
        assert!(scheduler.pop_launchable().is_some());

        for idx in 0..3 {
            scheduler.enqueue(
                PathBuf::from(format!("/tmp/catchup-{idx}.jsonl")),
                "codex",
                WorkPriority::Catchup,
            );
        }

        assert!(scheduler.pop_launchable().is_some());
        assert!(scheduler.pop_launchable().is_some());
        assert!(scheduler.pop_launchable().is_none());
    }

    #[test]
    fn test_scheduler_gives_retry_and_scan_turns_under_watch_pressure() {
        let mut scheduler = PathScheduler::new(8);

        scheduler.enqueue(
            PathBuf::from("/tmp/watch-1.jsonl"),
            "claude",
            WorkPriority::Watch,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/watch-2.jsonl"),
            "claude",
            WorkPriority::Watch,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/watch-3.jsonl"),
            "claude",
            WorkPriority::Watch,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/watch-4.jsonl"),
            "claude",
            WorkPriority::Watch,
        );
        scheduler.enqueue(
            PathBuf::from("/tmp/catchup.jsonl"),
            "claude",
            WorkPriority::Catchup,
        );
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

        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Watch
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Watch
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Catchup
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Retry
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Scan
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Watch
        );
        assert_eq!(
            scheduler.pop_launchable().unwrap().priority,
            WorkPriority::Watch
        );
    }
}
