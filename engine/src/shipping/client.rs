//! HTTP client for shipping compressed payloads to the Longhouse API.
//!
//! POST `{api_url}/api/agents/ingest` with gzip-compressed JSON body.
//! Handles 429 rate limiting with exponential backoff + Retry-After.

use std::time::Duration;

use anyhow::{Context, Result};
use rand::Rng;
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_ENCODING, CONTENT_TYPE, USER_AGENT};

use crate::config::ShipperConfig;
use crate::pipeline::compressor::{content_encoding, CompressionAlgo};

const SHIP_TRACE_HEADER: &str = "X-Longhouse-Ship-Trace";

/// Structured details for a network-layer ingest failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectErrorDetail {
    pub kind: &'static str,
    pub message: String,
}

/// Server-side ingest timing parsed from response headers.
///
/// Phase 1 instrumentation: the Runtime Host emits `X-Ingest-Queue-Wait-Ms`,
/// `X-Ingest-Exec-Ms`, and `X-Ingest-Label` on every successful ingest so
/// the engine can adapt concurrency without re-instrumenting in phase 2.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ServerIngestTiming {
    pub queue_wait_ms: Option<f64>,
    pub exec_ms: Option<f64>,
    pub label: Option<String>,
}

impl ServerIngestTiming {
    /// True if the server returned at least one of the phase-1 headers.
    pub fn is_observed(&self) -> bool {
        self.queue_wait_ms.is_some() || self.exec_ms.is_some() || self.label.is_some()
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

                            // Check Retry-After header
                            let base_wait = response
                                .headers()
                                .get("Retry-After")
                                .and_then(|v| v.to_str().ok())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(backoff);

                            // Add jitter (50%–100% of base_wait) and cap at 30s
                            let jitter_factor = 0.5 + rand::thread_rng().gen::<f64>() * 0.5;
                            let wait = (base_wait * jitter_factor).min(30.0);

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
                        413 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::PayloadTooLarge(body);
                        }
                        400..=499 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::RetryableClientError(status, body);
                        }
                        500..=599 => {
                            let body = response.text().await.unwrap_or_default();
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
        resp.error_for_status().context("POST returned non-2xx")?;
        Ok(())
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

fn parse_server_timing(headers: &reqwest::header::HeaderMap) -> ServerIngestTiming {
    fn parse_f64(headers: &reqwest::header::HeaderMap, name: &str) -> Option<f64> {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite())
    }
    ServerIngestTiming {
        queue_wait_ms: parse_f64(headers, "X-Ingest-Queue-Wait-Ms"),
        exec_ms: parse_f64(headers, "X-Ingest-Exec-Ms"),
        label: headers
            .get("X-Ingest-Label")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
    }
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
    use rand::Rng;

    use reqwest::header::{HeaderMap, HeaderValue};

    use super::{classify_connect_error_kind, parse_server_timing, ShipResult};

    fn classify_status(status: u16, body: &str) -> ShipResult {
        match status {
            400 | 422 => ShipResult::PayloadRejected(status, body.to_string()),
            413 => ShipResult::PayloadTooLarge(body.to_string()),
            401 | 403 | 400..=499 => ShipResult::RetryableClientError(status, body.to_string()),
            500..=599 => ShipResult::ServerError(status, body.to_string()),
            _ => ShipResult::RetryableClientError(status, body.to_string()),
        }
    }

    #[test]
    fn test_429_jitter_in_range() {
        // Verify the jitter formula produces values in [0.5 * base, base] and <= 30s
        let base_wait = 20.0_f64;
        let mut rng = rand::thread_rng();

        for _ in 0..1000 {
            let jitter_factor = 0.5 + rng.gen::<f64>() * 0.5;
            let wait = (base_wait * jitter_factor).min(30.0);

            assert!(
                wait >= base_wait * 0.5,
                "wait {:.2} should be >= {:.2}",
                wait,
                base_wait * 0.5
            );
            assert!(wait <= 30.0, "wait {:.2} should be capped at 30s", wait);
        }

        // Also verify cap works for large base_wait
        let large_base = 100.0_f64;
        let jitter_factor = 0.5 + rng.gen::<f64>() * 0.5;
        let wait = (large_base * jitter_factor).min(30.0);
        assert_eq!(wait, 30.0, "Large base_wait should be capped at 30s");
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
        headers.insert("X-Ingest-Label", HeaderValue::from_static("ingest-replay"));

        let timing = parse_server_timing(&headers);
        assert_eq!(timing.queue_wait_ms, Some(12.5));
        assert_eq!(timing.exec_ms, Some(48.2));
        assert_eq!(timing.label.as_deref(), Some("ingest-replay"));
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
        headers.insert("X-Ingest-Label", HeaderValue::from_static(""));
        let timing = parse_server_timing(&headers);
        assert_eq!(timing.queue_wait_ms, None);
        // "inf" parses to f64::INFINITY then is filtered by is_finite
        assert_eq!(timing.exec_ms, None);
        assert_eq!(timing.label, None);
        assert!(!timing.is_observed());
    }
}
