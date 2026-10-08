//! Waits a control command spends on a provider-side handshake (a Claude
//! channel becoming ready, an Antigravity hook claiming a queued message).
//!
//! Production always waits the caller's default. Tests that drive the real
//! dispatcher against a handshake that never arrives shorten the wait on their
//! own thread, so concurrently running tests keep the real timing.

use std::time::Duration;

#[cfg(test)]
thread_local! {
    static TEST_OVERRIDE: std::cell::Cell<Option<Duration>> = const { std::cell::Cell::new(None) };
}

pub(crate) fn control_wait(default: Duration) -> Duration {
    #[cfg(test)]
    if let Some(wait) = TEST_OVERRIDE.with(std::cell::Cell::get) {
        return wait;
    }
    default
}

#[cfg(test)]
pub(crate) fn with_test_control_wait<T>(wait: Duration, body: impl FnOnce() -> T) -> T {
    // Restored on drop, so a panicking body cannot leak the override into
    // whatever runs next on this thread.
    struct Restore(Option<Duration>);
    impl Drop for Restore {
        fn drop(&mut self) {
            TEST_OVERRIDE.with(|cell| cell.set(self.0));
        }
    }
    let _restore = Restore(TEST_OVERRIDE.with(|cell| cell.replace(Some(wait))));
    body()
}
