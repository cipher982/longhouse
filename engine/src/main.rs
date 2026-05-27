mod bench;
mod build_identity;
mod codex_app_server_canary;
mod codex_attachments;
mod codex_bridge;
mod codex_source;
mod codex_ws_relay;
mod commands;
mod config;
mod control_channel;
mod daemon;
mod discovery;
mod error_tracker;
mod flight;
mod heartbeat;
mod managed_bridge_scan;
mod managed_claude_scan;
mod managed_reaper;
mod observability;
mod outbox;
mod pipeline;
mod scheduler;
mod shipper;
mod shipping;
mod shipping_stats;
mod state;
mod text;
mod unmanaged_bindings;
mod watcher;

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use tracing_subscriber::fmt::MakeWriter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

use codex_app_server_canary::{
    parse_app_server_transport, parse_remote_tui_subscribe_phase,
    run as run_codex_app_server_canary, CanaryConfig,
};
use codex_bridge::{
    cmd_codex_bridge_attach, cmd_codex_bridge_interrupt, cmd_codex_bridge_run,
    cmd_codex_bridge_send, cmd_codex_bridge_start, cmd_codex_bridge_steer, cmd_codex_bridge_stop,
    BridgeAttachConfig, BridgeInterruptConfig, BridgeLaunchMode, BridgeRunConfig, BridgeSendConfig,
    BridgeStartConfig, BridgeSteerConfig, BridgeSteerError, BridgeStopConfig,
};
use config::ShipperConfig;
use pipeline::compressor::CompressionAlgo;
use state::db::open_db;

/// Parse the `--attachments-json` CLI flag into the engine's typed
/// `AttachmentRef` list. Empty / `None` / `null` / `[]` all collapse to no
/// attachments so text-only callers don't need to special-case the flag.
fn parse_attachments_cli_arg(
    raw: Option<&str>,
) -> anyhow::Result<Vec<crate::codex_attachments::AttachmentRef>> {
    let Some(raw) = raw else {
        return Ok(Vec::new());
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(Vec::new());
    }
    let value: serde_json::Value = serde_json::from_str(trimmed)
        .map_err(|e| anyhow::anyhow!("--attachments-json is not valid JSON: {e}"))?;
    crate::codex_attachments::parse_attachments(&serde_json::json!({ "attachments": value }))
}

fn parse_compression_algo(s: &str) -> anyhow::Result<CompressionAlgo> {
    match s.to_lowercase().as_str() {
        "gzip" | "gz" => Ok(CompressionAlgo::Gzip),
        "zstd" | "zstandard" => Ok(CompressionAlgo::Zstd),
        _ => anyhow::bail!("Unknown compression: {}. Use 'gzip' or 'zstd'", s),
    }
}

fn resolve_codex_bridge_start_roots(
    state_root: Option<PathBuf>,
    longhouse_home: Option<PathBuf>,
    isolation_root: Option<PathBuf>,
) -> anyhow::Result<(Option<PathBuf>, Option<PathBuf>)> {
    if let Some(root) = isolation_root {
        return Ok((
            Some(root.join("codex-bridge")),
            Some(root.join("longhouse")),
        ));
    }
    if state_root.is_some() && longhouse_home.is_none() {
        anyhow::bail!(
            "--state-root only isolates bridge files; pass --longhouse-home too or use --isolation-root <dir>"
        );
    }
    Ok((state_root, longhouse_home))
}

fn parse_codex_bridge_launch_mode(raw: &str) -> anyhow::Result<BridgeLaunchMode> {
    BridgeLaunchMode::from_cli_value(raw)
        .ok_or_else(|| anyhow::anyhow!("invalid Codex bridge launch mode: {raw}"))
}

fn codex_bridge_start_semantics(
    start_thread: bool,
    create_initial_thread: bool,
) -> (bool, BridgeLaunchMode) {
    if start_thread {
        (true, BridgeLaunchMode::DetachedUi)
    } else {
        (create_initial_thread, BridgeLaunchMode::Tui)
    }
}

fn codex_bridge_run_semantics(
    start_thread: bool,
    create_initial_thread: bool,
    launch_mode: &str,
) -> anyhow::Result<(bool, BridgeLaunchMode)> {
    if start_thread {
        return Ok((true, BridgeLaunchMode::DetachedUi));
    }
    Ok((
        create_initial_thread,
        parse_codex_bridge_launch_mode(launch_mode)?,
    ))
}

#[derive(Parser)]
#[command(
    name = "longhouse-engine",
    version = env!("LONGHOUSE_BUILD_QUALIFIED"),
    about = "Longhouse session shipper (Rust engine)"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Parse a JSONL file and report event counts (dev/validation tool)
    Parse {
        /// Path to session JSONL file
        path: PathBuf,

        /// Byte offset to start from
        #[arg(long, default_value = "0")]
        offset: u64,

        /// Print parsed events as JSON
        #[arg(long)]
        dump_events: bool,

        /// Also build + gzip-compress the ingest payload
        #[arg(long)]
        compress: bool,
    },

    /// Run multi-file benchmark (compare against Python profiling baselines)
    Bench {
        /// Scale level: L1 (1 file, largest), L2 (10% of files), L3 (100% of files)
        #[arg(long, default_value = "L1")]
        level: String,

        /// Include compression in benchmark
        #[arg(long)]
        compress: bool,

        /// Use rayon parallel file processing
        #[arg(long)]
        parallel: bool,

        /// Number of worker threads (default: num_cpus)
        #[arg(long, default_value = "0")]
        workers: usize,

        /// Compression algorithm: zstd (default) or gzip
        #[arg(long, default_value = "zstd")]
        compression: String,

        /// Mode B: actually POST each compressed payload to this ingest URL
        /// and measure end-to-end shipping throughput (events/sec, server
        /// queue/exec p50/p95). When unset the bench is parse+compress only
        /// (Mode A).
        #[arg(long)]
        ship_url: Option<String>,

        /// Auth token for --ship-url (required when ship-url is set).
        #[arg(long)]
        ship_token: Option<String>,

        /// Number of concurrent in-flight POSTs in Mode B.
        #[arg(long, default_value = "1")]
        ship_concurrency: usize,
    },

    /// Daemon mode: watch for file changes and ship incrementally
    Connect {
        /// API URL override (default: from ~/.longhouse/machine/state.json)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.longhouse/machine/device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "zstd")]
        compression: String,

        /// Reconciliation full scan interval in seconds
        #[arg(long, default_value = "300")]
        fallback_scan_secs: u64,

        /// Spool replay interval in seconds
        #[arg(long, default_value = "30")]
        spool_replay_secs: u64,

        /// Maximum compressed batch size in bytes before splitting/dead-lettering
        #[arg(long)]
        max_batch_bytes: Option<u64>,

        /// Log directory for rolling log files (default: ~/.longhouse/agent/logs, or LONGHOUSE_LOG_DIR env)
        #[arg(long)]
        log_dir: Option<PathBuf>,

        /// Human-readable name for this machine (default: from ~/.longhouse/machine/state.json or hostname)
        #[arg(long)]
        machine_name: Option<String>,
    },

    /// One-shot: scan all provider sessions and ship new events
    Ship {
        /// API URL override (default: from ~/.longhouse/machine/state.json)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.longhouse/machine/device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Ship a single file instead of scanning all providers
        #[arg(long)]
        file: Option<PathBuf>,

        /// Provider name override when using --file (claude, codex, antigravity, gemini)
        #[arg(long)]
        provider: Option<String>,

        /// Number of parallel workers (default: num_cpus)
        #[arg(long, default_value = "0")]
        workers: usize,

        /// Dry run: parse and compress but don't POST
        #[arg(long)]
        dry_run: bool,

        /// JSON output (machine readable)
        #[arg(long)]
        json: bool,

        /// Compression algorithm: zstd (default) or gzip
        #[arg(long, default_value = "zstd")]
        compression: String,

        /// Maximum compressed batch size in bytes before splitting/dead-lettering
        #[arg(long)]
        max_batch_bytes: Option<u64>,

        /// Human-readable name for this machine (default: from ~/.longhouse/machine/state.json or hostname)
        #[arg(long)]
        machine_name: Option<String>,

        /// Override session ID for managed-local sessions (uses provider's native ID as provider_session_id)
        #[arg(long)]
        session_id: Option<String>,

        /// When using --file, only ship once the unread range includes assistant/tool reply evidence
        #[arg(long)]
        require_reply_evidence: bool,
    },

    /// Dev canary: talk directly to `codex app-server` over stdio and capture the full stream
    CodexAppServerCanary {
        /// Initial user prompt to send as the first turn
        #[arg(long)]
        prompt: String,

        /// Working directory to bind the thread to (default: current directory)
        #[arg(long)]
        cwd: Option<PathBuf>,

        /// Reuse an explicit home root instead of creating a fresh temp one; the canary maps CODEX_HOME to <home>/.codex
        #[arg(long)]
        home: Option<PathBuf>,

        /// Approval policy stamped onto thread start/resume
        #[arg(long, default_value = "never")]
        approval_policy: String,

        /// Sandbox mode stamped onto thread start/resume
        #[arg(long, default_value = "read-only")]
        sandbox: String,

        /// Optional model override for thread/turn start
        #[arg(long)]
        model: Option<String>,

        /// Optional reasoning effort override for turn/start
        #[arg(long)]
        effort: Option<String>,

        /// Codex binary to invoke
        #[arg(long, default_value = "codex")]
        codex_bin: String,

        /// App-server transport for the canary client
        #[arg(long, default_value = "stdio")]
        app_server_transport: String,

        /// WebSocket listen port when using --app-server-transport websocket (0 = auto)
        #[arg(long, default_value = "0")]
        listen_port: u16,

        /// Session source tag stamped into new threads
        #[arg(long, default_value = "longhouse_canary")]
        session_source: String,

        /// Resume an existing Codex app-server thread instead of creating a new one
        #[arg(long)]
        resume_thread_id: Option<String>,

        /// Optional follow-up input to send via turn/steer after the initial turn starts
        #[arg(long)]
        steer_text: Option<String>,

        /// Delay before sending --steer-text
        #[arg(long, default_value = "1500")]
        steer_after_ms: u64,

        /// Delay before sending turn/interrupt
        #[arg(long)]
        interrupt_after_ms: Option<u64>,

        /// Auto-approve server-initiated approval requests instead of declining them
        #[arg(long)]
        auto_approve: bool,

        /// Spawn a real `codex resume --remote` TUI against the managed thread before sending the prompt
        #[arg(long)]
        spawn_remote_tui: bool,

        /// How long to wait for the remote TUI to stay alive after launch
        #[arg(long, default_value = "3000")]
        remote_tui_grace_ms: u64,

        /// Optional path for the PTY log captured from the remote TUI when --spawn-remote-tui is used
        #[arg(long)]
        remote_tui_log: Option<PathBuf>,

        /// When a second client should subscribe to a remote-TUI-owned thread
        #[arg(long, default_value = "postturn")]
        remote_tui_subscribe_phase: String,

        /// Probe thread/read after the run and report turn count
        #[arg(long)]
        probe_thread_read: bool,

        /// Probe thread/list after the run and report whether the managed thread is discoverable
        #[arg(long)]
        probe_thread_list: bool,

        /// Overall timeout for the full canary run
        #[arg(long, default_value = "60")]
        event_timeout_secs: u64,

        /// Write every client/server message as JSONL for debugging
        #[arg(long)]
        log_jsonl: Option<PathBuf>,

        /// Print the final summary as JSON instead of human-readable text
        #[arg(long)]
        json: bool,

        /// Use the real HOME/CODEX_HOME instead of the default isolated temp home root
        #[arg(long)]
        real_home: bool,

        /// Keep the isolated temp home root after the run for manual inspection
        #[arg(long)]
        keep_home: bool,

        /// Verify Codex hook presence by watching the isolated outbox for idle/thinking/idle
        #[arg(long)]
        verify_hooks: bool,

        /// Sleep this many ms between each websocket message read. Used by the remote
        /// backpressure probe to deliberately drain slower than the server produces,
        /// so the server-side outbound queue can fill and trip the slow-connection
        /// disconnect path on unpatched Codex. 0 disables throttling.
        #[arg(long, default_value = "0")]
        ws_read_throttle_ms: u64,

        /// Insert a drain-and-forward TCP relay between codex's WS listener
        /// and the canary client. The relay sets SO_RCVBUF/SO_SNDBUF to the
        /// platform max on all sockets so kernel buffers stay generous,
        /// giving codex's internal mpsc(128) room to flush. Experiment to
        /// see whether this prevents the slow-connection disconnect bug
        /// without forking codex.
        #[arg(long)]
        proxy_codex_ws: bool,
    },

    /// Bind a transcript path to a managed Longhouse session ID.
    ///
    /// Used by hooks and launchers to tell the daemon which session ID to use
    /// when shipping a managed-local transcript. Must be called BEFORE the
    /// transcript has new content to ship.
    Bind {
        /// Canonical absolute path to the transcript file
        #[arg(long)]
        path: PathBuf,

        /// Managed Longhouse session ID
        #[arg(long)]
        session_id: String,

        /// Provider name (claude, codex, antigravity, gemini)
        #[arg(long, default_value = "claude")]
        provider: String,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,
    },

    /// Native managed Codex bridge utilities
    CodexBridge {
        #[command(subcommand)]
        command: CodexBridgeCommands,
    },
}

#[derive(Subcommand)]
enum CodexBridgeCommands {
    /// Start a detached bridge daemon and print the ready thread metadata
    Start {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        cwd: PathBuf,

        #[arg(long)]
        url: String,

        #[arg(long)]
        token: String,

        #[arg(long, default_value = "codex")]
        codex_bin: String,

        #[arg(long)]
        session_source: Option<String>,

        #[arg(long)]
        approval_policy: Option<String>,

        #[arg(long)]
        sandbox: Option<String>,

        #[arg(long)]
        model: Option<String>,

        #[arg(long)]
        model_reasoning_effort: Option<String>,

        #[arg(long)]
        machine_name: Option<String>,

        #[arg(long)]
        auto_approve: bool,

        #[arg(long)]
        state_root: Option<PathBuf>,

        #[arg(long)]
        longhouse_home: Option<PathBuf>,

        /// Dev/test isolation root. Sets state root to <root>/codex-bridge and Longhouse home to <root>/longhouse.
        #[arg(long, conflicts_with_all = ["state_root", "longhouse_home"])]
        isolation_root: Option<PathBuf>,

        #[arg(long)]
        log_file: Option<PathBuf>,

        #[arg(long, default_value_t = 25)]
        start_timeout_secs: u64,

        /// Create the initial Codex thread ourselves via thread/start while
        /// preserving TUI lifecycle semantics.
        #[arg(long, conflicts_with = "start_thread")]
        create_initial_thread: bool,

        /// Legacy detached-UI option: create the initial Codex thread and
        /// persist detached-UI/headless launch-mode semantics.
        #[arg(long, conflicts_with = "create_initial_thread")]
        start_thread: bool,

        #[arg(long)]
        json: bool,
    },

    /// Long-lived bridge daemon process (normally started via `codex-bridge start`)
    Run {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        cwd: PathBuf,

        #[arg(long)]
        url: String,

        #[arg(long)]
        token: String,

        #[arg(long, default_value = "codex")]
        codex_bin: String,

        #[arg(long)]
        session_source: Option<String>,

        #[arg(long)]
        approval_policy: Option<String>,

        #[arg(long)]
        sandbox: Option<String>,

        #[arg(long)]
        model: Option<String>,

        #[arg(long)]
        model_reasoning_effort: Option<String>,

        #[arg(long)]
        machine_name: Option<String>,

        #[arg(long)]
        auto_approve: bool,

        #[arg(long)]
        longhouse_home: Option<PathBuf>,

        #[arg(long)]
        state_file: PathBuf,

        #[arg(long)]
        log_file: PathBuf,

        /// Create the initial Codex thread ourselves via thread/start.
        #[arg(long, conflicts_with = "start_thread")]
        create_initial_thread: bool,

        /// Persisted lifecycle mode for this bridge.
        #[arg(long, default_value = "tui")]
        launch_mode: String,

        /// Legacy detached-UI option: create the initial Codex thread and
        /// persist detached-UI/headless launch-mode semantics.
        #[arg(long, conflicts_with = "create_initial_thread")]
        start_thread: bool,
    },

    /// Attach stock Codex TUI to a running managed bridge
    Attach {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        state_root: Option<PathBuf>,

        #[arg(long)]
        codex_bin: Option<String>,
    },

    /// Send a prompt into a running managed bridge thread
    Send {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        text: String,

        #[arg(long)]
        state_root: Option<PathBuf>,

        /// Debug/operator escape hatch: route directly to Codex app-server if bridge IPC is unavailable.
        #[arg(long, hide = true)]
        allow_direct_ws_fallback: bool,

        /// JSON array of attachment refs: `[{"id":"...","mime_type":"image/png","sha256":"...","blob_url":"..."}]`.
        /// Empty / unset means text-only.
        #[arg(long)]
        attachments_json: Option<String>,

        #[arg(long)]
        json: bool,
    },

    /// Interrupt the currently active turn for a managed bridge thread
    Interrupt {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        state_root: Option<PathBuf>,
    },

    /// Send a mid-turn steer into the currently active turn.
    ///
    /// Exits 2 and prints `error_code: turn_ended` when the app-server
    /// reports the expected turn id is no longer active — callers should
    /// treat that as a product-level "too late" signal rather than a
    /// generic failure.
    Steer {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        text: String,

        #[arg(long)]
        state_root: Option<PathBuf>,

        /// JSON array of attachment refs (same shape as `send --attachments-json`).
        #[arg(long)]
        attachments_json: Option<String>,
    },

    /// Stop a running managed bridge and its local Codex app-server child
    Stop {
        #[arg(long)]
        session_id: String,

        #[arg(long)]
        state_root: Option<PathBuf>,

        /// Terminal reason to record when the bridge accepts the stop.
        #[arg(long)]
        reason: Option<String>,
    },
}

fn resolve_log_dir(log_dir_arg: Option<&std::path::Path>) -> std::path::PathBuf {
    if let Some(p) = log_dir_arg {
        return p.to_path_buf();
    }
    if let Ok(dir) = std::env::var("LONGHOUSE_LOG_DIR") {
        return std::path::PathBuf::from(dir);
    }
    config::get_agent_log_dir().unwrap_or_else(|_| {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        std::path::PathBuf::from(home)
            .join(".longhouse")
            .join("agent")
            .join("logs")
    })
}

fn prune_old_logs(log_dir: &std::path::Path, keep_days: u64) {
    let cutoff = std::time::SystemTime::now()
        .checked_sub(std::time::Duration::from_secs(keep_days * 86400))
        .unwrap_or(std::time::UNIX_EPOCH);

    if let Ok(entries) = std::fs::read_dir(log_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            // tracing_appender rolling::daily creates files named "engine.log.YYYY-MM-DD"
            // Match by file_name prefix rather than extension
            let is_engine_log = path
                .file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("engine.log"))
                .unwrap_or(false);
            if is_engine_log {
                if let Ok(meta) = std::fs::metadata(&path) {
                    if let Ok(modified) = meta.modified() {
                        if modified < cutoff {
                            let _ = std::fs::remove_file(&path);
                        }
                    }
                }
            }
        }
    }
}

fn command_name(command: &Commands) -> &'static str {
    match command {
        Commands::Parse { .. } => "parse",
        Commands::Bench { .. } => "bench",
        Commands::Ship { .. } => "ship",
        Commands::Connect { .. } => "connect",
        Commands::Bind { .. } => "bind",
        Commands::CodexAppServerCanary { .. } => "codex-app-server-canary",
        Commands::CodexBridge { command } => match command {
            CodexBridgeCommands::Start { .. } => "codex-bridge-start",
            CodexBridgeCommands::Run { .. } => "codex-bridge-run",
            CodexBridgeCommands::Attach { .. } => "codex-bridge-attach",
            CodexBridgeCommands::Send { .. } => "codex-bridge-send",
            CodexBridgeCommands::Interrupt { .. } => "codex-bridge-interrupt",
            CodexBridgeCommands::Steer { .. } => "codex-bridge-steer",
            CodexBridgeCommands::Stop { .. } => "codex-bridge-stop",
        },
    }
}

fn init_tracing_subscriber<W>(
    writer: W,
    ansi: bool,
    command_name: &'static str,
) -> anyhow::Result<Option<observability::OtelGuard>>
where
    W: for<'writer> MakeWriter<'writer> + Send + Sync + 'static,
{
    let env_filter = tracing_subscriber::EnvFilter::from_default_env()
        .add_directive("longhouse_engine=info".parse()?);
    let fmt_layer = tracing_subscriber::fmt::layer()
        .with_writer(writer)
        .with_ansi(ansi);
    let registry = tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt_layer);

    if let Some(otel) = observability::build_otel_setup(command_name)? {
        registry
            .with(tracing_opentelemetry::layer().with_tracer(otel.tracer))
            .try_init()?;
        Ok(Some(otel.guard))
    } else {
        registry.try_init()?;
        Ok(None)
    }
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    let command_name = command_name(&cli.command);

    // Drop any leftover image-attach blobs from a prior process before
    // touching anything else. Cheap, best-effort, no-op when empty.
    if matches!(cli.command, Commands::Connect { .. }) {
        crate::codex_attachments::cleanup_orphan_tmpdirs();
    }

    // For Connect (daemon) mode: use rolling file appender.
    // For all other commands: log to stderr as usual.
    let _guard;
    let otel_shutdown_guard;
    match &cli.command {
        Commands::Connect { log_dir, .. } => {
            let log_path = resolve_log_dir(log_dir.as_deref());
            std::fs::create_dir_all(&log_path)?;
            prune_old_logs(&log_path, 7);

            let file_appender = tracing_appender::rolling::daily(&log_path, "engine.log");
            let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
            _guard = Some(guard);
            otel_shutdown_guard = init_tracing_subscriber(non_blocking, false, command_name)?;
        }
        _ => {
            _guard = None;
            otel_shutdown_guard = init_tracing_subscriber(std::io::stderr, true, command_name)?;
        }
    }
    let _ = &otel_shutdown_guard;

    match cli.command {
        Commands::Connect {
            url,
            token,
            db,
            compression,
            fallback_scan_secs,
            spool_replay_secs,
            max_batch_bytes,
            log_dir: _,
            machine_name,
        } => {
            let algo = parse_compression_algo(&compression)?;
            let shipper_config = ShipperConfig::from_env()?.with_overrides(
                url.as_deref(),
                token.as_deref(),
                db.as_deref(),
                None,
                machine_name.as_deref(),
                max_batch_bytes,
            );
            pipeline::compressor::set_machine_name(&shipper_config.machine_name);

            let connect_config = daemon::ConnectConfig {
                shipper_config,
                algo,
                fallback_scan_secs,
                spool_replay_secs,
                flight_recorder_dir: if flight::flight_recorder_enabled() {
                    Some(config::get_agent_flight_dir()?)
                } else {
                    None
                },
            };

            // Keep LocalSet-based transcript jobs available while letting Send
            // tasks such as the control WebSocket heartbeat and HTTP egress
            // work run on worker threads if local file processing stalls.
            let default_worker_threads = (num_cpus::get() / 2).max(4);
            let worker_threads = std::env::var("LONGHOUSE_ENGINE_WORKER_THREADS")
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .filter(|value| *value > 0)
                .unwrap_or(default_worker_threads);
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(worker_threads)
                .enable_all()
                .build()?;
            let local = tokio::task::LocalSet::new();
            rt.block_on(local.run_until(daemon::run(connect_config)))?;
        }
        Commands::Parse {
            path,
            offset,
            dump_events,
            compress,
        } => {
            commands::cmd_parse::cmd_parse(&path, offset, dump_events, compress)?;
        }
        Commands::Bench {
            level,
            compress,
            parallel,
            workers,
            compression,
            ship_url,
            ship_token,
            ship_concurrency,
        } => {
            let algo = parse_compression_algo(&compression)?;
            commands::cmd_bench::cmd_bench(
                &level,
                compress,
                parallel,
                workers,
                algo,
                ship_url.as_deref(),
                ship_token.as_deref(),
                ship_concurrency.max(1),
            )?;
        }
        Commands::Ship {
            url,
            token,
            db,
            file,
            provider,
            workers,
            dry_run,
            json,
            compression,
            max_batch_bytes,
            machine_name,
            session_id,
            require_reply_evidence,
        } => {
            let algo = parse_compression_algo(&compression)?;
            // Initialize machine name for payload labeling
            if let Some(ref name) = machine_name {
                pipeline::compressor::set_machine_name(name);
            } else {
                // Load from canonical machine state (reads ~/.longhouse/machine/state.json)
                let cfg = ShipperConfig::from_env().unwrap_or_default();
                pipeline::compressor::set_machine_name(&cfg.machine_name);
            }
            // Build tokio runtime for async HTTP
            let rt = tokio::runtime::Runtime::new()?;
            if let Some(path) = file.as_ref() {
                rt.block_on(commands::cmd_ship::cmd_ship_file(
                    path,
                    provider.as_deref(),
                    url.as_deref(),
                    token.as_deref(),
                    db.as_deref(),
                    dry_run,
                    json,
                    algo,
                    max_batch_bytes,
                    session_id.as_deref(),
                    require_reply_evidence,
                ))?;
            } else {
                rt.block_on(commands::cmd_ship::cmd_ship(
                    url.as_deref(),
                    token.as_deref(),
                    db.as_deref(),
                    workers,
                    dry_run,
                    json,
                    algo,
                    max_batch_bytes,
                ))?;
            }
        }
        Commands::CodexAppServerCanary {
            prompt,
            cwd,
            home,
            approval_policy,
            sandbox,
            model,
            effort,
            codex_bin,
            app_server_transport,
            listen_port,
            session_source,
            resume_thread_id,
            steer_text,
            steer_after_ms,
            interrupt_after_ms,
            auto_approve,
            spawn_remote_tui,
            remote_tui_grace_ms,
            remote_tui_log,
            remote_tui_subscribe_phase,
            probe_thread_read,
            probe_thread_list,
            event_timeout_secs,
            log_jsonl,
            json,
            real_home,
            keep_home,
            verify_hooks,
            ws_read_throttle_ms,
            proxy_codex_ws,
        } => {
            let rt = tokio::runtime::Runtime::new()?;
            let summary = rt.block_on(run_codex_app_server_canary(CanaryConfig {
                prompt,
                cwd: cwd.unwrap_or(std::env::current_dir()?),
                home_override: home,
                approval_policy,
                sandbox,
                model,
                effort,
                codex_bin,
                app_server_transport: parse_app_server_transport(&app_server_transport)?,
                listen_port,
                session_source,
                resume_thread_id,
                steer_text,
                steer_after_ms,
                interrupt_after_ms,
                auto_approve,
                spawn_remote_tui,
                remote_tui_grace_ms,
                remote_tui_log,
                remote_tui_subscribe_phase: parse_remote_tui_subscribe_phase(
                    &remote_tui_subscribe_phase,
                )?,
                probe_thread_read,
                probe_thread_list,
                event_timeout_secs,
                log_jsonl,
                isolate_home: !real_home,
                keep_home,
                verify_hooks,
                ws_read_throttle_ms,
                proxy_codex_ws,
            }))?;
            if json {
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                println!("thread_id: {}", summary.thread_id);
                println!("turn_id: {}", summary.turn_id);
                println!("turn_status: {}", summary.turn_status);
                println!("sandbox: {}", summary.sandbox);
                if !summary.assistant_text.is_empty() {
                    println!("assistant_text: {}", summary.assistant_text);
                }
                if let Some(path) = summary.log_jsonl.as_deref() {
                    println!("log_jsonl: {}", path);
                }
                if let Some(home) = summary.isolated_home_path.as_deref() {
                    println!("isolated_home: {}", home);
                }
                println!("effective_home: {}", summary.effective_home_path);
                println!(
                    "effective_codex_home: {}",
                    summary.effective_codex_home_path
                );
                println!(
                    "remote_tui_subscribe_phase: {}",
                    summary.remote_tui_subscribe_phase
                );
                if let Some(path) = summary.thread_path.as_deref() {
                    println!("thread_path: {}", path);
                    println!("thread_path_exists: {}", summary.thread_path_exists);
                    println!(
                        "thread_path_within_home: {}",
                        summary.thread_path_within_home
                    );
                }
                if summary.hook_session_id.is_some() {
                    println!("hook_states: {:?}", summary.hook_states);
                    println!(
                        "hook_notification_counts: {:?}",
                        summary.hook_notification_counts
                    );
                }
                if !summary.server_request_counts.is_empty() {
                    println!("server_request_counts: {:?}", summary.server_request_counts);
                }
                if let Some(turn_count) = summary.thread_read_turn_count {
                    println!("thread_read_turn_count: {}", turn_count);
                }
                if let Some(thread_list_count) = summary.thread_list_count {
                    println!("thread_list_count: {}", thread_list_count);
                }
                if let Some(contains_thread) = summary.thread_list_contains_thread {
                    println!("thread_list_contains_thread: {}", contains_thread);
                }
                if !summary.response_errors.is_empty() {
                    println!("response_errors: {:?}", summary.response_errors);
                }
            }
        }
        Commands::Bind {
            path,
            session_id,
            provider,
            db,
        } => {
            let conn = open_db(db.as_deref())?;
            let sb = state::session_binding::SessionBinding::new(&conn);
            let canonical = std::fs::canonicalize(&path)
                .unwrap_or_else(|_| path.clone())
                .to_string_lossy()
                .to_string();
            sb.bind(&canonical, &session_id, &provider)?;
            eprintln!("Bound {} → {}", canonical, session_id);
        }
        Commands::CodexBridge { command } => {
            let rt = tokio::runtime::Runtime::new()?;
            match command {
                CodexBridgeCommands::Start {
                    session_id,
                    cwd,
                    url,
                    token,
                    codex_bin,
                    session_source: _,
                    approval_policy,
                    sandbox,
                    model,
                    model_reasoning_effort,
                    machine_name,
                    auto_approve,
                    state_root,
                    longhouse_home,
                    isolation_root,
                    log_file,
                    start_timeout_secs,
                    create_initial_thread,
                    start_thread,
                    json,
                } => {
                    let (state_root, longhouse_home) = resolve_codex_bridge_start_roots(
                        state_root,
                        longhouse_home,
                        isolation_root,
                    )?;
                    let (create_initial_thread, launch_mode) =
                        codex_bridge_start_semantics(start_thread, create_initial_thread);
                    let summary = rt.block_on(cmd_codex_bridge_start(BridgeStartConfig {
                        session_id,
                        cwd,
                        api_url: url,
                        api_token: token,
                        codex_bin,
                        approval_policy,
                        sandbox,
                        model,
                        model_reasoning_effort,
                        machine_name,
                        auto_approve,
                        state_root,
                        longhouse_home,
                        log_file,
                        start_timeout_secs,
                        create_initial_thread,
                        launch_mode,
                    }))?;
                    if json {
                        println!("{}", serde_json::to_string_pretty(&summary)?);
                    } else {
                        println!("session_id: {}", summary.session_id);
                        println!("pid: {}", summary.pid);
                        println!("state_file: {}", summary.state_file);
                        println!("log_file: {}", summary.log_file);
                        println!("ws_url: {}", summary.ws_url);
                        if let Some(ref tid) = summary.thread_id {
                            println!("thread_id: {tid}");
                        }
                        if let Some(path) = summary.thread_path.as_deref() {
                            println!("thread_path: {}", path);
                        }
                    }
                }
                CodexBridgeCommands::Run {
                    session_id,
                    cwd,
                    url,
                    token,
                    codex_bin,
                    session_source,
                    approval_policy,
                    sandbox,
                    model,
                    model_reasoning_effort,
                    machine_name,
                    auto_approve,
                    longhouse_home,
                    state_file,
                    log_file,
                    create_initial_thread,
                    launch_mode,
                    start_thread,
                } => {
                    let (create_initial_thread, launch_mode) = codex_bridge_run_semantics(
                        start_thread,
                        create_initial_thread,
                        &launch_mode,
                    )?;
                    rt.block_on(cmd_codex_bridge_run(BridgeRunConfig {
                        session_id,
                        cwd,
                        api_url: url,
                        api_token: token,
                        codex_bin,
                        session_source,
                        approval_policy,
                        sandbox,
                        model,
                        model_reasoning_effort,
                        machine_name,
                        auto_approve,
                        longhouse_home,
                        state_file,
                        log_file,
                        create_initial_thread,
                        launch_mode,
                    }))?;
                }
                CodexBridgeCommands::Attach {
                    session_id,
                    state_root,
                    codex_bin,
                } => {
                    let exit_code = cmd_codex_bridge_attach(BridgeAttachConfig {
                        session_id,
                        state_root,
                        codex_bin,
                    })?;
                    if exit_code != 0 {
                        std::process::exit(exit_code);
                    }
                }
                CodexBridgeCommands::Send {
                    session_id,
                    text,
                    state_root,
                    allow_direct_ws_fallback,
                    attachments_json,
                    json,
                } => {
                    let attachments = parse_attachments_cli_arg(attachments_json.as_deref())?;
                    let summary = rt.block_on(cmd_codex_bridge_send(BridgeSendConfig {
                        session_id,
                        text,
                        state_root,
                        allow_direct_ws_fallback,
                        attachments,
                    }))?;
                    if json {
                        println!("{}", serde_json::to_string_pretty(&summary)?);
                    } else {
                        println!("session_id: {}", summary.session_id);
                        println!("thread_id: {}", summary.thread_id);
                        println!("turn_id: {}", summary.turn_id);
                        println!("turn_status: {}", summary.turn_status);
                    }
                }
                CodexBridgeCommands::Interrupt {
                    session_id,
                    state_root,
                } => {
                    rt.block_on(cmd_codex_bridge_interrupt(BridgeInterruptConfig {
                        session_id,
                        state_root,
                    }))?;
                }
                CodexBridgeCommands::Steer {
                    session_id,
                    text,
                    state_root,
                    attachments_json,
                } => {
                    let attachments = parse_attachments_cli_arg(attachments_json.as_deref())?;
                    let res = rt.block_on(cmd_codex_bridge_steer(BridgeSteerConfig {
                        session_id,
                        text,
                        state_root,
                        attachments,
                    }));
                    match res {
                        Ok(()) => {}
                        Err(BridgeSteerError::NoActiveTurn) => {
                            // Structured product-level signal: the turn
                            // the caller expected has already ended. Use
                            // exit code 2 so the backend can distinguish
                            // this from a generic failure without parsing
                            // stderr.
                            eprintln!("error_code: turn_ended");
                            std::process::exit(2);
                        }
                        Err(BridgeSteerError::TurnEnded(msg)) => {
                            eprintln!("error_code: turn_ended");
                            eprintln!("error_detail: {msg}");
                            std::process::exit(2);
                        }
                        Err(err) => return Err(anyhow::anyhow!(err)),
                    }
                }
                CodexBridgeCommands::Stop {
                    session_id,
                    state_root,
                    reason,
                } => {
                    rt.block_on(cmd_codex_bridge_stop(BridgeStopConfig {
                        session_id,
                        state_root,
                        terminal_reason: reason,
                    }))?;
                }
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_bridge_isolation_root_sets_both_roots() {
        let root = PathBuf::from("/tmp/lh-codex-bridge-test");
        let (state_root, longhouse_home) =
            resolve_codex_bridge_start_roots(None, None, Some(root.clone())).unwrap();

        assert_eq!(state_root, Some(root.join("codex-bridge")));
        assert_eq!(longhouse_home, Some(root.join("longhouse")));
    }

    #[test]
    fn codex_bridge_state_root_requires_longhouse_home() {
        let err = resolve_codex_bridge_start_roots(Some(PathBuf::from("/tmp/state")), None, None)
            .unwrap_err()
            .to_string();

        assert!(err.contains("--state-root only isolates bridge files"));
    }

    #[test]
    fn codex_bridge_explicit_roots_pass_through() {
        let state = PathBuf::from("/tmp/state");
        let home = PathBuf::from("/tmp/home");
        let (state_root, longhouse_home) =
            resolve_codex_bridge_start_roots(Some(state.clone()), Some(home.clone()), None)
                .unwrap();

        assert_eq!(state_root, Some(state));
        assert_eq!(longhouse_home, Some(home));
    }

    #[test]
    fn codex_bridge_start_create_initial_thread_keeps_tui_launch_mode() {
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "start",
            "--session-id",
            "00000000-0000-0000-0000-000000000001",
            "--cwd",
            "/tmp",
            "--url",
            "https://longhouse.test",
            "--token",
            "token",
            "--create-initial-thread",
        ])
        .unwrap();

        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Start {
                        create_initial_thread,
                        start_thread,
                        ..
                    },
            } => {
                let (create_initial_thread, launch_mode) =
                    codex_bridge_start_semantics(start_thread, create_initial_thread);
                assert!(create_initial_thread);
                assert_eq!(launch_mode, BridgeLaunchMode::Tui);
            }
            _ => panic!("expected codex-bridge start command"),
        }
    }

    #[test]
    fn codex_bridge_start_thread_keeps_detached_ui_compatibility() {
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "start",
            "--session-id",
            "00000000-0000-0000-0000-000000000001",
            "--cwd",
            "/tmp",
            "--url",
            "https://longhouse.test",
            "--token",
            "token",
            "--start-thread",
        ])
        .unwrap();

        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Start {
                        create_initial_thread,
                        start_thread,
                        ..
                    },
            } => {
                let (create_initial_thread, launch_mode) =
                    codex_bridge_start_semantics(start_thread, create_initial_thread);
                assert!(create_initial_thread);
                assert_eq!(launch_mode, BridgeLaunchMode::DetachedUi);
            }
            _ => panic!("expected codex-bridge start command"),
        }
    }

    #[test]
    fn codex_bridge_send_direct_ws_fallback_defaults_off() {
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "send",
            "--session-id",
            "sess-test",
            "--text",
            "hello",
        ])
        .unwrap();

        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Send {
                        allow_direct_ws_fallback,
                        ..
                    },
            } => assert!(!allow_direct_ws_fallback),
            _ => panic!("expected codex-bridge send command"),
        }
    }

    #[test]
    fn codex_bridge_send_accepts_explicit_direct_ws_fallback() {
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "send",
            "--session-id",
            "sess-test",
            "--text",
            "hello",
            "--allow-direct-ws-fallback",
        ])
        .unwrap();

        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Send {
                        allow_direct_ws_fallback,
                        ..
                    },
            } => assert!(allow_direct_ws_fallback),
            _ => panic!("expected codex-bridge send command"),
        }
    }

    #[test]
    fn codex_bridge_send_parses_attachments_json() {
        let attach_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let blob_url = format!(
            "/api/agents/sessions/{}/inputs/1/attachments/{}/blob",
            session_id, attach_id
        );
        let attachments = serde_json::json!([
            {
                "id": attach_id,
                "mime_type": "image/png",
                "sha256": "a".repeat(64),
                "blob_url": blob_url,
            }
        ])
        .to_string();
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "send",
            "--session-id",
            "sess-test",
            "--text",
            "look at this",
            "--attachments-json",
            &attachments,
        ])
        .unwrap();
        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Send {
                        attachments_json, ..
                    },
            } => {
                let parsed = parse_attachments_cli_arg(attachments_json.as_deref()).unwrap();
                assert_eq!(parsed.len(), 1);
                assert_eq!(parsed[0].id, attach_id);
                assert_eq!(parsed[0].blob_url, blob_url);
            }
            _ => panic!("expected codex-bridge send command"),
        }
    }

    #[test]
    fn parse_attachments_cli_arg_handles_missing_and_empty() {
        assert!(parse_attachments_cli_arg(None).unwrap().is_empty());
        assert!(parse_attachments_cli_arg(Some("")).unwrap().is_empty());
        assert!(parse_attachments_cli_arg(Some("   ")).unwrap().is_empty());
        assert!(parse_attachments_cli_arg(Some("[]")).unwrap().is_empty());
    }

    #[test]
    fn parse_attachments_cli_arg_rejects_invalid_json() {
        assert!(parse_attachments_cli_arg(Some("not json")).is_err());
    }

    #[test]
    fn codex_bridge_steer_accepts_attachments_json() {
        let attach_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let blob_url = format!(
            "/api/agents/sessions/{}/inputs/2/attachments/{}/blob",
            session_id, attach_id
        );
        let attachments = serde_json::json!([
            {
                "id": attach_id,
                "mime_type": "image/jpeg",
                "sha256": "b".repeat(64),
                "blob_url": blob_url,
            }
        ])
        .to_string();
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "steer",
            "--session-id",
            "sess-test",
            "--text",
            "fine-tune",
            "--attachments-json",
            &attachments,
        ])
        .unwrap();
        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Steer {
                        attachments_json, ..
                    },
            } => {
                let parsed = parse_attachments_cli_arg(attachments_json.as_deref()).unwrap();
                assert_eq!(parsed.len(), 1);
                assert_eq!(parsed[0].id, attach_id);
            }
            _ => panic!("expected codex-bridge steer command"),
        }
    }

    #[test]
    fn codex_bridge_stop_accepts_terminal_reason() {
        let cli = Cli::try_parse_from([
            "longhouse-engine",
            "codex-bridge",
            "stop",
            "--session-id",
            "sess-test",
            "--reason",
            "terminal_disconnected",
        ])
        .unwrap();

        match cli.command {
            Commands::CodexBridge {
                command:
                    CodexBridgeCommands::Stop {
                        session_id, reason, ..
                    },
            } => {
                assert_eq!(session_id, "sess-test");
                assert_eq!(reason.as_deref(), Some("terminal_disconnected"));
            }
            _ => panic!("expected codex-bridge stop command"),
        }
    }
}
