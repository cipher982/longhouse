//! A Longhouse-owned `opencode serve` and its authenticated local API.
//!
//! Console starts one server per turn and drives the turn over HTTP (see
//! `opencode_run`). The server is a process Longhouse owns end to end: it picks
//! the port and the password, keeps the process group, and stops the group.

use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use reqwest::{Client, Method, Url};
use serde_json::Value;
use tokio::process::Child;

const READY_TIMEOUT: Duration = Duration::from_secs(30);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
pub(crate) const USERNAME: &str = "opencode";

/// Where a server listens and how to authenticate to it. Kept beside the run's
/// claim so a restarted engine can reconnect to the same server.
#[derive(Clone, Debug)]
pub(crate) struct OpenCodeServer {
    pub url: String,
    pub username: String,
    pub password: String,
    /// The workspace directory every request is scoped to.
    pub directory: String,
}

/// A non-2xx answer. Kept typed so a caller can tell "not found" from "down".
#[derive(Debug)]
pub(crate) struct HttpStatusError {
    pub status: u16,
    pub body: String,
}

impl std::fmt::Display for HttpStatusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "OpenCode server answered HTTP {}: {}",
            self.status, self.body
        )
    }
}

impl std::error::Error for HttpStatusError {}

impl OpenCodeServer {
    fn endpoint(&self, path: &str) -> Result<Url> {
        let mut url = Url::parse(self.url.trim())
            .with_context(|| format!("OpenCode server URL is invalid: {}", self.url))?;
        validate_local(&url)?;
        url.set_path(path);
        url.set_query(None);
        if !self.directory.trim().is_empty() {
            url.query_pairs_mut()
                .append_pair("directory", &self.directory);
        }
        Ok(url)
    }

    async fn send(&self, method: Method, path: &str, body: Option<Value>) -> Result<Value> {
        let client = Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .context("failed to build the OpenCode HTTP client")?;
        let mut request = client
            .request(method, self.endpoint(path)?)
            .basic_auth(&self.username, Some(&self.password))
            .header("Accept", "application/json");
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .with_context(|| format!("OpenCode server request {path} failed"))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .context("OpenCode server response could not be read")?;
        if !status.is_success() {
            return Err(HttpStatusError {
                status: status.as_u16(),
                body: text.chars().take(300).collect(),
            }
            .into());
        }
        if text.trim().is_empty() {
            return Ok(Value::Null);
        }
        serde_json::from_str(&text).context("OpenCode server returned invalid JSON")
    }

    pub(crate) async fn get(&self, path: &str) -> Result<Value> {
        self.send(Method::GET, path, None).await
    }

    pub(crate) async fn post(&self, path: &str, body: Option<Value>) -> Result<Value> {
        self.send(Method::POST, path, body).await
    }

    /// The server-sent event stream. No total timeout: it is long-lived, and a
    /// server that accepts and never answers is bounded by the caller.
    pub(crate) async fn events(&self) -> Result<reqwest::Response> {
        let response = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            // OpenCode can close a compressed event stream without a trailer.
            .no_gzip()
            .build()
            .context("failed to build the OpenCode event client")?
            .get(self.endpoint("/event")?)
            .header("Accept-Encoding", "identity")
            .basic_auth(&self.username, Some(&self.password))
            .send()
            .await
            .context("OpenCode event stream could not be opened")?;
        if !response.status().is_success() {
            bail!("OpenCode event stream answered HTTP {}", response.status());
        }
        Ok(response)
    }

    async fn healthy(&self) -> bool {
        self.get("/global/health").await.is_ok()
    }
}

fn validate_local(url: &Url) -> Result<()> {
    if url.scheme() != "http" {
        bail!("OpenCode server URL must use http on localhost");
    }
    match url.host_str() {
        Some("127.0.0.1") | Some("localhost") | Some("::1") | Some("[::1]") => Ok(()),
        _ => bail!("OpenCode server URL must be localhost"),
    }
}

/// Wait until the server answers, or its process has died, or time runs out.
pub(crate) async fn wait_ready(server: &OpenCodeServer, child: &mut Child) -> Result<()> {
    let deadline = Instant::now() + READY_TIMEOUT;
    loop {
        if server.healthy().await {
            return Ok(());
        }
        if let Some(status) = child.try_wait().context("checking the OpenCode server")? {
            bail!("OpenCode server exited before it was ready ({status})");
        }
        if Instant::now() >= deadline {
            bail!("OpenCode server was not ready within {READY_TIMEOUT:?}");
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn server(url: &str) -> OpenCodeServer {
        OpenCodeServer {
            url: url.to_string(),
            username: "opencode".to_string(),
            password: "secret".to_string(),
            directory: "/tmp/work space".to_string(),
        }
    }

    #[test]
    fn endpoints_are_scoped_to_the_workspace_directory() {
        let url = server("http://127.0.0.1:4096")
            .endpoint("/session/ses_1/message")
            .unwrap();
        assert_eq!(url.path(), "/session/ses_1/message");
        assert_eq!(url.query(), Some("directory=%2Ftmp%2Fwork+space"));
    }

    #[test]
    fn a_server_that_is_not_on_loopback_is_refused() {
        assert!(server("http://192.168.1.10:4096")
            .endpoint("/event")
            .is_err());
        assert!(server("https://127.0.0.1:4096").endpoint("/event").is_err());
        assert!(server("http://localhost:4096").endpoint("/event").is_ok());
    }
}
