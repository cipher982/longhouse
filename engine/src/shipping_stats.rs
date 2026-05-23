//! Rolling transport telemetry for engine ship attempts.
//!
//! This stays machine-local and cheap:
//! - bounded in-memory window
//! - one record per ship attempt
//! - summaries only, no payload content

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const SHIP_STATS_WINDOW: Duration = Duration::from_secs(60 * 60);
const SHIP_STATS_ACTIVE_WINDOW: Duration = Duration::from_secs(10 * 60);
const SHIP_STATS_MAX_RECORDS: usize = 50_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShipAttemptOutcome {
    Ok,
    RateLimited,
    ServerError,
    PayloadRejected,
    PayloadTooLarge,
    RetryableClientError,
    ConnectError,
}

impl ShipAttemptOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            ShipAttemptOutcome::Ok => "ok",
            ShipAttemptOutcome::RateLimited => "rate_limited",
            ShipAttemptOutcome::ServerError => "server_error",
            ShipAttemptOutcome::PayloadRejected => "payload_rejected",
            ShipAttemptOutcome::PayloadTooLarge => "payload_too_large",
            ShipAttemptOutcome::RetryableClientError => "retryable_client_error",
            ShipAttemptOutcome::ConnectError => "connect_error",
        }
    }
}

#[derive(Debug, Clone)]
struct ShipAttemptRecord {
    at: Instant,
    recorded_at: String,
    outcome: ShipAttemptOutcome,
    latency_ms: u64,
    http_status: Option<u16>,
    error_kind: Option<String>,
    error_message: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct ShipStatsSummary {
    pub last_ship_attempt_at: Option<String>,
    pub last_ship_result: Option<String>,
    pub last_ship_latency_ms: Option<u64>,
    pub last_ship_http_status: Option<u16>,
    pub last_ship_error_kind: Option<String>,
    pub last_ship_error_message: Option<String>,
    pub ship_attempts_1h: u32,
    pub ship_successes_1h: u32,
    pub ship_rate_limited_1h: u32,
    pub ship_server_errors_1h: u32,
    pub ship_payload_rejections_1h: u32,
    pub ship_payload_too_large_1h: u32,
    pub ship_retryable_client_errors_1h: u32,
    pub ship_connect_errors_1h: u32,
    pub ship_latency_p50_ms_1h: Option<u64>,
    pub ship_latency_p95_ms_1h: Option<u64>,
    pub ship_attempts_10m: u32,
    pub ship_successes_10m: u32,
    pub ship_rate_limited_10m: u32,
    pub ship_server_errors_10m: u32,
    pub ship_retryable_client_errors_10m: u32,
    pub ship_connect_errors_10m: u32,
    /// Phase 1 instrumentation: EWMA events/sec over the last ~10s of
    /// successful ship attempts. Drives the phase 2 adaptive controller
    /// and the bench harness's "events shipped per second" axis.
    pub events_per_sec_ewma_10s: Option<f64>,
}

/// EWMA throughput tracker for successful ship events.
///
/// Phase 1 instrumentation: a 10s exponential moving average on
/// events-per-second computed from successful ship attempts. The phase 2
/// adaptive controller reads this together with server-side queue_wait_ms
/// to decide whether to grow or shrink in-flight requests.
#[derive(Debug, Clone, Default)]
struct EwmaThroughput {
    /// Last update timestamp (None until first sample).
    last_at: Option<Instant>,
    /// Current EWMA estimate of events/sec.
    ewma_eps: f64,
}

impl EwmaThroughput {
    /// 10s time-constant — half-life ≈ 6.9s. New samples weighted by the
    /// elapsed-time fraction so bursty/idle gaps do not double-count.
    const TIME_CONSTANT_SECS: f64 = 10.0;

    fn record(&mut self, now: Instant, events: u32, latency_ms: u64) {
        if events == 0 {
            return;
        }
        // Prefer the actual ship duration (latency_ms) for the instantaneous
        // rate, falling back to wall-clock elapsed since the last sample.
        let dt_secs = (latency_ms.max(1) as f64) / 1000.0;
        let instantaneous = (events as f64) / dt_secs;

        let alpha = match self.last_at {
            None => 1.0,
            Some(prev) => {
                let elapsed = now.saturating_duration_since(prev).as_secs_f64();
                1.0 - (-elapsed / Self::TIME_CONSTANT_SECS).exp()
            }
        };
        self.ewma_eps = alpha * instantaneous + (1.0 - alpha) * self.ewma_eps;
        self.last_at = Some(now);
    }

    fn current(&self, now: Instant) -> Option<f64> {
        let last = self.last_at?;
        // Decay the estimate toward zero if no updates have arrived recently
        // so a long idle period doesn't keep reporting stale throughput.
        let elapsed = now.saturating_duration_since(last).as_secs_f64();
        let decay = (-elapsed / Self::TIME_CONSTANT_SECS).exp();
        let value = self.ewma_eps * decay;
        if !value.is_finite() {
            return None;
        }
        Some(value)
    }
}

#[derive(Clone, Default)]
pub struct RecentShipStatsTracker {
    inner: Arc<Mutex<VecDeque<ShipAttemptRecord>>>,
    throughput: Arc<Mutex<EwmaThroughput>>,
}

impl RecentShipStatsTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::new())),
            throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
        }
    }

    pub fn record(&self, outcome: ShipAttemptOutcome, latency_ms: u64, http_status: Option<u16>) {
        self.record_with_detail(outcome, latency_ms, http_status, None, None);
    }

    /// Record events shipped on a successful attempt for EWMA throughput.
    /// Call this AFTER `record(...)` for `ShipAttemptOutcome::Ok` ships.
    pub fn record_events_shipped(&self, events: u32, latency_ms: u64) {
        if let Ok(mut t) = self.throughput.lock() {
            t.record(Instant::now(), events, latency_ms);
        }
    }

    pub fn record_with_detail(
        &self,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<&str>,
        error_message: Option<&str>,
    ) {
        self.record_at(
            Instant::now(),
            chrono::Utc::now().to_rfc3339(),
            outcome,
            latency_ms,
            http_status,
            error_kind.map(str::to_string),
            error_message.map(truncate_error_message),
        );
    }

    pub fn summary(&self) -> ShipStatsSummary {
        let now = Instant::now();
        if let Ok(mut guard) = self.inner.lock() {
            prune_old_records(&mut guard, now);
            if guard.is_empty() {
                return ShipStatsSummary::default();
            }

            let mut summary = ShipStatsSummary::default();
            let mut latencies = Vec::with_capacity(guard.len());

            for record in guard.iter() {
                summary.ship_attempts_1h += 1;
                latencies.push(record.latency_ms);
                let in_active_window = now.duration_since(record.at) <= SHIP_STATS_ACTIVE_WINDOW;
                if in_active_window {
                    summary.ship_attempts_10m += 1;
                }
                match record.outcome {
                    ShipAttemptOutcome::Ok => {
                        summary.ship_successes_1h += 1;
                        if in_active_window {
                            summary.ship_successes_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::RateLimited => {
                        summary.ship_rate_limited_1h += 1;
                        if in_active_window {
                            summary.ship_rate_limited_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::ServerError => {
                        summary.ship_server_errors_1h += 1;
                        if in_active_window {
                            summary.ship_server_errors_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::PayloadRejected => summary.ship_payload_rejections_1h += 1,
                    ShipAttemptOutcome::PayloadTooLarge => summary.ship_payload_too_large_1h += 1,
                    ShipAttemptOutcome::RetryableClientError => {
                        summary.ship_retryable_client_errors_1h += 1;
                        if in_active_window {
                            summary.ship_retryable_client_errors_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::ConnectError => {
                        summary.ship_connect_errors_1h += 1;
                        if in_active_window {
                            summary.ship_connect_errors_10m += 1;
                        }
                    }
                }
            }

            if let Some(last) = guard.back() {
                summary.last_ship_attempt_at = Some(last.recorded_at.clone());
                summary.last_ship_result = Some(last.outcome.as_str().to_string());
                summary.last_ship_latency_ms = Some(last.latency_ms);
                summary.last_ship_http_status = last.http_status;
                summary.last_ship_error_kind = last.error_kind.clone();
                summary.last_ship_error_message = last.error_message.clone();
            }

            latencies.sort_unstable();
            summary.ship_latency_p50_ms_1h = percentile(&latencies, 0.50);
            summary.ship_latency_p95_ms_1h = percentile(&latencies, 0.95);
            summary.events_per_sec_ewma_10s =
                self.throughput.lock().ok().and_then(|t| t.current(now));
            summary
        } else {
            ShipStatsSummary::default()
        }
    }

    fn record_at(
        &self,
        at: Instant,
        recorded_at: String,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<String>,
        error_message: Option<String>,
    ) {
        if let Ok(mut guard) = self.inner.lock() {
            guard.push_back(ShipAttemptRecord {
                at,
                recorded_at,
                outcome,
                latency_ms,
                http_status,
                error_kind,
                error_message,
            });
            prune_old_records(&mut guard, at);
            while guard.len() > SHIP_STATS_MAX_RECORDS {
                guard.pop_front();
            }
        }
    }
}

fn truncate_error_message(message: &str) -> String {
    const MAX_ERROR_CHARS: usize = 300;
    let mut chars = message.chars();
    let truncated: String = chars.by_ref().take(MAX_ERROR_CHARS).collect();
    if chars.next().is_some() {
        format!("{truncated}...")
    } else {
        truncated
    }
}

fn prune_old_records(records: &mut VecDeque<ShipAttemptRecord>, now: Instant) {
    while let Some(front) = records.front() {
        if now.saturating_duration_since(front.at) > SHIP_STATS_WINDOW {
            records.pop_front();
        } else {
            break;
        }
    }
}

fn percentile(values: &[u64], quantile: f64) -> Option<u64> {
    if values.is_empty() {
        return None;
    }
    let clamped = quantile.clamp(0.0, 1.0);
    let scaled = (values.len().saturating_sub(1) as f64) * clamped;
    let lower_idx = scaled.floor() as usize;
    let upper_idx = scaled.ceil() as usize;
    let lower = *values.get(lower_idx)?;
    let upper = *values.get(upper_idx)?;
    if lower_idx == upper_idx {
        return Some(lower);
    }
    let weight = scaled - lower_idx as f64;
    Some(((lower as f64) + ((upper as f64) - (lower as f64)) * weight).round() as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recent_ship_stats_summary_counts_outcomes_and_percentiles() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        tracker.record_at(
            now - Duration::from_secs(10),
            "2026-04-23T20:00:00Z".to_string(),
            ShipAttemptOutcome::Ok,
            40,
            None,
            None,
            None,
        );
        tracker.record_at(
            now - Duration::from_secs(9),
            "2026-04-23T20:00:01Z".to_string(),
            ShipAttemptOutcome::Ok,
            60,
            None,
            None,
            None,
        );
        tracker.record_at(
            now - Duration::from_secs(8),
            "2026-04-23T20:00:02Z".to_string(),
            ShipAttemptOutcome::ServerError,
            120,
            Some(503),
            Some("server_response".to_string()),
            Some("503: upstream unavailable".to_string()),
        );
        tracker.record_at(
            now - Duration::from_secs(7),
            "2026-04-23T20:00:03Z".to_string(),
            ShipAttemptOutcome::ConnectError,
            220,
            None,
            Some("timeout".to_string()),
            Some("request timed out after 60s".to_string()),
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 4);
        assert_eq!(summary.ship_successes_1h, 2);
        assert_eq!(summary.ship_server_errors_1h, 1);
        assert_eq!(summary.ship_connect_errors_1h, 1);
        assert_eq!(summary.ship_attempts_10m, 4);
        assert_eq!(summary.ship_successes_10m, 2);
        assert_eq!(summary.ship_server_errors_10m, 1);
        assert_eq!(summary.ship_connect_errors_10m, 1);
        assert_eq!(
            summary.last_ship_attempt_at.as_deref(),
            Some("2026-04-23T20:00:03Z")
        );
        assert_eq!(summary.last_ship_result.as_deref(), Some("connect_error"));
        assert_eq!(summary.last_ship_latency_ms, Some(220));
        assert_eq!(summary.last_ship_http_status, None);
        assert_eq!(summary.last_ship_error_kind.as_deref(), Some("timeout"));
        assert_eq!(
            summary.last_ship_error_message.as_deref(),
            Some("request timed out after 60s")
        );
        assert_eq!(summary.ship_latency_p50_ms_1h, Some(90));
        assert_eq!(summary.ship_latency_p95_ms_1h, Some(205));
    }

    #[test]
    fn recent_ship_stats_prunes_old_records() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        tracker.record_at(
            now - Duration::from_secs(SHIP_STATS_WINDOW.as_secs() + 60),
            "2026-04-23T18:00:00Z".to_string(),
            ShipAttemptOutcome::Ok,
            50,
            None,
            None,
            None,
        );
        tracker.record_at(
            now - Duration::from_secs(30),
            "2026-04-23T19:59:30Z".to_string(),
            ShipAttemptOutcome::RateLimited,
            100,
            Some(429),
            Some("rate_limited".to_string()),
            Some("429: rate limited".to_string()),
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 1);
        assert_eq!(summary.ship_rate_limited_1h, 1);
        assert_eq!(summary.ship_successes_1h, 0);
        assert_eq!(summary.ship_attempts_10m, 1);
        assert_eq!(summary.ship_rate_limited_10m, 1);
        assert_eq!(summary.ship_successes_10m, 0);
        assert_eq!(
            summary.last_ship_attempt_at.as_deref(),
            Some("2026-04-23T19:59:30Z")
        );
        assert_eq!(summary.last_ship_result.as_deref(), Some("rate_limited"));
        assert_eq!(summary.last_ship_http_status, Some(429));
        assert_eq!(
            summary.last_ship_error_kind.as_deref(),
            Some("rate_limited")
        );
    }

    #[test]
    fn recent_ship_stats_truncates_last_error_message() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_with_detail(
            ShipAttemptOutcome::ConnectError,
            60_000,
            None,
            Some("timeout"),
            Some(&"x".repeat(350)),
        );

        let summary = tracker.summary();

        let message = summary.last_ship_error_message.unwrap();
        assert_eq!(message.chars().count(), 303);
        assert!(message.ends_with("..."));
    }

    #[test]
    fn ewma_throughput_first_sample_uses_latency_for_instantaneous_rate() {
        let mut tput = EwmaThroughput::default();
        let t0 = Instant::now();
        tput.record(t0, 100, 100); // 100 events in 100ms => 1000 eps
        let v = tput.current(t0).unwrap();
        assert!((v - 1000.0).abs() < 1e-6, "expected 1000 eps, got {v}");
    }

    #[test]
    fn ewma_throughput_decays_toward_zero_during_idle() {
        let mut tput = EwmaThroughput::default();
        let t0 = Instant::now();
        tput.record(t0, 100, 100); // 1000 eps initial
                                   // 60s later, with TIME_CONSTANT 10s, decay factor ≈ e^-6 ≈ 0.0025
        let later = t0 + Duration::from_secs(60);
        let v = tput.current(later).unwrap();
        assert!(v < 5.0, "expected near-zero after 60s idle, got {v}");
    }

    #[test]
    fn record_events_shipped_surfaces_in_summary() {
        let tracker = RecentShipStatsTracker::new();
        // Record one successful ship of 200 events that took 50ms => 4000 eps.
        tracker.record(ShipAttemptOutcome::Ok, 50, None);
        tracker.record_events_shipped(200, 50);
        let summary = tracker.summary();
        let eps = summary.events_per_sec_ewma_10s.unwrap();
        // The summary call may have advanced Instant::now() slightly, so we
        // accept a small decay band rather than equality with 4000.
        assert!(
            eps > 3500.0 && eps <= 4000.5,
            "expected ~4000 eps, got {eps}"
        );
    }

    #[test]
    fn record_events_shipped_zero_events_is_a_noop() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_events_shipped(0, 50);
        let summary = tracker.summary();
        assert_eq!(summary.events_per_sec_ewma_10s, None);
    }

    #[test]
    fn recent_ship_stats_keeps_active_window_separate_from_one_hour_window() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        tracker.record_at(
            now - Duration::from_secs(20 * 60),
            "2026-04-23T19:40:00Z".to_string(),
            ShipAttemptOutcome::ConnectError,
            3_000,
            None,
            Some("timeout".to_string()),
            Some("request timed out".to_string()),
        );
        tracker.record_at(
            now - Duration::from_secs(60),
            "2026-04-23T19:59:00Z".to_string(),
            ShipAttemptOutcome::Ok,
            200,
            None,
            None,
            None,
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 2);
        assert_eq!(summary.ship_connect_errors_1h, 1);
        assert_eq!(summary.ship_attempts_10m, 1);
        assert_eq!(summary.ship_connect_errors_10m, 0);
        assert_eq!(summary.ship_successes_10m, 1);
    }
}
