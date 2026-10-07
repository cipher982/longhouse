//! The JSON-RPC wire every Codex app-server client speaks: the Helm bridge,
//! the Console worker pool and the release canary. Each client keeps its own
//! transport and event loop, because they want different things from the
//! same stream (the bridge settles pause requests, the Console projects
//! events, the canary counts them). What they share lives here, so the wire
//! is written once: message shapes, request ids, how a response becomes a
//! result, the default answers to server requests, the initialize handshake,
//! and the stdio and websocket pumps that turn a transport into events.

use std::collections::BTreeMap;
use std::path::Path;
use std::process::ExitStatus;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{ChildStderr, ChildStdout};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::handshake::client::Request;
use tokio_tungstenite::tungstenite::Message;

/// What a client's reader tasks deliver.
#[derive(Debug)]
pub(crate) enum StreamEvent {
    Rpc(Value),
    Stderr(String),
    StdoutParseError(String),
    TransportClosed(String),
    ChildExited(ExitStatus),
}

pub(crate) fn request(id: u64, method: &str, params: Value) -> Value {
    json!({"id": id, "method": method, "params": params})
}

pub(crate) fn notification(method: &str, params: Value) -> Value {
    json!({"method": method, "params": params})
}

pub(crate) fn response(id: Value, result: Value) -> Value {
    json!({"id": id, "result": result})
}

/// A message carrying both an id and a method is the server asking us.
pub(crate) fn is_server_request(value: &Value) -> bool {
    value.get("id").is_some() && value.get("method").is_some()
}

/// The numeric id of a response (our ids are always numbers).
pub(crate) fn response_id(value: &Value) -> Option<u64> {
    value.get("id").and_then(Value::as_u64)
}

/// `"{method} failed: {error}"` when the response carries an error.
pub(crate) fn response_error(value: &Value, method: &str) -> Option<String> {
    value
        .get("error")
        .map(|error| format!("{method} failed: {error}"))
}

/// The `result` of a response to `method`; an error or a missing result fails.
pub(crate) fn response_result(value: &Value, method: &str) -> Result<Value> {
    if let Some(message) = response_error(value, method) {
        bail!(message);
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| anyhow!("response for {method} missing result"))
}

/// Request ids and the method each outstanding one was for.
#[derive(Debug)]
pub(crate) struct RequestIds {
    next: u64,
    pending: BTreeMap<u64, String>,
}

impl RequestIds {
    pub(crate) fn starting_at(first: u64) -> Self {
        Self {
            next: first,
            pending: BTreeMap::new(),
        }
    }

    /// The next id, not tracked.
    pub(crate) fn allocate(&mut self) -> u64 {
        let id = self.next;
        self.next += 1;
        id
    }

    /// The next id, remembered against `method`, and the request to send.
    pub(crate) fn begin(&mut self, method: &str, params: Value) -> (u64, Value) {
        let id = self.allocate();
        self.pending.insert(id, method.to_string());
        (id, request(id, method, params))
    }

    /// Forget `id` and name the method it was for.
    pub(crate) fn settle(&mut self, id: u64) -> String {
        self.pending
            .remove(&id)
            .unwrap_or_else(|| format!("request#{id}"))
    }
}

/// The `initialize` params: who we are, plus what we ask of the server.
pub(crate) fn initialize_params(name: &str, title: &str, capabilities: Value) -> Value {
    json!({
        "clientInfo": {
            "name": name,
            "title": title,
            "version": env!("CARGO_PKG_VERSION"),
        },
        "capabilities": capabilities,
    })
}

/// The answer to a server request when nobody is asked: approve or decline
/// everything. `None` for a request no client knows how to answer.
/// `fallback_label` answers a question that offers no options.
pub(crate) fn approval_answer(
    method: &str,
    params: &Value,
    approve: bool,
    fallback_label: &str,
) -> Option<Value> {
    Some(match method {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => json!({
            "decision": if approve { "accept" } else { "decline" }
        }),
        "item/permissions/requestApproval" => json!({
            "scope": "turn",
            "permissions": if approve {
                params.get("permissions").cloned().unwrap_or_else(|| json!({}))
            } else {
                json!({})
            }
        }),
        "item/tool/requestUserInput" => json!({
            "answers": request_user_input_answers(params, approve, fallback_label)
        }),
        "mcpServer/elicitation/request" => json!({
            "action": "decline",
            "content": Value::Null,
        }),
        "applyPatchApproval" | "execCommandApproval" => json!({
            "decision": if approve { "Approved" } else { "Denied" }
        }),
        _ => return None,
    })
}

/// The Console's answer: a headless turn declines everything and answers no
/// questions at all.
pub(crate) fn console_decline_answer(method: &str) -> Option<Value> {
    Some(match method {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
            json!({"decision": "decline"})
        }
        "item/permissions/requestApproval" => json!({"scope": "turn", "permissions": {}}),
        "item/tool/requestUserInput" => json!({"answers": {}}),
        "mcpServer/elicitation/request" => json!({"action": "decline", "content": null}),
        "applyPatchApproval" | "execCommandApproval" => json!({"decision": "Denied"}),
        _ => return None,
    })
}

/// One answer per question: the first option's label (or `fallback_label`)
/// when approving, none when declining.
pub(crate) fn request_user_input_answers(
    params: &Value,
    approve: bool,
    fallback_label: &str,
) -> Value {
    let mut answers = serde_json::Map::new();
    let Some(questions) = params.get("questions").and_then(Value::as_array) else {
        return Value::Object(answers);
    };
    for question in questions {
        let Some(id) = question.get("id").and_then(Value::as_str) else {
            continue;
        };
        let question_answers = if approve {
            question
                .get("options")
                .and_then(Value::as_array)
                .and_then(|options| options.first())
                .and_then(|option| option.get("label"))
                .and_then(Value::as_str)
                .map(|label| vec![Value::String(label.to_string())])
                .unwrap_or_else(|| vec![Value::String(fallback_label.to_string())])
        } else {
            Vec::new()
        };
        answers.insert(id.to_string(), json!({ "answers": question_answers }));
    }
    Value::Object(answers)
}

/// Parse each stdout line as a message.
pub(crate) fn spawn_stdout_pump(stdout: ChildStdout, events: mpsc::UnboundedSender<StreamEvent>) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            match serde_json::from_str::<Value>(&line) {
                Ok(value) => {
                    let _ = events.send(StreamEvent::Rpc(value));
                }
                Err(err) => {
                    let _ = events.send(StreamEvent::StdoutParseError(format!("{err}: {line}")));
                }
            }
        }
    });
}

/// Forward each stderr line; the receiver gets the first websocket URL the
/// app-server announces.
pub(crate) fn spawn_stderr_pump(
    stderr: ChildStderr,
    events: mpsc::UnboundedSender<StreamEvent>,
) -> oneshot::Receiver<String> {
    let (listen_tx, listen_rx) = oneshot::channel();
    tokio::spawn(async move {
        let mut listen_tx = Some(listen_tx);
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(url) = extract_websocket_listen_url(&line) {
                if let Some(tx) = listen_tx.take() {
                    let _ = tx.send(url);
                }
            }
            let _ = events.send(StreamEvent::Stderr(line));
        }
    });
    listen_rx
}

/// `listening on: ws://…` from the app-server's startup line.
pub(crate) fn extract_websocket_listen_url(line: &str) -> Option<String> {
    let marker = "listening on:";
    let (_, tail) = line.split_once(marker)?;
    let candidate = tail.trim();
    if candidate.starts_with("ws://") || candidate.starts_with("wss://") {
        Some(candidate.to_string())
    } else {
        None
    }
}

/// The websocket upgrade, carrying the relay's bearer token when there is one.
pub(crate) fn websocket_request(ws_url: &str, bearer: Option<&str>) -> Result<Request> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;

    let mut request = ws_url
        .into_client_request()
        .with_context(|| format!("building app-server request for {ws_url}"))?;
    if let Some(token) = bearer {
        request.headers_mut().insert(
            tokio_tungstenite::tungstenite::http::header::AUTHORIZATION,
            format!("Bearer {token}")
                .parse()
                .context("app-server relay token is not a valid header value")?,
        );
    }
    Ok(request)
}

/// How the websocket reader reports the end of the connection.
#[derive(Clone, Copy, Debug)]
pub(crate) enum WebSocketEnd {
    /// Send `TransportClosed("{label} …")` however the connection ends.
    Report(&'static str),
    /// Stop quietly, reporting only a read error, as a stderr line.
    Quiet,
}

/// Split a connected websocket into an outbound line sender and a reader that
/// delivers messages as events. `read_throttle` pauses after each frame.
pub(crate) fn spawn_websocket_pump<S>(
    ws_stream: tokio_tungstenite::WebSocketStream<S>,
    events: mpsc::UnboundedSender<StreamEvent>,
    end: WebSocketEnd,
    read_throttle: Duration,
) -> mpsc::UnboundedSender<String>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let (mut ws_write, mut ws_read) = ws_stream.split();
    let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(async move {
        while let Some(line) = outbound_rx.recv().await {
            if ws_write.send(Message::Text(line.into())).await.is_err() {
                break;
            }
        }
        let _ = ws_write.close().await;
    });
    tokio::spawn(async move {
        let closed = loop {
            let Some(message) = ws_read.next().await else {
                break "stream ended without a close frame".to_string();
            };
            match message {
                Ok(Message::Text(text)) => match serde_json::from_str::<Value>(&text) {
                    Ok(value) => {
                        let _ = events.send(StreamEvent::Rpc(value));
                    }
                    Err(err) => {
                        let _ =
                            events.send(StreamEvent::StdoutParseError(format!("{err}: {text}")));
                    }
                },
                Ok(Message::Close(frame)) => break format!("closed: {frame:?}"),
                Ok(_) => {}
                Err(err) => {
                    if let WebSocketEnd::Quiet = end {
                        let _ = events
                            .send(StreamEvent::Stderr(format!("websocket read error: {err}")));
                    }
                    break format!("read error: {err}");
                }
            }
            if !read_throttle.is_zero() {
                tokio::time::sleep(read_throttle).await;
            }
        };
        if let WebSocketEnd::Report(label) = end {
            let _ = events.send(StreamEvent::TransportClosed(format!("{label} {closed}")));
        }
    });
    outbound_tx
}

pub(crate) fn extract_string(value: &Value, path: &[&str]) -> Option<String> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_str().map(ToString::to_string)
}

/// A rollout Codex has started writing: present and non-empty.
pub(crate) fn thread_rollout_is_ready(path: impl AsRef<Path>) -> bool {
    std::fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.len() > 0)
        .unwrap_or(false)
}

/// A subscribe that raced the rollout's first write, worth retrying.
pub(crate) fn is_retryable_thread_subscription_error(message: &str) -> bool {
    message.contains("no rollout found for thread id")
        || (message.contains("failed to load rollout") && message.contains("is empty"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_ids_name_settled_and_unknown_responses() {
        let mut ids = RequestIds::starting_at(1);
        let (id, payload) = ids.begin("thread/read", json!({}));
        assert_eq!(
            payload,
            json!({"id": 1, "method": "thread/read", "params": {}})
        );
        assert_eq!(ids.allocate(), 2);
        assert_eq!(ids.settle(id), "thread/read");
        assert_eq!(ids.settle(id), "request#1");
    }

    #[test]
    fn a_response_error_is_named_after_its_method() {
        let failed = json!({"id": 1, "error": {"code": 1}});
        assert_eq!(
            response_result(&failed, "turn/start")
                .unwrap_err()
                .to_string(),
            "turn/start failed: {\"code\":1}"
        );
        assert_eq!(
            response_result(&json!({"id": 1}), "turn/start")
                .unwrap_err()
                .to_string(),
            "response for turn/start missing result"
        );
    }

    #[test]
    fn an_unknown_server_request_has_no_default_answer() {
        assert!(approval_answer("bogus/request", &json!({}), true, "x").is_none());
        assert!(console_decline_answer("bogus/request").is_none());
    }
}
