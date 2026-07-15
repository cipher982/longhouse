//! HTTP client for shipping compressed payloads to the Longhouse API.
//!
//! POST `{api_url}/api/agents/ingest` with gzip-compressed JSON body.
//! Handles 429 rate limiting with exponential backoff + Retry-After.

use std::collections::BTreeMap;
use std::time::Duration;

use anyhow::{Context, Result};
use rand::Rng;
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_ENCODING, CONTENT_TYPE, USER_AGENT};
use serde::de::DeserializeOwned;

use crate::config::ShipperConfig;
use crate::pipeline::compressor::{content_encoding, CompressionAlgo};
use crate::shipping::storage_v2::{StorageV2Capabilities, StorageV2Envelope, StorageV2Receipt};
use crate::shipping::storage_v2::{STORAGE_V2_CAPABILITIES_PATH, STORAGE_V2_LANE_HEADER};

const SHIP_TRACE_HEADER: &str = "X-Longhouse-Ship-Trace";
const INGEST_BACKPRESSURE_HEADER: &str = "X-Ingest-Backpressure";
const INGEST_ERROR_KIND_HEADER: &str = "X-Ingest-Error-Kind";
const INGEST_LANE_HEADER: &str = "X-Ingest-Lane";
const WRITE_BACKPRESSURE_HEADER: &str = "X-Longhouse-Write-Backpressure";
const WRITE_ERROR_KIND_HEADER: &str = "X-Longhouse-Write-Error-Kind";
const WRITE_LANE_HEADER: &str = "X-Longhouse-Write-Lane";
const STORAGE_BACKPRESSURE_HEADER: &str = "X-Longhouse-Storage-Backpressure";
const ARCHIVE_INGEST_BACKPRESSURE_KIND: &str = "archive_ingest_backpressure";
const LIVE_INGEST_BACKPRESSURE_KIND: &str = "live_ingest_backpressure";
const HOT_WRITE_BACKPRESSURE_KIND: &str = "hot_write_backpressure";

/// Structured details for a network-layer ingest failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectErrorDetail {
    pub kind: &'static str,
    pub message: String,
}

/// Structured details for server-declared ingest backpressure.
#[derive(Debug, Clone, PartialEq)]
pub struct ServerBackpressureDetail {
    pub status_code: u16,
    pub kind: &'static str,
    pub body: String,
    pub lane: Option<String>,
    pub retry_after_seconds: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct StorageV2Backpressure {
    pub lane: String,
    pub retry_after: Duration,
}

impl std::fmt::Display for StorageV2Backpressure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "storage-v2 {} lane busy; retry after {}ms",
            self.lane,
            self.retry_after.as_millis()
        )
    }
}

impl std::error::Error for StorageV2Backpressure {}

/// Server-side ingest timing parsed from response headers.
///
/// Phase 1 instrumentation: the Runtime Host emits timing, lane, and
/// admission headers on every successful ingest so the engine can adapt
/// concurrency without re-instrumenting in phase 2.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ServerIngestTiming {
    pub queue_wait_ms: Option<f64>,
    pub exec_ms: Option<f64>,
    pub commit_count: Option<u64>,
    pub commit_ms: Option<f64>,
    pub chunk_size: Option<u64>,
    pub store_stage_ms: Option<BTreeMap<String, f64>>,
    pub label: Option<String>,
    pub lane: Option<String>,
    pub admission_state: Option<String>,
}

impl ServerIngestTiming {
    /// True if the server returned at least one of the phase-1 headers.
    pub fn is_observed(&self) -> bool {
        self.queue_wait_ms.is_some()
            || self.exec_ms.is_some()
            || self.commit_count.is_some()
            || self.commit_ms.is_some()
            || self.chunk_size.is_some()
            || self.store_stage_ms.is_some()
            || self.label.is_some()
            || self.lane.is_some()
            || self.admission_state.is_some()
    }
}

/// Result of a shipping attempt.
#[derive(Debug)]
pub enum ShipResult {
    /// Successfully shipped. `server_timing` is populated when the Runtime
    /// Host returned phase-1 instrumentation headers; `None` against older
    /// servers.
    Ok { server_timing: ServerIngestTiming },
    /// Rate limited and retries exhausted. Should spool for later.
    RateLimited,
    /// Server error (5xx). Should spool for later.
    ServerError(u16, String),
    /// Server explicitly rejected reconstructable archive work due to pressure.
    ServerBackpressure(ServerBackpressureDetail),
    /// Request was rejected because the payload itself is invalid.
    PayloadRejected(u16, String),
    /// Payload is valid but too large for the current server/proxy limits.
    PayloadTooLarge(String),
    /// Auth/config/wrong-host style client error. Should stay replayable.
    RetryableClientError(u16, String),
    /// Connection error (DNS, timeout, refused). Should spool for later.
    ConnectError(ConnectErrorDetail),
}

/// HTTP client with connection pooling and retry logic.
#[derive(Clone)]
pub struct ShipperClient {
    client: reqwest::Client,
    ingest_url: String,
    max_retries_429: u32,
    base_backoff: f64,
}

impl ShipperClient {
    /// Create a new client with specific compression algorithm.
    pub fn with_compression(config: &ShipperConfig, compression: CompressionAlgo) -> Result<Self> {
        let mut default_headers = HeaderMap::new();
        default_headers.insert(
            USER_AGENT,
            HeaderValue::from_str(&format!("longhouse-engine/{}", env!("CARGO_PKG_VERSION")))
                .context("invalid user-agent header value")?,
        );
        default_headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        default_headers.insert(
            CONTENT_ENCODING,
            HeaderValue::from_static(content_encoding(compression)),
        );

        if let Some(ref token) = config.api_token {
            default_headers.insert(
                "X-Agents-Token",
                HeaderValue::from_str(token).context("invalid token header value")?,
            );
        }

        let client = reqwest::Client::builder()
            .default_headers(default_headers)
            .timeout(Duration::from_secs(config.timeout_seconds))
            .connect_timeout(Duration::from_secs(5))
            .pool_idle_timeout(Duration::from_secs(120))
            .pool_max_idle_per_host(4)
            .tcp_keepalive(Duration::from_secs(30))
            .build()
            .context("building HTTP client")?;

        let ingest_url = format!("{}/api/agents/ingest", config.api_url.trim_end_matches('/'));

        Ok(Self {
            client,
            ingest_url,
            max_retries_429: config.max_retries_429,
            base_backoff: config.base_backoff_seconds,
        })
    }

    /// Ship a gzip-compressed payload. Handles 429 retries internally.
    pub async fn ship(&self, compressed_payload: Vec<u8>) -> ShipResult {
        self.ship_with_trace_and_timeout(compressed_payload, None, None)
            .await
    }

    /// Ship a compressed payload with an optional request timeout override.
    pub async fn ship_with_trace_and_timeout(
        &self,
        compressed_payload: Vec<u8>,
        trace_header: Option<&str>,
        request_timeout: Option<Duration>,
    ) -> ShipResult {
        let mut retries = 0u32;
        let mut backoff = self.base_backoff;

        loop {
            let mut request = self
                .client
                .post(&self.ingest_url)
                .body(compressed_payload.clone());
            if let Some(trace_header) = trace_header {
                request = request.header(SHIP_TRACE_HEADER, trace_header);
            }
            if let Some(request_timeout) = request_timeout {
                request = request.timeout(request_timeout);
            }
            let result = request.send().await;

            match result {
                Err(e) => {
                    return ShipResult::ConnectError(classify_connect_error(&e));
                }
                Ok(response) => {
                    let status = response.status().as_u16();

                    match status {
                        200..=299 => {
                            let server_timing = parse_server_timing(response.headers());
                            return ShipResult::Ok { server_timing };
                        }
                        429 => {
                            if retries >= self.max_retries_429 {
                                tracing::warn!("Rate limited after {} retries, giving up", retries);
                                return ShipResult::RateLimited;
                            }

                            let retry_after_seconds = parse_retry_after_seconds(response.headers());
                            let wait = rate_limit_retry_wait_seconds(
                                retry_after_seconds,
                                backoff,
                                rand::thread_rng().gen::<f64>(),
                            );

                            tracing::info!(
                                "Rate limited (429), retry {}/{}, waiting {:.1}s",
                                retries + 1,
                                self.max_retries_429,
                                wait
                            );

                            tokio::time::sleep(Duration::from_secs_f64(wait)).await;
                            retries += 1;
                            backoff *= 2.0;
                        }
                        401 | 403 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::RetryableClientError(status, body);
                        }
                        400 | 422 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::PayloadRejected(status, body);
                        }
                        426 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::RetryableClientError(status, body);
                        }
                        413 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::PayloadTooLarge(body);
                        }
                        400..=499 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::RetryableClientError(status, body);
                        }
                        500..=599 => {
                            let headers = response.headers().clone();
                            let body = response.text().await.unwrap_or_default();
                            if let Some(detail) =
                                parse_server_backpressure(status, &headers, body.clone())
                            {
                                return ShipResult::ServerBackpressure(detail);
                            }
                            return ShipResult::ServerError(status, body);
                        }
                        _ => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::RetryableClientError(status, body);
                        }
                    }
                }
            }
        }
    }

    /// POST a small JSON payload with an optional request-level timeout.
    pub async fn post_json_with_timeout(
        &self,
        path_suffix: &str,
        body: Vec<u8>,
        request_timeout: Option<Duration>,
    ) -> Result<()> {
        let url = self.ingest_url.replace("/api/agents/ingest", path_suffix);
        let mut request = self
            .client
            .post(&url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            // Remove Content-Encoding for uncompressed requests
            .header(reqwest::header::CONTENT_ENCODING, "identity")
            .body(body);
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let resp = request.send().await.context("POST failed")?;
        let status = resp.status();
        if status.is_success() {
            return Ok(());
        }
        let headers = resp.headers().clone();
        let body = resp.text().await.unwrap_or_default();
        if let Some(detail) =
            parse_server_write_backpressure(status.as_u16(), &headers, body.clone())
        {
            anyhow::bail!(
                "POST returned Runtime Host write backpressure: kind={} lane={} retry_after_seconds={:?}: {}",
                detail.kind,
                detail.lane.as_deref().unwrap_or("unknown"),
                detail.retry_after_seconds,
                detail.body
            );
        }
        anyhow::bail!("POST returned {status}: {body}");
    }

    /// POST JSON and decode a JSON response with an optional request timeout.
    pub async fn post_json_decode_with_timeout<T: DeserializeOwned>(
        &self,
        path_suffix: &str,
        body: Vec<u8>,
        request_timeout: Option<Duration>,
    ) -> Result<T> {
        let url = self.ingest_url.replace("/api/agents/ingest", path_suffix);
        let mut request = self
            .client
            .post(&url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header(reqwest::header::CONTENT_ENCODING, "identity")
            .body(body);
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let resp = request.send().await.context("POST failed")?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("POST returned {status}: {body}");
        }
        resp.json::<T>().await.context("POST returned invalid JSON")
    }

    /// PUT raw bytes to a machine-authenticated route.
    pub async fn put_bytes_with_timeout(
        &self,
        path_suffix: &str,
        content_type: &str,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
        request_timeout: Option<Duration>,
    ) -> Result<()> {
        let url = self.ingest_url.replace("/api/agents/ingest", path_suffix);
        let mut request = self
            .client
            .put(&url)
            .header(reqwest::header::CONTENT_TYPE, content_type)
            .header(reqwest::header::CONTENT_ENCODING, "identity")
            .body(body);
        for (name, value) in headers {
            request = request.header(name, value);
        }
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let resp = request.send().await.context("PUT failed")?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("PUT returned {status}: {body}");
        }
        Ok(())
    }

    /// GET a small JSON response with an optional request-level timeout.
    pub async fn get_json_with_timeout<T: DeserializeOwned>(
        &self,
        path_suffix: &str,
        request_timeout: Option<Duration>,
    ) -> Result<T> {
        let url = self.ingest_url.replace("/api/agents/ingest", path_suffix);
        let mut request = self.client.get(&url);
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let resp = request.send().await.context("GET failed")?;
        let resp = resp.error_for_status().context("GET returned non-2xx")?;
        resp.json::<T>().await.context("GET returned invalid JSON")
    }

    /// Negotiate storage-v2 once at process startup. Only a literal 404 means
    /// an older Runtime Host; auth, transport, and server failures are errors.
    pub async fn storage_v2_capabilities(
        &self,
        machine_id: &str,
        request_timeout: Option<Duration>,
    ) -> Result<Option<StorageV2Capabilities>> {
        let url = self
            .ingest_url
            .replace("/api/agents/ingest", STORAGE_V2_CAPABILITIES_PATH);
        let mut request = self
            .client
            .get(&url)
            .header("X-Longhouse-Machine-Id", machine_id);
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let response = request
            .send()
            .await
            .context("storage-v2 capability request failed")?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let response = response
            .error_for_status()
            .context("storage-v2 capability request returned non-2xx")?;
        let capabilities = response
            .json::<StorageV2Capabilities>()
            .await
            .context("storage-v2 capability response is invalid")?;
        capabilities.validate(machine_id)?;
        Ok(Some(capabilities))
    }

    #[allow(dead_code)] // Kept as the typed convenience boundary for callers/tests.
    pub async fn ship_storage_v2_envelope(
        &self,
        ingest_path: &str,
        lane: &str,
        envelope: &StorageV2Envelope,
        request_timeout: Option<Duration>,
    ) -> Result<StorageV2Receipt> {
        let body = serde_json::to_vec(envelope).context("serializing storage-v2 envelope")?;
        self.ship_storage_v2_body(
            ingest_path,
            lane,
            body,
            &envelope.expected_envelope_id,
            request_timeout,
        )
        .await
    }

    /// Ship a previously persisted storage-v2 request body without
    /// reserializing it. Ambiguous outcomes must retry these exact bytes.
    pub async fn ship_storage_v2_body(
        &self,
        ingest_path: &str,
        lane: &str,
        body: Vec<u8>,
        expected_envelope_id: &str,
        request_timeout: Option<Duration>,
    ) -> Result<StorageV2Receipt> {
        if lane != "live" && lane != "repair" {
            anyhow::bail!("storage-v2 lane must be live or repair");
        }
        let url = self.ingest_url.replace("/api/agents/ingest", ingest_path);
        let mut request = self
            .client
            .post(&url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header(reqwest::header::CONTENT_ENCODING, "identity")
            .header(STORAGE_V2_LANE_HEADER, lane)
            .body(body);
        if let Some(request_timeout) = request_timeout {
            request = request.timeout(request_timeout);
        }
        let response = request
            .send()
            .await
            .context("storage-v2 envelope POST failed")?;
        let status = response.status();
        if !status.is_success() {
            let headers = response.headers().clone();
            let body = response.text().await.unwrap_or_default();
            if let Some(backpressure) =
                parse_storage_v2_backpressure(status.as_u16(), &headers, &body, lane)
            {
                return Err(backpressure.into());
            }
            anyhow::bail!("storage-v2 envelope POST returned {status}: {body}");
        }
        let receipt = response
            .json::<StorageV2Receipt>()
            .await
            .context("storage-v2 envelope receipt is invalid JSON")?;
        receipt.validate(expected_envelope_id)?;
        Ok(receipt)
    }

    /// Get the ingest URL (for logging).
    pub fn ingest_url(&self) -> &str {
        &self.ingest_url
    }

    /// Check if the API is reachable (health check).
    pub async fn health_check(&self) -> Result<bool> {
        let health_url = self.ingest_url.replace("/api/agents/ingest", "/api/health");
        match self.client.get(&health_url).send().await {
            Ok(resp) => Ok(resp.status().is_success()),
            Err(_) => Ok(false),
        }
    }
}

fn parse_storage_v2_backpressure(
    status_code: u16,
    headers: &reqwest::header::HeaderMap,
    body: &str,
    lane: &str,
) -> Option<StorageV2Backpressure> {
    let typed_busy = parse_header_string(headers, STORAGE_BACKPRESSURE_HEADER)
        .is_some_and(|kind| kind == "storage_lane_busy");
    if status_code != 503 || (!typed_busy && !body.contains("storage_lane_busy")) {
        return None;
    }
    Some(StorageV2Backpressure {
        lane: lane.to_string(),
        retry_after: parse_retry_after_seconds(headers)
            .map(Duration::from_secs_f64)
            .unwrap_or(Duration::from_secs(5)),
    })
}

fn parse_server_timing(headers: &reqwest::header::HeaderMap) -> ServerIngestTiming {
    fn parse_f64(headers: &reqwest::header::HeaderMap, name: &str) -> Option<f64> {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite())
    }
    fn parse_u64(headers: &reqwest::header::HeaderMap, name: &str) -> Option<u64> {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.trim().parse::<u64>().ok())
    }
    fn parse_stage_map(
        headers: &reqwest::header::HeaderMap,
        name: &str,
    ) -> Option<BTreeMap<String, f64>> {
        let raw = headers.get(name).and_then(|v| v.to_str().ok())?;
        let parsed: BTreeMap<String, f64> = serde_json::from_str(raw).ok()?;
        let filtered: BTreeMap<String, f64> = parsed
            .into_iter()
            .filter(|(key, value)| !key.trim().is_empty() && value.is_finite() && *value >= 0.0)
            .collect();
        (!filtered.is_empty()).then_some(filtered)
    }
    ServerIngestTiming {
        queue_wait_ms: parse_f64(headers, "X-Ingest-Queue-Wait-Ms"),
        exec_ms: parse_f64(headers, "X-Ingest-Exec-Ms"),
        commit_count: parse_u64(headers, "X-Ingest-Commit-Count"),
        commit_ms: parse_f64(headers, "X-Ingest-Commit-Ms"),
        chunk_size: parse_u64(headers, "X-Ingest-Chunk-Size"),
        store_stage_ms: parse_stage_map(headers, "X-Ingest-Store-Stage-Ms"),
        label: headers
            .get("X-Ingest-Label")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        lane: headers
            .get("X-Ingest-Lane")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        admission_state: headers
            .get("X-Ingest-Admission-State")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
    }
}

fn parse_header_string(headers: &reqwest::header::HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn parse_retry_after_seconds(headers: &reqwest::header::HeaderMap) -> Option<f64> {
    headers
        .get("Retry-After")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v > 0.0)
}

fn rate_limit_retry_wait_seconds(
    retry_after_seconds: Option<f64>,
    backoff_seconds: f64,
    jitter_seed: f64,
) -> f64 {
    let jitter_seed = jitter_seed.clamp(0.0, 1.0);
    if let Some(retry_after) = retry_after_seconds {
        let jitter_window = (retry_after * 0.10).clamp(0.1, 5.0);
        return retry_after + (jitter_window * jitter_seed);
    }

    let jitter_factor = 0.5 + jitter_seed * 0.5;
    (backoff_seconds * jitter_factor).min(30.0)
}

fn parse_server_backpressure(
    status_code: u16,
    headers: &reqwest::header::HeaderMap,
    body: String,
) -> Option<ServerBackpressureDetail> {
    if status_code != 503 {
        return None;
    }
    let header_kind = parse_header_string(headers, INGEST_BACKPRESSURE_HEADER)
        .or_else(|| parse_header_string(headers, INGEST_ERROR_KIND_HEADER));
    let legacy_body_match = body.contains("Archive ingest backlog is throttled");
    let kind = match header_kind.as_deref() {
        Some(ARCHIVE_INGEST_BACKPRESSURE_KIND) => ARCHIVE_INGEST_BACKPRESSURE_KIND,
        Some(LIVE_INGEST_BACKPRESSURE_KIND) => LIVE_INGEST_BACKPRESSURE_KIND,
        _ if legacy_body_match => ARCHIVE_INGEST_BACKPRESSURE_KIND,
        _ => return None,
    };
    Some(ServerBackpressureDetail {
        status_code,
        kind,
        body,
        lane: parse_header_string(headers, INGEST_LANE_HEADER),
        retry_after_seconds: parse_retry_after_seconds(headers),
    })
}

fn parse_server_write_backpressure(
    status_code: u16,
    headers: &reqwest::header::HeaderMap,
    body: String,
) -> Option<ServerBackpressureDetail> {
    if status_code != 503 {
        return None;
    }
    let header_kind = parse_header_string(headers, WRITE_BACKPRESSURE_HEADER)
        .or_else(|| parse_header_string(headers, WRITE_ERROR_KIND_HEADER));
    let kind = match header_kind.as_deref() {
        Some(HOT_WRITE_BACKPRESSURE_KIND) => HOT_WRITE_BACKPRESSURE_KIND,
        _ => return None,
    };
    Some(ServerBackpressureDetail {
        status_code,
        kind,
        body,
        lane: parse_header_string(headers, WRITE_LANE_HEADER),
        retry_after_seconds: parse_retry_after_seconds(headers),
    })
}

fn classify_connect_error(error: &reqwest::Error) -> ConnectErrorDetail {
    ConnectErrorDetail {
        kind: classify_connect_error_kind(
            error.is_timeout(),
            error.is_connect(),
            error.is_request(),
            &error.to_string(),
        ),
        message: error.to_string(),
    }
}

fn classify_connect_error_kind(
    is_timeout: bool,
    is_connect: bool,
    is_request: bool,
    message: &str,
) -> &'static str {
    let lower = message.to_ascii_lowercase();
    if is_timeout || lower.contains("timed out") || lower.contains("timeout") {
        return "timeout";
    }
    if lower.contains("dns")
        || lower.contains("resolve")
        || lower.contains("name or service not known")
        || lower.contains("nodename nor servname")
        || lower.contains("no such host")
    {
        return "dns";
    }
    if lower.contains("connection refused")
        || lower.contains("os error 61")
        || lower.contains("os error 111")
    {
        return "connection_refused";
    }
    if lower.contains("connection reset")
        || lower.contains("connection closed")
        || lower.contains("broken pipe")
    {
        return "connection_closed";
    }
    if lower.contains("tls") || lower.contains("ssl") || lower.contains("certificate") {
        return "tls";
    }
    if is_connect {
        return "connect";
    }
    if is_request {
        return "request";
    }
    "network"
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use rand::Rng;

    use reqwest::header::{HeaderMap, HeaderValue};

    use super::{
        classify_connect_error_kind, parse_server_backpressure, parse_server_timing,
        parse_server_write_backpressure, parse_storage_v2_backpressure,
        rate_limit_retry_wait_seconds, ShipResult,
    };

    fn classify_status(status: u16, body: &str) -> ShipResult {
        match status {
            400 | 422 => ShipResult::PayloadRejected(status, body.to_string()),
            426 => ShipResult::RetryableClientError(status, body.to_string()),
            413 => ShipResult::PayloadTooLarge(body.to_string()),
            401 | 403 | 400..=499 => ShipResult::RetryableClientError(status, body.to_string()),
            500..=599 => ShipResult::ServerError(status, body.to_string()),
            _ => ShipResult::RetryableClientError(status, body.to_string()),
        }
    }

    #[test]
    fn test_429_without_retry_after_keeps_legacy_bounded_jitter() {
        let base_wait = 20.0_f64;
        let mut rng = rand::thread_rng();

        for _ in 0..1000 {
            let wait = rate_limit_retry_wait_seconds(None, base_wait, rng.gen::<f64>());

            assert!(
                wait >= base_wait * 0.5,
                "wait {:.2} should be >= {:.2}",
                wait,
                base_wait * 0.5
            );
            assert!(wait <= 30.0, "wait {:.2} should be capped at 30s", wait);
        }

        let large_base = 100.0_f64;
        let wait = rate_limit_retry_wait_seconds(None, large_base, rng.gen::<f64>());
        assert_eq!(wait, 30.0, "Large base_wait should be capped at 30s");
    }

    #[test]
    fn test_429_retry_after_is_floor_with_jitter_on_top() {
        assert_eq!(rate_limit_retry_wait_seconds(Some(120.0), 1.0, 0.0), 120.0);
        assert_eq!(rate_limit_retry_wait_seconds(Some(120.0), 1.0, 1.0), 125.0);
        assert_eq!(rate_limit_retry_wait_seconds(Some(2.0), 1.0, 0.5), 2.1);
    }

    #[test]
    fn storage_v2_busy_lane_is_typed_backpressure_with_retry_floor() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Longhouse-Storage-Backpressure",
            HeaderValue::from_static("storage_lane_busy"),
        );
        headers.insert("Retry-After", HeaderValue::from_static("7"));
        let detail = parse_storage_v2_backpressure(503, &headers, "{}", "repair")
            .expect("typed storage saturation should be backpressure");
        assert_eq!(detail.lane, "repair");
        assert_eq!(detail.retry_after, Duration::from_secs(7));
        assert!(parse_storage_v2_backpressure(500, &headers, "{}", "repair").is_none());
    }

    #[test]
    fn test_classify_payload_rejections_vs_retryable_client_errors() {
        assert!(matches!(
            classify_status(400, "invalid json"),
            ShipResult::PayloadRejected(400, _)
        ));
        assert!(matches!(
            classify_status(422, "invalid payload"),
            ShipResult::PayloadRejected(422, _)
        ));
        assert!(matches!(
            classify_status(426, "storage v2 required"),
            ShipResult::RetryableClientError(426, _)
        ));
        assert!(matches!(
            classify_status(413, "too large"),
            ShipResult::PayloadTooLarge(_)
        ));
        assert!(matches!(
            classify_status(401, "bad token"),
            ShipResult::RetryableClientError(401, _)
        ));
        assert!(matches!(
            classify_status(405, "method not allowed"),
            ShipResult::RetryableClientError(405, _)
        ));
    }

    #[test]
    fn test_classify_connect_error_kind_from_reqwest_shape_and_message() {
        assert_eq!(
            classify_connect_error_kind(true, true, false, "operation timed out"),
            "timeout"
        );
        assert_eq!(
            classify_connect_error_kind(false, true, false, "dns error: failed to lookup address"),
            "dns"
        );
        assert_eq!(
            classify_connect_error_kind(
                false,
                true,
                false,
                "tcp connect error: Connection refused (os error 61)"
            ),
            "connection_refused"
        );
        assert_eq!(
            classify_connect_error_kind(
                false,
                true,
                false,
                "connection closed before message completed"
            ),
            "connection_closed"
        );
        assert_eq!(
            classify_connect_error_kind(false, false, true, "builder error"),
            "request"
        );
    }

    #[test]
    fn test_parse_server_timing_full_headers() {
        let mut headers = HeaderMap::new();
        headers.insert("X-Ingest-Queue-Wait-Ms", HeaderValue::from_static("12.5"));
        headers.insert("X-Ingest-Exec-Ms", HeaderValue::from_static("48.2"));
        headers.insert("X-Ingest-Commit-Count", HeaderValue::from_static("3"));
        headers.insert("X-Ingest-Commit-Ms", HeaderValue::from_static("24.5"));
        headers.insert("X-Ingest-Chunk-Size", HeaderValue::from_static("100"));
        headers.insert(
            "X-Ingest-Store-Stage-Ms",
            HeaderValue::from_static("{\"provider_event_observations\":42.5,\"total\":123.0}"),
        );
        headers.insert("X-Ingest-Label", HeaderValue::from_static("ingest-replay"));
        headers.insert("X-Ingest-Lane", HeaderValue::from_static("archive"));
        headers.insert(
            "X-Ingest-Admission-State",
            HeaderValue::from_static("archive_slot_acquired"),
        );

        let timing = parse_server_timing(&headers);
        assert_eq!(timing.queue_wait_ms, Some(12.5));
        assert_eq!(timing.exec_ms, Some(48.2));
        assert_eq!(timing.commit_count, Some(3));
        assert_eq!(timing.commit_ms, Some(24.5));
        assert_eq!(timing.chunk_size, Some(100));
        assert_eq!(
            timing
                .store_stage_ms
                .as_ref()
                .and_then(|map| map.get("total")),
            Some(&123.0)
        );
        assert_eq!(timing.label.as_deref(), Some("ingest-replay"));
        assert_eq!(timing.lane.as_deref(), Some("archive"));
        assert_eq!(
            timing.admission_state.as_deref(),
            Some("archive_slot_acquired")
        );
        assert!(timing.is_observed());
    }

    #[test]
    fn test_parse_server_timing_missing_headers_returns_unobserved() {
        let headers = HeaderMap::new();
        let timing = parse_server_timing(&headers);
        assert_eq!(timing, super::ServerIngestTiming::default());
        assert!(!timing.is_observed());
    }

    #[test]
    fn test_parse_server_timing_garbage_values_drop_silently() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Ingest-Queue-Wait-Ms",
            HeaderValue::from_static("not-a-number"),
        );
        headers.insert("X-Ingest-Exec-Ms", HeaderValue::from_static("inf"));
        headers.insert("X-Ingest-Commit-Count", HeaderValue::from_static("-1"));
        headers.insert("X-Ingest-Commit-Ms", HeaderValue::from_static("nan"));
        headers.insert(
            "X-Ingest-Chunk-Size",
            HeaderValue::from_static("one hundred"),
        );
        headers.insert(
            "X-Ingest-Store-Stage-Ms",
            HeaderValue::from_static("{\"bad\":-1,\"nan\":NaN}"),
        );
        headers.insert("X-Ingest-Label", HeaderValue::from_static(""));
        headers.insert("X-Ingest-Lane", HeaderValue::from_static(""));
        headers.insert("X-Ingest-Admission-State", HeaderValue::from_static(""));
        let timing = parse_server_timing(&headers);
        assert_eq!(timing.queue_wait_ms, None);
        // "inf" parses to f64::INFINITY then is filtered by is_finite
        assert_eq!(timing.exec_ms, None);
        assert_eq!(timing.commit_count, None);
        assert_eq!(timing.commit_ms, None);
        assert_eq!(timing.chunk_size, None);
        assert_eq!(timing.store_stage_ms, None);
        assert_eq!(timing.label, None);
        assert_eq!(timing.lane, None);
        assert_eq!(timing.admission_state, None);
        assert!(!timing.is_observed());
    }

    #[test]
    fn test_parse_server_backpressure_from_typed_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Ingest-Backpressure",
            HeaderValue::from_static("archive_ingest_backpressure"),
        );
        headers.insert("X-Ingest-Lane", HeaderValue::from_static("archive"));
        headers.insert("Retry-After", HeaderValue::from_static("5"));

        let detail =
            parse_server_backpressure(503, &headers, "{\"detail\":\"throttled\"}".to_string())
                .expect("typed backpressure should parse");

        assert_eq!(detail.status_code, 503);
        assert_eq!(detail.kind, "archive_ingest_backpressure");
        assert_eq!(detail.lane.as_deref(), Some("archive"));
        assert_eq!(detail.retry_after_seconds, Some(5.0));
    }

    #[test]
    fn test_parse_server_backpressure_from_live_typed_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Ingest-Backpressure",
            HeaderValue::from_static("live_ingest_backpressure"),
        );
        headers.insert("X-Ingest-Lane", HeaderValue::from_static("live"));
        headers.insert("Retry-After", HeaderValue::from_static("7"));

        let detail =
            parse_server_backpressure(503, &headers, "{\"detail\":\"live throttled\"}".to_string())
                .expect("typed live backpressure should parse");

        assert_eq!(detail.status_code, 503);
        assert_eq!(detail.kind, "live_ingest_backpressure");
        assert_eq!(detail.lane.as_deref(), Some("live"));
        assert_eq!(detail.retry_after_seconds, Some(7.0));
    }

    #[test]
    fn test_parse_server_backpressure_keeps_legacy_body_match() {
        let headers = HeaderMap::new();
        let detail = parse_server_backpressure(
            503,
            &headers,
            "{\"detail\":\"Archive ingest backlog is throttled; retry shortly\"}".to_string(),
        )
        .expect("legacy archive backpressure body should parse");

        assert_eq!(detail.kind, "archive_ingest_backpressure");
        assert_eq!(detail.retry_after_seconds, None);
    }

    #[test]
    fn test_parse_server_backpressure_ignores_generic_503() {
        let headers = HeaderMap::new();
        assert!(
            parse_server_backpressure(503, &headers, "upstream unavailable".to_string()).is_none()
        );
    }

    #[test]
    fn test_parse_server_write_backpressure_from_typed_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-Longhouse-Write-Backpressure",
            HeaderValue::from_static("hot_write_backpressure"),
        );
        headers.insert("X-Longhouse-Write-Lane", HeaderValue::from_static("hot"));
        headers.insert("Retry-After", HeaderValue::from_static("2"));

        let detail =
            parse_server_write_backpressure(503, &headers, "{\"detail\":\"busy\"}".to_string())
                .expect("typed hot write backpressure should parse");

        assert_eq!(detail.status_code, 503);
        assert_eq!(detail.kind, "hot_write_backpressure");
        assert_eq!(detail.lane.as_deref(), Some("hot"));
        assert_eq!(detail.retry_after_seconds, Some(2.0));
    }

    #[test]
    fn test_parse_server_write_backpressure_ignores_ingest_and_generic_503() {
        let mut ingest_headers = HeaderMap::new();
        ingest_headers.insert(
            "X-Ingest-Backpressure",
            HeaderValue::from_static("live_ingest_backpressure"),
        );
        assert!(parse_server_write_backpressure(503, &ingest_headers, "{}".to_string()).is_none());

        let generic_headers = HeaderMap::new();
        assert!(parse_server_write_backpressure(503, &generic_headers, "{}".to_string()).is_none());
    }
}
