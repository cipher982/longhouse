//! Drain-and-forward TCP relay in front of codex's app-server WebSocket.
//!
//! Upstream codex closes slow WS clients when its internal 128-slot outbound
//! mpsc fills (codex-rs/app-server/src/transport/mod.rs). Under bursty LLM
//! streaming that limit gets hit trivially. Rather than fork codex to grow
//! the channel, we sit a tiny localhost TCP relay between codex and every
//! consumer that wants to drive it (the bridge's own client, the remote TUI,
//! future consumers). The relay:
//!
//! - Binds an ephemeral localhost port.
//! - Requires `Authorization: Bearer <auth_token()>` on the inbound HTTP
//!   upgrade. Loopback TCP carries no permission bits, and everything behind
//!   this socket — the live transcript, `turn/start`, `turn/steer` — is
//!   reachable by any local process that can `connect()`, including the
//!   model's own shell tool. The handshake bytes are forwarded verbatim once
//!   the token checks out; codex ignores the header on a loopback listener.
//! - Accepts any number of authenticated inbound connections. Each inbound
//!   gets its own outbound TcpStream to codex's real WS port and a pair of
//!   byte-splicing tasks via `tokio::io::copy`. No WS framing awareness
//!   needed — the tunnel is transparent at the TCP layer.
//! - Sets SO_RCVBUF and SO_SNDBUF to 16 MiB (kernel clamps silently if the
//!   platform allows less) on both sides so the kernel loopback buffer is
//!   large enough to absorb burst streams.
//! - Drains at TCP line speed because the copy tasks do nothing else.
//!
//! Result: codex's internal mpsc never stays full long enough to trip the
//! slow-client disconnect path, even when the far consumer is slow (TUI
//! rendering, engine per-event processing, etc).
//!
//! Empirical: stock upstream codex 0.124.0 under the same 10ms/2000-line
//! stress that reliably disconnects without the relay, completes with the
//! relay. See session note 2026-04-23-zerg-managed-codex-update-automation.md.

use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use rand::RngCore as _;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::io::{copy, AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// Real upgrade requests are a few hundred bytes; anything past this is not a
/// client we want to keep buffering for.
const MAX_REQUEST_HEAD: usize = 16 * 1024;
const REQUEST_HEAD_TIMEOUT: Duration = Duration::from_secs(10);

static AUTH_TOKEN: OnceLock<String> = OnceLock::new();

/// The bearer token every relay connection must present.
///
/// One bridge process owns one managed session, so a per-process token is a
/// per-session token. The bridge publishes it in its 0600 state file, which is
/// how out-of-process clients (`longhouse codex` send/steer, the `--remote`
/// TUI) get it.
pub fn auth_token() -> &'static str {
    AUTH_TOKEN.get_or_init(|| {
        let mut bytes = [0_u8; 24];
        rand::rngs::OsRng.fill_bytes(&mut bytes);
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
    })
}

/// Spawn a relay in front of `upstream_url`. Returns the `ws://...` URL the
/// relay is listening on.
///
/// The relay task runs for the lifetime of the tokio runtime. Accepts are
/// per-inbound so the same relay serves the bridge's own client plus any
/// `codex --remote` TUI that shows up later.
pub async fn spawn(upstream_url: &str) -> Result<String> {
    let upstream_addr = upstream_url
        .strip_prefix("ws://")
        .ok_or_else(|| anyhow!("codex WS relay only supports ws:// URLs, got {upstream_url}"))?
        .to_string();

    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .context("binding codex WS relay listener")?;
    let local_addr = listener
        .local_addr()
        .context("reading codex WS relay listen addr")?;
    let relay_url = format!("ws://{}", local_addr);

    tokio::spawn(async move {
        loop {
            let (mut inbound, _peer) = match listener.accept().await {
                Ok(pair) => pair,
                Err(err) => {
                    eprintln!("codex WS relay accept failed: {err}");
                    return;
                }
            };
            let upstream_addr = upstream_addr.clone();
            tokio::spawn(async move {
                let _ = inbound.set_nodelay(true);
                let head = match read_request_head(&mut inbound).await {
                    Ok(head) => head,
                    Err(err) => {
                        eprintln!("codex WS relay dropped an inbound connection: {err}");
                        return;
                    }
                };
                if !head_is_authorized(&head) {
                    let _ = inbound
                        .write_all(
                            b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        )
                        .await;
                    return;
                }
                let mut outbound = match TcpStream::connect(&upstream_addr).await {
                    Ok(sock) => sock,
                    Err(err) => {
                        eprintln!("codex WS relay upstream dial to {upstream_addr} failed: {err}");
                        return;
                    }
                };
                let _ = outbound.set_nodelay(true);
                set_large_socket_buffers(&inbound);
                set_large_socket_buffers(&outbound);
                if let Err(err) = outbound.write_all(&head).await {
                    eprintln!("codex WS relay upstream handshake write failed: {err}");
                    return;
                }

                let (mut ri, mut wi) = inbound.split();
                let (mut ro, mut wo) = outbound.split();
                let client_to_server = copy(&mut ri, &mut wo);
                let server_to_client = copy(&mut ro, &mut wi);
                let _ = tokio::join!(client_to_server, server_to_client);
            });
        }
    });

    Ok(relay_url)
}

/// Read the inbound HTTP request head, one byte at a time so no WebSocket
/// payload is consumed past the blank line. Heads are a few hundred bytes and
/// arrive once per client, so the syscall count is irrelevant.
async fn read_request_head(inbound: &mut TcpStream) -> Result<Vec<u8>> {
    let read = async {
        let mut head = Vec::with_capacity(512);
        let mut byte = [0_u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            if head.len() >= MAX_REQUEST_HEAD {
                return Err(anyhow!("request head exceeded {MAX_REQUEST_HEAD} bytes"));
            }
            if inbound.read(&mut byte).await? == 0 {
                return Err(anyhow!("connection closed mid-handshake"));
            }
            head.push(byte[0]);
        }
        Ok(head)
    };
    tokio::time::timeout(REQUEST_HEAD_TIMEOUT, read)
        .await
        .map_err(|_| anyhow!("timed out reading the request head"))?
}

fn head_is_authorized(head: &[u8]) -> bool {
    let expected = format!("Bearer {}", auth_token());
    String::from_utf8_lossy(head)
        .lines()
        .filter_map(|line| line.split_once(':'))
        .any(|(name, value)| name.eq_ignore_ascii_case("authorization") && value.trim() == expected)
}

fn set_large_socket_buffers(sock: &TcpStream) {
    use std::os::fd::{AsRawFd, RawFd};
    // 16 MiB. Kernel silently clamps if the platform allows less; we don't
    // need getsockopt confirmation — whatever the platform gives us is better
    // than default.
    let desired: libc::c_int = 16 * 1024 * 1024;
    let fd: RawFd = sock.as_raw_fd();
    unsafe {
        let _ = libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            &desired as *const _ as *const libc::c_void,
            std::mem::size_of_val(&desired) as libc::socklen_t,
        );
        let _ = libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            &desired as *const _ as *const libc::c_void,
            std::mem::size_of_val(&desired) as libc::socklen_t,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_relay_token_opens_the_socket() {
        let authorized = format!(
            "GET / HTTP/1.1\r\nHost: 127.0.0.1:1\r\nUpgrade: websocket\r\nAuthorization: Bearer {}\r\n\r\n",
            auth_token()
        );
        assert!(head_is_authorized(authorized.as_bytes()));
        assert!(!head_is_authorized(
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:1\r\nUpgrade: websocket\r\n\r\n"
        ));
        assert!(!head_is_authorized(
            b"GET / HTTP/1.1\r\nAuthorization: Bearer guessed-token\r\n\r\n"
        ));
    }

    #[tokio::test]
    async fn unauthenticated_connections_never_reach_upstream() {
        // Stands in for codex's app-server: reports back whether the relay
        // forwarded the handshake, and whether the bearer survived it.
        let upstream = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let upstream_addr = upstream.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((mut sock, _)) = upstream.accept().await {
                tokio::spawn(async move {
                    let head = read_request_head(&mut sock).await.unwrap();
                    let reply = if head_is_authorized(&head) {
                        b"UPSTREAM-AUTH".as_slice()
                    } else {
                        b"UPSTREAM-NONE".as_slice()
                    };
                    let _ = sock.write_all(reply).await;
                });
            }
        });

        let relay_url = spawn(&format!("ws://{upstream_addr}")).await.unwrap();
        let relay_addr = relay_url.strip_prefix("ws://").unwrap().to_string();

        let mut anonymous = TcpStream::connect(&relay_addr).await.unwrap();
        anonymous
            .write_all(b"GET / HTTP/1.1\r\nHost: relay\r\nUpgrade: websocket\r\n\r\n")
            .await
            .unwrap();
        let mut refusal = Vec::new();
        anonymous.read_to_end(&mut refusal).await.unwrap();
        assert!(String::from_utf8_lossy(&refusal).starts_with("HTTP/1.1 401"));

        let mut authorized = TcpStream::connect(&relay_addr).await.unwrap();
        authorized
            .write_all(
                format!(
                    "GET / HTTP/1.1\r\nHost: relay\r\nUpgrade: websocket\r\nAuthorization: Bearer {}\r\n\r\n",
                    auth_token()
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        let mut spliced = [0_u8; 13];
        authorized.read_exact(&mut spliced).await.unwrap();
        assert_eq!(&spliced, b"UPSTREAM-AUTH");
    }
}
