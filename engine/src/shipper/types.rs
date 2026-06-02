//! Shared types for the shipper module.

use std::path::Path;
use std::time::Duration;

use crate::text::truncate_head_chars;

/// Result of parsing + compressing a single file.
pub struct ShipItem {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub event_count: usize,
    pub session_id: String,
    pub compressed: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct ShipTraceContext {
    pub work_context: &'static str,
    pub observation_source: &'static str,
    pub observed_at_ms: i64,
    pub latest_observed_at_ms: Option<i64>,
    pub wake_received_at_ms: Option<i64>,
    pub enqueued_at_ms: i64,
    pub job_started_at_ms: i64,
    pub prepare_started_at_ms: i64,
    pub prepare_finished_at_ms: i64,
    pub prepare_blocking_queue_wait_ms: Option<u64>,
    pub prepare_open_db_ms: Option<u64>,
    pub prepare_identity_ms: Option<u64>,
    pub prepare_cursor_ms: Option<u64>,
    pub prepare_binding_wait_ms: Option<u64>,
    pub prepare_parse_ms: Option<u64>,
    pub prepare_batch_build_ms: Option<u64>,
    pub session_id_hint: Option<String>,
    pub turn_id: Option<String>,
    pub wake_reason: Option<String>,
    pub file_len_hint: Option<u64>,
}

#[derive(Clone, Debug, Default)]
pub struct PrepareTraceTimings {
    pub blocking_queue_wait_ms: Option<u64>,
    pub open_db_ms: Option<u64>,
    pub identity_ms: Option<u64>,
    pub cursor_ms: Option<u64>,
    pub binding_wait_ms: Option<u64>,
    pub parse_ms: Option<u64>,
    pub batch_build_ms: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SourceLineMode {
    Full,
    EventOnly,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum CursorMode {
    Archive,
    Live,
}

pub struct DeadLetterItem {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub event_count: usize,
    pub session_id: String,
    pub reason: String,
}

pub struct AckOnlyItem {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub session_id: String,
}

pub enum PreparedAction {
    Ship(ShipItem),
    DeadLetter(DeadLetterItem),
    AckOnly(AckOnlyItem),
}

pub struct PreparedFile {
    pub path_str: String,
    pub offset: u64,
    pub new_offset: u64,
    pub has_reply_evidence: bool,
    pub(crate) cursor_mode: CursorMode,
    pub actions: Vec<PreparedAction>,
}

pub const SLOW_FILE_PROCESSING_MS: u128 = 5_000;

pub(crate) fn truncate_http_body(body: &str) -> String {
    truncate_head_chars(body, 200)
}

impl PreparedAction {
    pub(crate) fn event_count(&self) -> usize {
        match self {
            PreparedAction::Ship(item) => item.event_count,
            PreparedAction::DeadLetter(item) => item.event_count,
            PreparedAction::AckOnly(_) => 0,
        }
    }

    #[cfg(test)]
    pub(crate) fn offset(&self) -> u64 {
        match self {
            PreparedAction::Ship(item) => item.offset,
            PreparedAction::DeadLetter(item) => item.offset,
            PreparedAction::AckOnly(item) => item.offset,
        }
    }

    #[cfg(test)]
    pub(crate) fn new_offset(&self) -> u64 {
        match self {
            PreparedAction::Ship(item) => item.new_offset,
            PreparedAction::DeadLetter(item) => item.new_offset,
            PreparedAction::AckOnly(item) => item.new_offset,
        }
    }
}

impl PreparedFile {
    pub fn total_event_count(&self) -> usize {
        self.actions.iter().map(PreparedAction::event_count).sum()
    }
}

pub fn log_slow_file_processing(
    context: &str,
    path: &Path,
    provider: &str,
    event_count: usize,
    byte_count: u64,
    dead_lettered: usize,
    elapsed: Duration,
) {
    if elapsed.as_millis() < SLOW_FILE_PROCESSING_MS {
        return;
    }

    tracing::warn!(
        context,
        path = %path.display(),
        provider,
        event_count,
        byte_count,
        dead_lettered,
        elapsed_ms = elapsed.as_millis() as u64,
        "Slow file processing"
    );
}

pub struct ShipPreparedOutcome {
    pub events_shipped: usize,
    pub bytes_shipped: u64,
    pub dead_lettered: usize,
    pub fully_processed: bool,
    pub had_connect_error: bool,
}

impl Default for ShipPreparedOutcome {
    fn default() -> Self {
        Self {
            events_shipped: 0,
            bytes_shipped: 0,
            dead_lettered: 0,
            fully_processed: true,
            had_connect_error: false,
        }
    }
}

pub(crate) struct ReplaySpoolOutcome {
    pub resolved: usize,
    pub failed: usize,
    pub events_shipped: usize,
    pub had_connect_error: bool,
}

impl Default for ReplaySpoolOutcome {
    fn default() -> Self {
        Self {
            resolved: 0,
            failed: 0,
            events_shipped: 0,
            had_connect_error: false,
        }
    }
}

pub(crate) struct GapRecoveryOutcome {
    pub had_gap: bool,
    pub replay_ready: bool,
}

pub(crate) enum AttemptedShip {
    Shipped(ShipItem),
    Transient {
        item: ShipItem,
        error: String,
        is_connect_error: bool,
        is_backpressure: bool,
    },
    PayloadTooLarge {
        item: ShipItem,
    },
    PayloadRejected {
        item: ShipItem,
        status_code: u16,
        body: String,
    },
}

#[cfg(test)]
#[allow(dead_code)]
pub enum ShipAndRecordOutcome {
    Shipped { events: usize },
    Spooled { is_connect_error: bool },
    DeadLettered { status_code: u16 },
}
