use parking_lot::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::watch;

const DEFAULT_FRESH_HORIZON_SECS: u64 = 120;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostLifecycle {
    #[serde(rename = "type", default)]
    pub kind: String,
    pub state: String,
    #[serde(default)]
    pub runtime_epoch: Option<String>,
    #[serde(default)]
    pub attempt_id: Option<String>,
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub expected_back_by: Option<String>,
    #[serde(default)]
    pub deadline: Option<String>,
    #[serde(default)]
    pub cutoff: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostLinkStatus {
    pub state: String,
    pub since: String,
    pub claim: Option<HostLifecycle>,
    pub claim_started_at: Option<String>,
    pub last_acknowledged_at: Option<String>,
    pub fresh_horizon_secs: Option<u64>,
    /// Last time any serving evidence arrived: an accepted write, an open
    /// admission, a serving lifecycle frame or a heartbeat acknowledgement.
    #[serde(default)]
    pub last_serving_at: Option<String>,
    pub runtime_epoch: Option<String>,
}
impl HostLinkStatus {
    pub fn has_valid_claim_at(&self, now: DateTime<Utc>) -> bool {
        valid_claim(self.claim.as_ref(), now)
    }

    pub fn state_now(&self) -> String {
        state_at(self, Utc::now())
    }
}

#[derive(Clone)]
pub struct HostLink {
    inner: Arc<Mutex<HostLinkStatus>>,
    changed: watch::Sender<HostLinkStatus>,
    serving_generation: Arc<AtomicU64>,
    last_serving_epoch: Arc<Mutex<Option<String>>>,
}

impl HostLink {
    pub fn new() -> Self {
        let now = Utc::now().to_rfc3339();
        let initial = HostLinkStatus {
            state: "unknown".to_string(),
            since: now,
            claim: None,
            claim_started_at: None,
            last_acknowledged_at: None,
            fresh_horizon_secs: None,
            last_serving_at: None,
            runtime_epoch: None,
        };
        let (changed, _) = watch::channel(initial.clone());
        Self {
            inner: Arc::new(Mutex::new(initial)),
            changed,
            serving_generation: Arc::new(AtomicU64::new(0)),
            last_serving_epoch: Arc::new(Mutex::new(None)),
        }
    }

    pub fn subscribe(&self) -> watch::Receiver<HostLinkStatus> {
        self.changed.subscribe()
    }
    pub fn serving_generation(&self) -> u64 {
        self.serving_generation.load(Ordering::Relaxed)
    }

    pub fn snapshot(&self) -> HostLinkStatus {
        self.refresh()
    }

    pub fn refresh(&self) -> HostLinkStatus {
        let mut status = self.inner.lock();
        let now = Utc::now();
        let state = state_at(&status, now);
        if status.state != state {
            status.state = state;
            status.since = now.to_rfc3339();
            self.changed.send_replace(status.clone());
        }
        status.clone()
    }

    pub fn has_valid_claim(&self) -> bool {
        let status = self.refresh();
        valid_claim(status.claim.as_ref(), Utc::now())
    }

    pub fn is_updating(&self) -> bool {
        matches!(self.refresh().state.as_str(), "updating" | "slow_update")
    }

    /// Whether a planned restart explains this failure, so it may log quietly.
    /// Only restart-shaped failures qualify, and only while a claim is held:
    /// transport errors and timeouts, typed restart refusals
    /// (`runtime_restarting` / `runtime_unreachable`), or a gateway page with
    /// no body of ours. Anything else, such as a 4xx, a typed backpressure
    /// refusal or a serialization error, is a genuine failure and keeps its
    /// normal level during an update.
    pub fn explains_failure(&self, error: &str) -> bool {
        self.is_updating() && restart_shaped_failure(error)
    }

    pub fn observe_lifecycle_value(&self, value: &Value) {
        let Ok(lifecycle) = serde_json::from_value::<HostLifecycle>(value.clone()) else {
            return;
        };
        if lifecycle.kind != "host.lifecycle" {
            return;
        }
        match lifecycle.state.as_str() {
            "updating" => self.observe_claim(lifecycle),
            "serving" => self.observe_serving_evidence(lifecycle.runtime_epoch.as_deref()),
            _ => {}
        }
    }

    pub fn observe_claim(&self, claim: HostLifecycle) {
        if claim.kind != "host.lifecycle"
            || claim.state != "updating"
            || !valid_claim(Some(&claim), Utc::now())
        {
            return;
        }
        let now = Utc::now();
        let mut status = self.inner.lock();
        let same_attempt = status
            .claim
            .as_ref()
            .and_then(|previous| previous.attempt_id.as_deref())
            == claim.attempt_id.as_deref();
        status.claim_started_at = if same_attempt {
            status
                .claim_started_at
                .clone()
                .or_else(|| Some(now.to_rfc3339()))
        } else {
            Some(now.to_rfc3339())
        };
        status.runtime_epoch = claim.runtime_epoch.clone().or(status.runtime_epoch.clone());
        status.claim = Some(claim);
        let state = state_at(&status, now);
        if status.state != state {
            status.state = state;
            status.since = now.to_rfc3339();
        }
        self.changed.send_replace(status.clone());
    }

    pub fn observe_default_restart_claim(&self) {
        // Repeated 1012 closes are not lease renewals; preserve the first
        // default cutoff until serving evidence clears this claim.
        if self.snapshot().claim.is_some() {
            return;
        }
        let now = Utc::now();
        let lifecycle = HostLifecycle {
            kind: "host.lifecycle".to_string(),
            state: "updating".to_string(),
            runtime_epoch: None,
            attempt_id: None,
            phase: None,
            expected_back_by: Some((now + ChronoDuration::seconds(30)).to_rfc3339()),
            deadline: Some((now + ChronoDuration::seconds(360)).to_rfc3339()),
            cutoff: Some((now + ChronoDuration::seconds(960)).to_rfc3339()),
        };
        self.observe_claim(lifecycle);
    }

    pub fn observe_serving_evidence(&self, runtime_epoch: Option<&str>) {
        let now = Utc::now();
        let mut status = self.inner.lock();
        let epoch = runtime_epoch.filter(|epoch| !epoch.is_empty());
        let mut last_serving_epoch = self.last_serving_epoch.lock();
        let serving_epoch_changed =
            epoch.is_some_and(|epoch| last_serving_epoch.as_deref() != Some(epoch));
        if status.state != "serving" || serving_epoch_changed {
            self.serving_generation.fetch_add(1, Ordering::Relaxed);
        }
        if let Some(epoch) = epoch {
            status.runtime_epoch = Some(epoch.to_string());
            *last_serving_epoch = Some(epoch.to_string());
        }
        status.claim = None;
        status.claim_started_at = None;
        status.last_serving_at = Some(now.to_rfc3339());
        if status.state != "serving" {
            status.state = "serving".to_string();
            status.since = now.to_rfc3339();
        }
        self.changed.send_replace(status.clone());
    }

    pub fn observe_runtime_admission(&self, runtime_epoch: Option<&str>, admission: Option<&str>) {
        self.note_runtime_epoch(runtime_epoch);
        if admission == Some("open") {
            self.observe_serving_evidence(runtime_epoch);
        } else {
            self.refresh();
        }
    }
    pub fn observe_fresh_horizon(&self, fresh_horizon_secs: Option<u64>) {
        let Some(seconds) = fresh_horizon_secs.filter(|seconds| *seconds > 0) else {
            return;
        };
        let mut status = self.inner.lock();
        if status.fresh_horizon_secs != Some(seconds) {
            status.fresh_horizon_secs = Some(seconds);
            self.changed.send_replace(status.clone());
        }
    }

    pub fn record_heartbeat_ack(&self, fresh_horizon_secs: Option<u64>) {
        self.observe_serving_evidence(None);
        let now = Utc::now();
        let mut status = self.inner.lock();
        status.last_acknowledged_at = Some(now.to_rfc3339());
        if fresh_horizon_secs.is_some_and(|seconds| seconds > 0) {
            status.fresh_horizon_secs = fresh_horizon_secs;
        }
        status.claim = None;
        status.claim_started_at = None;
        if status.state != "serving" {
            status.state = "serving".to_string();
            status.since = now.to_rfc3339();
        }
        self.changed.send_replace(status.clone());
    }

    /// Record a typed restart refusal. Returns true only for a host-wide K1
    /// refusal (`runtime_restarting` / `runtime_unreachable`), the one case in
    /// which every request to this host should wait out `Retry-After`. Lane
    /// backpressure and other 503s/429s stay local to their caller.
    pub fn observe_http_rejection(&self, status_code: u16, body: &str) -> bool {
        if status_code != 503 {
            return false;
        }
        let Ok(value) = serde_json::from_str::<Value>(body) else {
            return false;
        };
        if let Some(claim) = value.get("claim") {
            self.observe_lifecycle_value(claim);
        }
        matches!(
            value.get("code").and_then(Value::as_str),
            Some("runtime_restarting" | "runtime_unreachable")
        )
    }

    pub fn note_runtime_epoch(&self, runtime_epoch: Option<&str>) {
        if let Some(epoch) = runtime_epoch.filter(|epoch| !epoch.is_empty()) {
            let mut status = self.inner.lock();
            if status.runtime_epoch.as_deref() != Some(epoch) {
                status.runtime_epoch = Some(epoch.to_string());
                self.changed.send_replace(status.clone());
            }
        }
    }
}

pub(crate) fn restart_shaped_failure(error: &str) -> bool {
    let error = error.to_ascii_lowercase();
    if error.contains("runtime_restarting") || error.contains("runtime_unreachable") {
        return true;
    }
    let transport = [
        "error sending request",
        "connection",
        "timed out",
        "timeout",
        "post failed",
        "put failed",
        "broken pipe",
        "reset by peer",
    ];
    if transport.iter().any(|needle| error.contains(needle)) && !error.contains("returned 4") {
        return true;
    }
    let gateway_status = ["returned 502", "returned 503", "returned 504"]
        .iter()
        .any(|needle| error.contains(needle));
    gateway_status
        && (error.contains("<!doctype")
            || error.contains("<html")
            || error.trim_end().ends_with(':'))
}

impl Default for HostLink {
    fn default() -> Self {
        Self::new()
    }
}

fn valid_claim(claim: Option<&HostLifecycle>, now: DateTime<Utc>) -> bool {
    let Some(claim) = claim else {
        return false;
    };
    if claim.state != "updating" {
        return false;
    }
    let Some(deadline) = claim.deadline.as_deref().and_then(parse_timestamp) else {
        return false;
    };
    let Some(cutoff) = claim.cutoff.as_deref().and_then(parse_timestamp) else {
        return false;
    };
    now < deadline.min(cutoff)
}

fn state_at(status: &HostLinkStatus, now: DateTime<Utc>) -> String {
    if let Some(claim) = status.claim.as_ref() {
        if valid_claim(Some(claim), now) {
            let expected = claim
                .expected_back_by
                .as_deref()
                .and_then(parse_timestamp)
                .or_else(|| claim.deadline.as_deref().and_then(parse_timestamp));
            return if expected.is_some_and(|expected| now < expected) {
                "updating"
            } else {
                "slow_update"
            }
            .to_string();
        }
        return "unreachable".to_string();
    }

    let horizon = status
        .fresh_horizon_secs
        .filter(|seconds| *seconds > 0)
        .unwrap_or(DEFAULT_FRESH_HORIZON_SECS);
    // The freshest serving evidence counts. An old heartbeat acknowledgement
    // must never outvote a recovery (open admission, accepted write, serving
    // frame) observed just now.
    let reference = [
        status.last_acknowledged_at.as_deref(),
        status.last_serving_at.as_deref(),
    ]
    .into_iter()
    .flatten()
    .filter_map(parse_timestamp)
    .max()
    .or_else(|| parse_timestamp(&status.since));
    if reference.is_some_and(|at| now.signed_duration_since(at).num_seconds() < horizon as i64) {
        if status.last_acknowledged_at.is_some() || status.state == "serving" {
            "serving".to_string()
        } else {
            status.state.clone()
        }
    } else {
        "unreachable".to_string()
    }
}

fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

#[cfg(test)]
mod tests {
    use super::{state_at, HostLifecycle, HostLink};
    use chrono::{Duration, Utc};
    use serde_json::json;
    fn claim(expected: i64, deadline: i64, cutoff: i64, attempt: &str) -> HostLifecycle {
        let now = Utc::now();
        let at = |seconds| (now + Duration::seconds(seconds)).to_rfc3339();
        HostLifecycle {
            kind: "host.lifecycle".to_string(),
            state: "updating".to_string(),
            runtime_epoch: Some("runtime-a".to_string()),
            attempt_id: Some(attempt.to_string()),
            phase: Some("drain".to_string()),
            expected_back_by: Some(at(expected)),
            deadline: Some(at(deadline)),
            cutoff: Some(at(cutoff)),
        }
    }

    #[test]
    fn each_claim_source_and_close_default_claim_is_accepted() {
        let link = HostLink::new();
        link.observe_lifecycle_value(&json!({
            "type": "host.lifecycle", "state": "updating", "runtime_epoch": "runtime-a",
            "attempt_id": "attempt", "phase": "drain",
            "expected_back_by": (Utc::now() + Duration::seconds(30)).to_rfc3339(),
            "deadline": (Utc::now() + Duration::seconds(360)).to_rfc3339(),
            "cutoff": (Utc::now() + Duration::seconds(960)).to_rfc3339()
        }));
        assert_eq!(link.snapshot().state, "updating");

        let k1 = HostLink::new();
        k1.observe_http_rejection(
            503,
            &json!({"code":"runtime_restarting", "claim": {
                "type":"host.lifecycle", "state":"updating", "runtime_epoch":"runtime-b",
                "attempt_id":"attempt", "expected_back_by":(Utc::now() + Duration::seconds(30)).to_rfc3339(),
                "deadline":(Utc::now() + Duration::seconds(360)).to_rfc3339(),
                "cutoff":(Utc::now() + Duration::seconds(960)).to_rfc3339()
            }}).to_string(),
        );
        assert_eq!(k1.snapshot().state, "updating");

        let closed = HostLink::new();
        closed.observe_default_restart_claim();
        assert_eq!(closed.snapshot().state, "updating");
        assert!(closed.snapshot().claim.unwrap().cutoff.is_some());
    }

    #[test]
    fn repeated_restart_close_does_not_extend_an_expired_default_claim() {
        let link = HostLink::new();
        link.observe_default_restart_claim();
        let expired_at = (Utc::now() - Duration::seconds(1)).to_rfc3339();
        {
            let mut status = link.inner.lock();
            let claim = status.claim.as_mut().unwrap();
            claim.expected_back_by = Some(expired_at.clone());
            claim.deadline = Some(expired_at.clone());
            claim.cutoff = Some(expired_at.clone());
        }

        link.observe_default_restart_claim();

        let status = link.snapshot();
        assert_eq!(status.state, "unreachable");
        assert_eq!(
            status.claim.unwrap().cutoff.as_deref(),
            Some(expired_at.as_str())
        );
    }

    #[test]
    fn renewal_preserves_claim_start_and_epoch_change_alone_does_not_serve() {
        let link = HostLink::new();
        link.observe_claim(claim(30, 360, 960, "attempt"));
        let started = link.snapshot().claim_started_at;
        link.note_runtime_epoch(Some("runtime-b"));
        assert_eq!(link.snapshot().state, "updating");
        link.observe_claim(claim(45, 420, 960, "attempt"));
        assert_eq!(link.snapshot().claim_started_at, started);
        assert_eq!(link.snapshot().state, "updating");
        link.observe_lifecycle_value(
            &json!({"type":"host.lifecycle", "state":"serving", "runtime_epoch":"runtime-b"}),
        );
        assert_eq!(link.snapshot().state, "serving");
        assert!(link.snapshot().claim.is_none());
    }

    #[test]
    fn claim_expiry_moves_from_slow_update_to_unreachable() {
        let link = HostLink::new();
        let now = Utc::now();
        let lifecycle = HostLifecycle {
            kind: "host.lifecycle".to_string(),
            state: "updating".to_string(),
            runtime_epoch: Some("runtime-a".to_string()),
            attempt_id: Some("attempt".to_string()),
            phase: None,
            expected_back_by: Some((now - Duration::seconds(1)).to_rfc3339()),
            deadline: Some((now + Duration::seconds(20)).to_rfc3339()),
            cutoff: Some((now + Duration::seconds(30)).to_rfc3339()),
        };
        link.observe_claim(lifecycle);
        assert_eq!(link.snapshot().state, "slow_update");

        let expired = HostLink::new();
        let now = Utc::now();
        expired.observe_claim(HostLifecycle {
            kind: "host.lifecycle".to_string(),
            state: "updating".to_string(),
            runtime_epoch: None,
            attempt_id: Some("attempt".to_string()),
            phase: None,
            expected_back_by: Some((now - Duration::seconds(20)).to_rfc3339()),
            deadline: Some((now - Duration::seconds(2)).to_rfc3339()),
            cutoff: Some((now + Duration::seconds(30)).to_rfc3339()),
        });
        assert_eq!(expired.snapshot().state, "unknown");
    }
    #[test]
    fn state_boundaries_use_expected_deadline_and_cutoff_timestamps() {
        let now = Utc::now();
        let timestamp = |seconds| (now + Duration::seconds(seconds)).to_rfc3339();
        let updating_claim = |deadline, cutoff| HostLifecycle {
            kind: "host.lifecycle".to_string(),
            state: "updating".to_string(),
            runtime_epoch: Some("runtime-a".to_string()),
            attempt_id: Some("attempt".to_string()),
            phase: None,
            expected_back_by: Some(timestamp(5)),
            deadline: Some(timestamp(deadline)),
            cutoff: Some(timestamp(cutoff)),
        };
        let mut status = HostLink::new().snapshot();

        status.claim = Some(updating_claim(10, 15));
        assert_eq!(state_at(&status, now + Duration::seconds(4)), "updating");
        assert_eq!(state_at(&status, now + Duration::seconds(5)), "slow_update");
        assert_eq!(
            state_at(&status, now + Duration::seconds(10)),
            "unreachable"
        );

        status.claim = Some(updating_claim(15, 10));
        assert_eq!(
            state_at(&status, now + Duration::seconds(10)),
            "unreachable"
        );
    }

    #[test]
    fn serving_requires_open_admission_or_write_and_ack_horizon_is_retained() {
        let link = HostLink::new();
        link.observe_claim(claim(30, 360, 960, "attempt"));
        link.observe_runtime_admission(Some("runtime-b"), Some("pending"));
        assert_eq!(link.snapshot().state, "updating");
        link.observe_runtime_admission(Some("runtime-b"), Some("open"));
        assert_eq!(link.snapshot().state, "serving");

        let ack = HostLink::new();
        ack.record_heartbeat_ack(Some(240));
        assert_eq!(ack.snapshot().fresh_horizon_secs, Some(240));
        assert!(ack.snapshot().last_acknowledged_at.is_some());
    }

    #[test]
    fn fresh_serving_evidence_outvotes_an_expired_heartbeat_ack() {
        let link = HostLink::new();
        link.record_heartbeat_ack(Some(120));
        let mut status = link.snapshot();
        let stale = (Utc::now() - Duration::seconds(600)).to_rfc3339();
        status.last_acknowledged_at = Some(stale.clone());
        status.last_serving_at = Some(stale);
        assert_eq!(state_at(&status, Utc::now()), "unreachable");

        // An open admission (or accepted write) right now means serving, even
        // though the last heartbeat acknowledgement is far past the horizon.
        status.last_serving_at = Some(Utc::now().to_rfc3339());
        assert_eq!(state_at(&status, Utc::now()), "serving");
    }

    #[test]
    fn only_typed_restart_refusals_are_host_wide() {
        let link = HostLink::new();
        assert!(
            link.observe_http_rejection(503, &json!({"code": "runtime_restarting"}).to_string())
        );
        assert!(
            link.observe_http_rejection(503, &json!({"code": "runtime_unreachable"}).to_string())
        );
        // Lane backpressure, untyped 503s and HTML pages never pause the host.
        assert!(
            !link.observe_http_rejection(429, &json!({"code": "runtime_restarting"}).to_string())
        );
        assert!(
            !link.observe_http_rejection(503, &json!({"code": "write_backpressure"}).to_string())
        );
        assert!(!link.observe_http_rejection(503, "<!DOCTYPE html>"));
    }

    #[test]
    fn only_restart_shaped_failures_log_quietly_during_an_update() {
        let link = HostLink::new();
        link.observe_claim(claim(30, 360, 960, "attempt"));
        // Expected while the host restarts: transport errors, typed refusals,
        // gateway pages.
        assert!(link.explains_failure("POST failed: error sending request for url"));
        assert!(link.explains_failure(r#"POST returned 503: {"code":"runtime_restarting"}"#));
        assert!(link.explains_failure("POST returned 502: <!DOCTYPE html><title>Bad gateway"));
        // Genuine failures keep their level even during an update.
        assert!(!link.explains_failure(r#"POST returned 422: {"detail":"invalid event"}"#));
        assert!(!link.explains_failure(r#"POST returned 503: {"code":"write_backpressure"}"#));
        assert!(!link.explains_failure("task 12 panicked with message \"boom\""));
        assert!(!link.explains_failure("runtime event could not be serialized"));
        // Without a claim nothing is explained away.
        assert!(!HostLink::new().explains_failure("POST failed: error sending request for url"));
    }
}
