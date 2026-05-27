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
//! - Accepts any number of inbound connections. Each inbound gets its own
//!   outbound TcpStream to codex's real WS port and a pair of byte-splicing
//!   tasks via `tokio::io::copy`. No WS framing awareness needed — the
//!   tunnel is transparent at the TCP layer.
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
use tokio::io::copy;
use tokio::net::{TcpListener, TcpStream};

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
