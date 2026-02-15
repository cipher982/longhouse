//! HTTP client for shipping compressed payloads to the Longhouse API.
//!
//! POST `{api_url}/api/agents/ingest` with gzip-compressed JSON body.
//! Handles 429 rate limiting with exponential backoff + Retry-After.

use std::time::Duration;

use anyhow::{Context, Result};
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_ENCODING, CONTENT_TYPE};

use crate::config::ShipperConfig;

/// Result of a shipping attempt.
#[derive(Debug)]
pub enum ShipResult {
    /// Successfully shipped. Contains the response body.
    Ok(serde_json::Value),
    /// Rate limited and retries exhausted. Should spool for later.
    RateLimited,
    /// Server error (5xx). Should spool for later.
    ServerError(u16, String),
    /// Client error (4xx, not 429). Bad payload — skip, don't spool.
    ClientError(u16, String),
    /// Connection error (DNS, timeout, refused). Should spool for later.
    ConnectError(String),
}

/// HTTP client with connection pooling and retry logic.
pub struct ShipperClient {
    client: reqwest::Client,
    ingest_url: String,
    api_token: Option<String>,
    max_retries_429: u32,
    base_backoff: f64,
}

impl ShipperClient {
    /// Create a new client from config.
    pub fn new(config: &ShipperConfig) -> Result<Self> {
        let mut default_headers = HeaderMap::new();
        default_headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        default_headers.insert(CONTENT_ENCODING, HeaderValue::from_static("gzip"));

        if let Some(ref token) = config.api_token {
            default_headers.insert(
                "X-Agents-Token",
                HeaderValue::from_str(token).context("invalid token header value")?,
            );
        }

        let client = reqwest::Client::builder()
            .default_headers(default_headers)
            .timeout(Duration::from_secs(config.timeout_seconds))
            .pool_max_idle_per_host(4)
            .build()
            .context("building HTTP client")?;

        let ingest_url = format!(
            "{}/api/agents/ingest",
            config.api_url.trim_end_matches('/')
        );

        Ok(Self {
            client,
            ingest_url,
            api_token: config.api_token.clone(),
            max_retries_429: config.max_retries_429,
            base_backoff: config.base_backoff_seconds,
        })
    }

    /// Ship a gzip-compressed payload. Handles 429 retries internally.
    pub async fn ship(&self, compressed_payload: Vec<u8>) -> ShipResult {
        let mut retries = 0u32;
        let mut backoff = self.base_backoff;

        loop {
            let result = self
                .client
                .post(&self.ingest_url)
                .body(compressed_payload.clone())
                .send()
                .await;

            match result {
                Err(e) => {
                    return ShipResult::ConnectError(e.to_string());
                }
                Ok(response) => {
                    let status = response.status().as_u16();

                    match status {
                        200..=299 => {
                            let body = response
                                .json::<serde_json::Value>()
                                .await
                                .unwrap_or(serde_json::Value::Null);
                            return ShipResult::Ok(body);
                        }
                        429 => {
                            if retries >= self.max_retries_429 {
                                tracing::warn!(
                                    "Rate limited after {} retries, giving up",
                                    retries
                                );
                                return ShipResult::RateLimited;
                            }

                            // Check Retry-After header
                            let wait = response
                                .headers()
                                .get("Retry-After")
                                .and_then(|v| v.to_str().ok())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(backoff);

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
                            return ShipResult::ClientError(status, body);
                        }
                        400..=499 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::ClientError(status, body);
                        }
                        500..=599 => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::ServerError(status, body);
                        }
                        _ => {
                            let body = response.text().await.unwrap_or_default();
                            return ShipResult::ClientError(status, body);
                        }
                    }
                }
            }
        }
    }

    /// Get the ingest URL (for logging).
    pub fn ingest_url(&self) -> &str {
        &self.ingest_url
    }

    /// Check if the API is reachable (health check).
    pub async fn health_check(&self) -> Result<bool> {
        let health_url = self
            .ingest_url
            .replace("/api/agents/ingest", "/api/health");
        match self.client.get(&health_url).send().await {
            Ok(resp) => Ok(resp.status().is_success()),
            Err(_) => Ok(false),
        }
    }
}

/// Read API URL from the standard location.
pub fn read_api_url() -> Result<String> {
    let config = ShipperConfig::from_env()?;
    Ok(config.api_url)
}

/// Check if the shipper has valid config (URL + token).
pub fn has_valid_config() -> bool {
    match ShipperConfig::from_env() {
        Ok(config) => {
            !config.api_url.is_empty() && config.api_token.is_some()
        }
        Err(_) => false,
    }
}
