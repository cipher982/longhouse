//! Filesystem watcher for session files using the `notify` crate.
//!
//! Wraps `notify::recommended_watcher` (FSEvents on macOS, inotify on Linux)
//! with a tokio mpsc channel. Events are coalesced using a HashMap + flush
//! interval (throttle pattern, not debounce) to handle rapid JSONL appends
//! without starving.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::Result;
use notify::event::{CreateKind, DataChange, EventKind, ModifyKind};
use notify::{RecommendedWatcher, RecursiveMode, Watcher};
use tokio::sync::mpsc;

use crate::discovery::ProviderConfig;

/// Bounded channel capacity for file events.
const WATCHER_CHANNEL_CAPACITY: usize = 2048;

/// Valid session file extensions and SQLite sidecar suffixes emitted by providers.
const SESSION_EXTENSIONS: &[&str] = &["jsonl", "json", "db", "db-wal", "db-shm"];

fn has_session_extension(path: &std::path::Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map_or(false, |e| SESSION_EXTENSIONS.contains(&e))
}

/// Temporary/swap file patterns to ignore.
fn is_temp_file(path: &std::path::Path) -> bool {
    let name = match path.file_name().and_then(|n| n.to_str()) {
        Some(n) => n,
        None => return true,
    };
    name.starts_with('.')
        || name.starts_with('~')
        || name.ends_with(".swp")
        || name.ends_with(".tmp")
        || name.ends_with('~')
        || name.contains(".#")
}

/// File watcher that delivers batches of changed session file paths.
pub struct SessionWatcher {
    // Must stay alive — dropping stops the watcher.
    _watcher: RecommendedWatcher,
    rx: mpsc::Receiver<WatcherEvent>,
}

/// A filesystem change after provider/session filtering.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WatcherEvent {
    pub path: PathBuf,
    pub observed_at_ms: i64,
    pub latest_observed_at_ms: i64,
}

impl SessionWatcher {
    /// Start watching all provider directories.
    pub fn new(providers: &[ProviderConfig], managed_state_dirs: &[PathBuf]) -> Result<Self> {
        let (tx, rx) = mpsc::channel(WATCHER_CHANNEL_CAPACITY);
        let dropped_events = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let dropped_clone = dropped_events.clone();

        let watcher_tx = tx.clone();
        let mut watcher = notify::recommended_watcher(
            move |res: notify::Result<notify::Event>| {
                let event = match res {
                    Ok(e) => e,
                    Err(_) => return,
                };

                // Filter to content-change events only
                match event.kind {
                    EventKind::Modify(ModifyKind::Data(
                        DataChange::Content | DataChange::Size | DataChange::Any,
                    ))
                    | EventKind::Modify(ModifyKind::Data(_))
                    | EventKind::Create(CreateKind::File | CreateKind::Any)
                    | EventKind::Modify(ModifyKind::Name(_)) => {}
                    // Accept Any (some backends don't differentiate)
                    EventKind::Modify(ModifyKind::Any) => {}
                    _ => return,
                }

                for path in event.paths {
                    // Filter by extension
                    if !has_session_extension(&path) {
                        tracing::debug!(path = %path.display(), "Skipping watcher event path without session extension");
                        continue;
                    }

                    // Skip temp files
                    if is_temp_file(&path) {
                        tracing::debug!(path = %path.display(), "Skipping temporary watcher event path");
                        continue;
                    }

                    // Bounded send. If the OS watcher floods, reconciliation
                    // scan repairs missed files.
                    let observed_at_ms = chrono::Utc::now().timestamp_millis();
                    let watcher_event = WatcherEvent {
                        path,
                        observed_at_ms,
                        latest_observed_at_ms: observed_at_ms,
                    };
                    if watcher_tx.try_send(watcher_event).is_err() {
                        let n =
                            dropped_clone.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
                        // Warn once per 1000 drops
                        if n % 1000 == 0 {
                            eprintln!(
                                "[engine] WARNING: watcher channel full, {} events dropped (reconciliation scan will repair)",
                                n
                            );
                        }
                    }
                }
            },
        )?;

        // Watch all provider root directories recursively
        for provider in providers {
            if provider.root.exists() {
                watcher.watch(&provider.root, RecursiveMode::Recursive)?;
                tracing::info!(
                    "Watching {} for {} sessions",
                    provider.root.display(),
                    provider.name
                );
            }
        }
        for state_dir in managed_state_dirs {
            if state_dir.exists() && !provider_owns_root(providers, state_dir) {
                watcher.watch(state_dir, RecursiveMode::Recursive)?;
                tracing::info!(
                    path = %state_dir.display(),
                    "Watching managed provider state"
                );
            }
        }

        Ok(Self {
            _watcher: watcher,
            rx,
        })
    }

    /// Await the next filesystem event from the OS watcher thread.
    ///
    /// Returns `None` only if the channel has been closed (watcher dropped),
    /// which signals daemon shutdown.
    pub async fn next_event(&mut self) -> Option<WatcherEvent> {
        self.rx.recv().await
    }

    /// Drain the currently buffered watcher events and coalesce them with
    /// `first`. The caller owns any wait/coalescing policy before this point.
    pub fn collect_ready_batch(&mut self, first: WatcherEvent) -> Vec<WatcherEvent> {
        let mut batch: HashMap<PathBuf, (i64, i64)> = HashMap::new();
        batch.insert(
            first.path,
            (first.observed_at_ms, first.latest_observed_at_ms),
        );

        while let Ok(event) = self.rx.try_recv() {
            batch
                .entry(event.path)
                .and_modify(|(first_observed, latest_observed)| {
                    *first_observed = (*first_observed).min(event.observed_at_ms);
                    *latest_observed = (*latest_observed).max(event.latest_observed_at_ms);
                })
                .or_insert((event.observed_at_ms, event.latest_observed_at_ms));
        }

        coalesced_batch_to_events(batch)
    }
}

fn provider_owns_root(providers: &[ProviderConfig], path: &Path) -> bool {
    providers.iter().any(|provider| path.starts_with(&provider.root))
}

fn coalesced_batch_to_events(batch: HashMap<PathBuf, (i64, i64)>) -> Vec<WatcherEvent> {
    let mut events: Vec<_> = batch
        .into_iter()
        .map(
            |(path, (observed_at_ms, latest_observed_at_ms))| WatcherEvent {
                path,
                observed_at_ms,
                latest_observed_at_ms,
            },
        )
        .collect();
    events.sort_by(|a, b| a.path.cmp(&b.path));
    events
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_bounded_channel_drops_not_panics() {
        // Create a bounded channel directly — fill it, then verify try_send fails gracefully
        let (tx, mut rx) = mpsc::channel::<WatcherEvent>(2);

        // Fill the channel
        tx.try_send(WatcherEvent {
            path: PathBuf::from("/a"),
            observed_at_ms: 1,
            latest_observed_at_ms: 1,
        })
        .unwrap();
        tx.try_send(WatcherEvent {
            path: PathBuf::from("/b"),
            observed_at_ms: 2,
            latest_observed_at_ms: 2,
        })
        .unwrap();

        // Third send should fail (channel full), not panic
        let result = tx.try_send(WatcherEvent {
            path: PathBuf::from("/c"),
            observed_at_ms: 3,
            latest_observed_at_ms: 3,
        });
        assert!(result.is_err(), "Full channel should reject send");

        // Drain to verify first two got through
        assert_eq!(
            rx.try_recv().unwrap(),
            WatcherEvent {
                path: PathBuf::from("/a"),
                observed_at_ms: 1,
                latest_observed_at_ms: 1
            }
        );
        assert_eq!(
            rx.try_recv().unwrap(),
            WatcherEvent {
                path: PathBuf::from("/b"),
                observed_at_ms: 2,
                latest_observed_at_ms: 2
            }
        );
        assert!(
            rx.try_recv().is_err(),
            "Channel should be empty after drain"
        );
    }

    #[test]
    fn test_coalesced_batch_preserves_first_and_latest_observed_times() {
        let mut batch = HashMap::new();
        batch.insert(PathBuf::from("/b.jsonl"), (2, 5));
        batch.insert(PathBuf::from("/a.jsonl"), (1, 9));

        let events = coalesced_batch_to_events(batch);

        assert_eq!(
            events,
            vec![
                WatcherEvent {
                    path: PathBuf::from("/a.jsonl"),
                    observed_at_ms: 1,
                    latest_observed_at_ms: 9,
                },
                WatcherEvent {
                    path: PathBuf::from("/b.jsonl"),
                    observed_at_ms: 2,
                    latest_observed_at_ms: 5,
                },
            ]
        );
    }

    #[test]
    fn test_opencode_sqlite_files_pass_extension_filter() {
        assert!(has_session_extension(std::path::Path::new("opencode.db")));
        assert!(has_session_extension(std::path::Path::new(
            "opencode.db-wal"
        )));
        assert!(has_session_extension(std::path::Path::new(
            "opencode.db-shm"
        )));
    }
}
