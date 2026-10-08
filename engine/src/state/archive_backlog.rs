//! The `archive_backlog` block of the heartbeat payload.
//!
//! The v1 pointer spool that used to fill these counts is gone: storage-v2
//! ships every source from its own epoch cursor, so there is no separate
//! backlog of byte ranges left to count. The block stays on the wire because
//! the archive repair control (`paused`/`trickle`/`drain`) still reports its
//! mode here, and the Runtime Host, Desktop and machine routes read it. Pause
//! provenance is attached only while ranges are pending, so with no range
//! backlog the block reads `complete`.

use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ArchiveBacklogSnapshot {
    pub state: String,
    pub mode: String,
    pub pending_ranges: usize,
    pub ready_ranges: usize,
    pub deferred_ranges: usize,
    pub pending_paths: usize,
    pub pending_sessions: usize,
    pub pending_bytes: u64,
    pub dead_ranges: usize,
    pub dead_bytes: u64,
    pub huge_pending_ranges: usize,
    pub huge_pending_bytes: u64,
    pub oldest_pending_at: Option<String>,
    pub newest_pending_at: Option<String>,
    pub next_retry_at_min: Option<String>,
    pub next_retry_at_max: Option<String>,
    pub next_deferred_retry_at: Option<String>,
    pub max_retry_count: u32,
    pub latest_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pause_actor: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pause_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pause_updated_at: Option<String>,
    pub providers: Vec<ArchiveProviderSummary>,
    pub size_buckets: BTreeMap<String, ArchiveSizeBucketSummary>,
}

impl Default for ArchiveBacklogSnapshot {
    fn default() -> Self {
        Self {
            state: "complete".to_string(),
            mode: "idle".to_string(),
            pending_ranges: 0,
            ready_ranges: 0,
            deferred_ranges: 0,
            pending_paths: 0,
            pending_sessions: 0,
            pending_bytes: 0,
            dead_ranges: 0,
            dead_bytes: 0,
            huge_pending_ranges: 0,
            huge_pending_bytes: 0,
            oldest_pending_at: None,
            newest_pending_at: None,
            next_retry_at_min: None,
            next_retry_at_max: None,
            next_deferred_retry_at: None,
            max_retry_count: 0,
            latest_error: None,
            pause_actor: None,
            pause_reason: None,
            pause_updated_at: None,
            providers: Vec::new(),
            size_buckets: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct ArchiveProviderSummary {
    pub provider: String,
    pub pending_ranges: usize,
    pub pending_paths: usize,
    pub pending_sessions: usize,
    pub pending_bytes: u64,
    pub dead_ranges: usize,
    pub dead_bytes: u64,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct ArchiveSizeBucketSummary {
    pub pending_ranges: usize,
    pub pending_bytes: u64,
}
