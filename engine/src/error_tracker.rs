//! Rate-limited error logging to prevent log spam during outages.
//!
//! Logs warn on the first failure and every 100th after that.
//! Emits info on first success after a run of failures.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// Shared error tracker — cheap to clone, backed by atomics.
#[derive(Clone)]
pub struct ConsecutiveErrorTracker {
    inner: Arc<ErrorTrackerInner>,
}

struct ErrorTrackerInner {
    consecutive: AtomicU32,
    /// Total errors since last reset (for recovery message).
    total_since_reset: AtomicU32,
    /// Timestamp of first error in current run (for recovery message).
    first_error_at: Mutex<Option<Instant>>,
}

impl ConsecutiveErrorTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(ErrorTrackerInner {
                consecutive: AtomicU32::new(0),
                total_since_reset: AtomicU32::new(0),
                first_error_at: Mutex::new(None),
            }),
        }
    }

    /// Call on each error. Returns true if this error should be logged (warn).
    ///
    /// Logs the 1st failure and every 100th after that.
    pub fn record_error(&self) -> bool {
        let n = self.inner.consecutive.fetch_add(1, Ordering::Relaxed);
        self.inner.total_since_reset.fetch_add(1, Ordering::Relaxed);

        // Record first error time
        if n == 0 {
            if let Ok(mut guard) = self.inner.first_error_at.lock() {
                *guard = Some(Instant::now());
            }
        }

        // Log 1st error and every 100th
        n == 0 || (n + 1) % 100 == 0
    }

    /// Call on success. Returns Some(count) if recovering from errors (should emit info).
    pub fn record_success(&self) -> Option<u32> {
        let prev = self.inner.consecutive.swap(0, Ordering::Relaxed);
        if prev > 0 {
            let total = self.inner.total_since_reset.swap(0, Ordering::Relaxed);
            if let Ok(mut guard) = self.inner.first_error_at.lock() {
                *guard = None;
            }
            Some(total)
        } else {
            None
        }
    }

    /// Current consecutive error count.
    pub fn consecutive_count(&self) -> u32 {
        self.inner.consecutive.load(Ordering::Relaxed)
    }
}

impl Default for ConsecutiveErrorTracker {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_error_tracker_rate_limits() {
        let tracker = ConsecutiveErrorTracker::new();

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
        let tracker = ConsecutiveErrorTracker::new();

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
        let tracker = ConsecutiveErrorTracker::new();

        // Success with no prior errors → None
        assert_eq!(tracker.record_success(), None);
    }
}
