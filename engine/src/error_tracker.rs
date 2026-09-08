//! Rate-limited error logging to prevent log spam during outages.
//!
//! Logs warn on the first failure and every 100th after that.
//! Emits info on first success after a run of failures.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Shared error tracker — cheap to clone, backed by atomics.
#[derive(Clone)]
pub struct ErrorTracker {
    inner: Arc<ErrorTrackerInner>,
}

struct ErrorTrackerInner {
    /// Errors since the last successful operation, used for rate limiting and
    /// the recovery log message.
    total_since_reset: AtomicU32,
}

impl ErrorTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(ErrorTrackerInner {
                total_since_reset: AtomicU32::new(0),
            }),
        }
    }

    /// Call on each error. Returns true if this error should be logged (warn).
    ///
    /// Logs the 1st failure and every 100th after that.
    pub fn record_error(&self) -> bool {
        let n = self.inner.total_since_reset.fetch_add(1, Ordering::Relaxed);
        n == 0 || (n + 1) % 100 == 0
    }

    /// Call on success. Returns Some(count) if recovering from errors.
    pub fn record_success(&self) -> Option<u32> {
        let total = self.inner.total_since_reset.swap(0, Ordering::Relaxed);
        (total > 0).then_some(total)
    }
}
impl Default for ErrorTracker {
    fn default() -> Self {
        Self::new()
    }
}

const RECENT_ISSUE_WINDOW: Duration = Duration::from_secs(60 * 60);

/// Rolling counter for local process issues such as parse anomalies.
#[derive(Clone)]
pub struct RecentIssueTracker {
    inner: Arc<Mutex<VecDeque<Instant>>>,
}

impl RecentIssueTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::new())),
        }
    }

    pub fn record(&self) {
        self.record_at(Instant::now());
    }

    pub fn count_last_hour(&self) -> u32 {
        let now = Instant::now();
        if let Ok(mut guard) = self.inner.lock() {
            prune_recent_issues(&mut guard, now);
            guard.len() as u32
        } else {
            0
        }
    }

    fn record_at(&self, instant: Instant) {
        if let Ok(mut guard) = self.inner.lock() {
            guard.push_back(instant);
            prune_recent_issues(&mut guard, instant);
        }
    }
}

impl Default for RecentIssueTracker {
    fn default() -> Self {
        Self::new()
    }
}

fn prune_recent_issues(entries: &mut VecDeque<Instant>, now: Instant) {
    while let Some(front) = entries.front() {
        if now.saturating_duration_since(*front) > RECENT_ISSUE_WINDOW {
            entries.pop_front();
        } else {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_error_tracker_rate_limits() {
        let tracker = ErrorTracker::new();

        // First error should log
        assert!(tracker.record_error(), "1st error should log");

        // 2nd through 99th should NOT log
        for i in 1..99 {
            let should_log = tracker.record_error();
            assert!(!should_log, "error {} should be suppressed", i + 1);
        }

        // 100th should log again
        assert!(tracker.record_error(), "100th error should log");

        // 101-199 suppressed
        for i in 100..199 {
            let should_log = tracker.record_error();
            assert!(!should_log, "error {} should be suppressed", i + 1);
        }

        // 200th should log
        assert!(tracker.record_error(), "200th error should log");
    }

    #[test]
    fn test_error_tracker_recovery() {
        let tracker = ErrorTracker::new();

        tracker.record_error();
        tracker.record_error();
        tracker.record_error();

        // Success should return the count
        let recovered = tracker.record_success();
        assert_eq!(recovered, Some(3));

        // Subsequent success returns None
        let no_recovery = tracker.record_success();
        assert_eq!(no_recovery, None);
    }

    #[test]
    fn test_error_tracker_no_false_recovery() {
        let tracker = ErrorTracker::new();

        // Success with no prior errors → None
        assert_eq!(tracker.record_success(), None);
    }

    #[test]
    fn test_recent_issue_tracker_counts_recent_records() {
        let tracker = RecentIssueTracker::new();
        tracker.record();
        tracker.record();

        assert_eq!(tracker.count_last_hour(), 2);
    }

    #[test]
    fn test_recent_issue_tracker_prunes_old_records() {
        let tracker = RecentIssueTracker::new();
        let now = Instant::now();

        tracker.record_at(now - Duration::from_secs(RECENT_ISSUE_WINDOW.as_secs() + 60));
        tracker.record_at(now - Duration::from_secs(RECENT_ISSUE_WINDOW.as_secs() - 60));

        assert_eq!(tracker.count_last_hour(), 1);
    }
}
