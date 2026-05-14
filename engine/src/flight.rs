//! Local flight recorder for dogfood debugging.
//!
//! This is deliberately machine-local and metadata-only. It records timing,
//! queue, and resource counters, but never transcript payloads, prompt text,
//! tool bodies, compressed request bytes, or response bodies.

use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::SystemTime;

use anyhow::{Context, Result};
use crossbeam_channel::{bounded, Receiver, Sender, TrySendError};
use rusqlite::Connection;
use serde_json::{json, Value};

use crate::shipping_stats::ShipStatsSummary;
use crate::state::spool::Spool;

const DEFAULT_BUFFER_CAPACITY: usize = 2048;
const RETENTION_DAYS: i64 = 7;

#[derive(Clone)]
pub struct FlightRecorder {
    tx: Sender<Value>,
    dropped: Arc<AtomicU64>,
}

pub fn flight_recorder_enabled() -> bool {
    match std::env::var("LONGHOUSE_ENGINE_FLIGHT_RECORDER") {
        Ok(value) => matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => false,
    }
}

impl FlightRecorder {
    pub fn start(dir: PathBuf) -> Result<Self> {
        fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
        prune_old_files(&dir);

        let capacity = std::env::var("LONGHOUSE_ENGINE_FLIGHT_RECORDER_BUFFER")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .filter(|value| *value > 0)
            .unwrap_or(DEFAULT_BUFFER_CAPACITY);
        let (tx, rx) = bounded(capacity);
        std::thread::Builder::new()
            .name("longhouse-flight-recorder".to_string())
            .spawn(move || writer_loop(dir, rx))
            .context("starting flight recorder writer")?;

        Ok(Self {
            tx,
            dropped: Arc::new(AtomicU64::new(0)),
        })
    }

    pub fn record(&self, mut value: Value) {
        stamp_record(&mut value, self.dropped.load(Ordering::Relaxed));
        match self.tx.try_send(value) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
            }
            Err(TrySendError::Disconnected(_)) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

pub fn outbox_snapshot(dir: &Path) -> Value {
    let mut count = 0_u64;
    let mut bytes = 0_u64;
    let mut oldest_age_ms: Option<u64> = None;
    let now = SystemTime::now();

    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return json!({
                "status": "missing",
                "count": 0,
                "bytes": 0,
                "oldest_age_ms": null,
            });
        }
        Err(err) => {
            return json!({
                "status": "read_failed",
                "error_kind": err.kind().to_string(),
            });
        }
    };

    for entry in entries.flatten() {
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        if !metadata.is_file() {
            continue;
        }
        count += 1;
        bytes = bytes.saturating_add(metadata.len());
        if let Ok(modified) = metadata.modified() {
            if let Ok(age) = now.duration_since(modified) {
                let age_ms = age.as_millis().min(u128::from(u64::MAX)) as u64;
                oldest_age_ms = Some(
                    oldest_age_ms
                        .map(|current| current.max(age_ms))
                        .unwrap_or(age_ms),
                );
            }
        }
    }

    json!({
        "status": "ok",
        "count": count,
        "bytes": bytes,
        "oldest_age_ms": oldest_age_ms,
    })
}

pub fn spool_snapshot(conn: &Connection) -> Value {
    let spool = Spool::new(conn);
    json!({
        "pending_count": spool.pending_count().ok(),
        "dead_count": spool.dead_count().ok(),
    })
}

pub fn process_snapshot() -> Value {
    match getrusage_snapshot() {
        Some(snapshot) => snapshot,
        None => json!({ "status": "unavailable" }),
    }
}

pub fn disk_snapshot(path: &Path) -> Value {
    json!({
        "path": path.to_string_lossy(),
        "free_bytes": disk_free_bytes(path),
    })
}

pub fn ship_stats_snapshot(summary: ShipStatsSummary) -> Value {
    json!({
        "last_ship_attempt_at": summary.last_ship_attempt_at,
        "last_ship_result": summary.last_ship_result,
        "last_ship_latency_ms": summary.last_ship_latency_ms,
        "last_ship_http_status": summary.last_ship_http_status,
        "last_ship_error_kind": summary.last_ship_error_kind,
        "ship_attempts_1h": summary.ship_attempts_1h,
        "ship_successes_1h": summary.ship_successes_1h,
        "ship_rate_limited_1h": summary.ship_rate_limited_1h,
        "ship_server_errors_1h": summary.ship_server_errors_1h,
        "ship_payload_rejections_1h": summary.ship_payload_rejections_1h,
        "ship_payload_too_large_1h": summary.ship_payload_too_large_1h,
        "ship_retryable_client_errors_1h": summary.ship_retryable_client_errors_1h,
        "ship_connect_errors_1h": summary.ship_connect_errors_1h,
        "ship_latency_p50_ms_1h": summary.ship_latency_p50_ms_1h,
        "ship_latency_p95_ms_1h": summary.ship_latency_p95_ms_1h,
        "ship_attempts_10m": summary.ship_attempts_10m,
        "ship_successes_10m": summary.ship_successes_10m,
        "ship_rate_limited_10m": summary.ship_rate_limited_10m,
        "ship_server_errors_10m": summary.ship_server_errors_10m,
        "ship_retryable_client_errors_10m": summary.ship_retryable_client_errors_10m,
        "ship_connect_errors_10m": summary.ship_connect_errors_10m,
    })
}

fn stamp_record(value: &mut Value, dropped: u64) {
    if !value.is_object() {
        *value = json!({ "value": value.take() });
    }
    let Some(map) = value.as_object_mut() else {
        return;
    };
    map.entry("recorded_at".to_string())
        .or_insert_with(|| json!(chrono::Utc::now().to_rfc3339()));
    map.insert(
        "flight_recorder_dropped_records".to_string(),
        json!(dropped),
    );
}

fn writer_loop(dir: PathBuf, rx: Receiver<Value>) {
    let mut current_date = String::new();
    let mut writer: Option<BufWriter<File>> = None;

    for value in rx {
        let date = chrono::Utc::now().format("%Y-%m-%d").to_string();
        if writer.is_none() || current_date != date {
            current_date = date;
            writer = match open_writer(&dir, &current_date) {
                Ok(writer) => Some(writer),
                Err(err) => {
                    tracing::warn!(
                        error = %err,
                        dir = %dir.display(),
                        "flight recorder could not open JSONL file"
                    );
                    None
                }
            };
        }

        let Some(active_writer) = writer.as_mut() else {
            continue;
        };
        match serde_json::to_writer(&mut *active_writer, &value) {
            Ok(()) => {
                if let Err(err) = active_writer
                    .write_all(b"\n")
                    .and_then(|_| active_writer.flush())
                {
                    tracing::warn!(
                        error = %err,
                        dir = %dir.display(),
                        "flight recorder write failed"
                    );
                    writer = None;
                }
            }
            Err(err) => {
                tracing::warn!(error = %err, "flight recorder JSON serialization failed");
            }
        }
    }
}

fn open_writer(dir: &Path, date: &str) -> std::io::Result<BufWriter<File>> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(format!("flight-{date}.jsonl")))?;
    Ok(BufWriter::new(file))
}

fn prune_old_files(dir: &Path) {
    let cutoff = chrono::Utc::now() - chrono::Duration::days(RETENTION_DAYS);
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        let Some(date_text) = name
            .strip_prefix("flight-")
            .and_then(|value| value.strip_suffix(".jsonl"))
        else {
            continue;
        };
        let Ok(date) = chrono::NaiveDate::parse_from_str(date_text, "%Y-%m-%d") else {
            continue;
        };
        let Some(file_midnight) = date.and_hms_opt(0, 0, 0) else {
            continue;
        };
        let file_time =
            chrono::DateTime::<chrono::Utc>::from_naive_utc_and_offset(file_midnight, chrono::Utc);
        if file_time < cutoff {
            let _ = fs::remove_file(path);
        }
    }
}

#[cfg(unix)]
fn disk_free_bytes(path: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;

    let c_path = CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(c_path.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return None;
    }
    let stat = unsafe { stat.assume_init() };
    Some((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64))
}

#[cfg(not(unix))]
fn disk_free_bytes(_path: &Path) -> Option<u64> {
    None
}

#[cfg(unix)]
fn getrusage_snapshot() -> Option<Value> {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } != 0 {
        return None;
    }
    let usage = unsafe { usage.assume_init() };

    #[cfg(target_os = "macos")]
    let max_rss_bytes = usage.ru_maxrss as u64;
    #[cfg(not(target_os = "macos"))]
    let max_rss_bytes = (usage.ru_maxrss as u64).saturating_mul(1024);

    Some(json!({
        "status": "ok",
        "max_rss_bytes": max_rss_bytes,
        "user_cpu_us": timeval_to_micros(usage.ru_utime),
        "system_cpu_us": timeval_to_micros(usage.ru_stime),
    }))
}

#[cfg(not(unix))]
fn getrusage_snapshot() -> Option<Value> {
    None
}

#[cfg(unix)]
fn timeval_to_micros(value: libc::timeval) -> u64 {
    (value.tv_sec as u64)
        .saturating_mul(1_000_000)
        .saturating_add(value.tv_usec as u64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Map;

    #[test]
    fn recorder_writes_jsonl_without_payload_assumptions() {
        let dir = tempfile::tempdir().unwrap();
        let recorder = FlightRecorder::start(dir.path().to_path_buf()).unwrap();
        recorder.record(json!({
            "schema": "test.v1",
            "kind": "sample"
        }));
        drop(recorder);

        let path = dir.path().join(format!(
            "flight-{}.jsonl",
            chrono::Utc::now().format("%Y-%m-%d")
        ));
        for _ in 0..50 {
            if path.exists()
                && fs::read_to_string(&path)
                    .unwrap_or_default()
                    .contains("test.v1")
            {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        panic!("flight recorder did not write test record");
    }

    #[test]
    fn stamp_record_adds_timestamp_and_drop_count() {
        let mut record = Value::Object(Map::new());
        stamp_record(&mut record, 3);
        assert!(record.get("recorded_at").is_some());
        assert_eq!(
            record
                .get("flight_recorder_dropped_records")
                .and_then(Value::as_u64),
            Some(3)
        );
    }
}
