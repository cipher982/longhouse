//! Filesystem watcher for session files using the `notify` crate.
//!
//! Wraps `notify::recommended_watcher` (FSEvents on macOS, inotify on Linux)
//! with a tokio mpsc channel. Events are coalesced using a HashSet + flush
//! interval (throttle pattern, not debounce) to handle rapid JSONL appends
//! without starving.

use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use notify::event::{CreateKind, DataChange, EventKind, ModifyKind};
use notify::{RecommendedWatcher, RecursiveMode, Watcher};
use tokio::sync::mpsc;

use crate::discovery::ProviderConfig;

/// Valid session file extensions.
const SESSION_EXTENSIONS: &[&str] = &["jsonl", "json"];

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
    rx: mpsc::UnboundedReceiver<PathBuf>,
}

impl SessionWatcher {
    /// Start watching all provider directories.
    pub fn new(providers: &[ProviderConfig]) -> Result<Self> {
        let (tx, rx) = mpsc::unbounded_channel();

        let watcher_tx = tx.clone();
        let mut watcher = notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
            let event = match res {
                Ok(e) => e,
                Err(_) => return,
            };

            // Filter to content-change events only
            match event.kind {
                EventKind::Modify(ModifyKind::Data(DataChange::Content | DataChange::Size | DataChange::Any))
                | EventKind::Modify(ModifyKind::Data(_))
                | EventKind::Create(CreateKind::File | CreateKind::Any)
                | EventKind::Modify(ModifyKind::Name(_)) => {}
                // Accept Any (some backends don't differentiate)
                EventKind::Modify(ModifyKind::Any) => {}
                _ => return,
            }

            for path in event.paths {
                // Filter by extension
                let ext_ok = path
                    .extension()
                    .and_then(|e| e.to_str())
                    .map_or(false, |e| SESSION_EXTENSIONS.contains(&e));
                if !ext_ok {
                    continue;
                }

                // Skip temp files
                if is_temp_file(&path) {
                    continue;
                }

                // Non-blocking send — if receiver is dropped, we stop silently
                let _ = watcher_tx.send(path);
            }
        })?;

        // Watch all provider root directories recursively
        for provider in providers {
            if provider.root.exists() {
                watcher.watch(&provider.root, RecursiveMode::Recursive)?;
                tracing::info!("Watching {} for {} sessions", provider.root.display(), provider.name);
            }
        }

        Ok(Self {
            _watcher: watcher,
            rx,
        })
    }

    /// Collect changed paths for `flush_interval`, then return the deduplicated batch.
    ///
    /// This implements throttling (not debouncing): we always flush after the
    /// interval, even if writes are still happening. This prevents starvation
    /// on continuously-appended JSONL files.
    ///
    /// Blocks until at least one event arrives, then collects for flush_interval.
    /// Returns None if the watcher channel was closed.
    pub async fn next_batch(&mut self, flush_interval: Duration) -> Option<Vec<PathBuf>> {
        let mut batch = HashSet::new();

        // Wait for the first event (blocks until something happens — zero CPU)
        match self.rx.recv().await {
            Some(path) => {
                batch.insert(path);
            }
            None => return None, // Channel closed
        }

        // Collect additional events until flush_interval expires.
        // biased toward the deadline so we always flush on time,
        // even under sustained writes (throttle, not debounce).
        let deadline = tokio::time::Instant::now() + flush_interval;
        loop {
            tokio::select! {
                biased;
                _ = tokio::time::sleep_until(deadline) => {
                    break;
                }
                result = self.rx.recv() => {
                    match result {
                        Some(path) => { batch.insert(path); }
                        None => break,
                    }
                }
            }
        }

        Some(batch.into_iter().collect())
    }
}
