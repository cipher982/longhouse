mod bench;
mod codex_app_server_canary;
mod codex_bridge;
mod config;
mod daemon;
mod discovery;
mod error_tracker;
mod heartbeat;
mod outbox;
mod pipeline;
mod scheduler;
mod shipper;
mod shipping;
mod state;
mod watcher;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::Instant;

use clap::{Parser, Subcommand};
use rayon::prelude::*;

use codex_app_server_canary::{
    parse_app_server_transport, run as run_codex_app_server_canary, CanaryConfig,
};
use codex_bridge::{
    cmd_codex_bridge_attach, cmd_codex_bridge_interrupt, cmd_codex_bridge_run,
    cmd_codex_bridge_send, cmd_codex_bridge_start, BridgeAttachConfig, BridgeInterruptConfig,
    BridgeRunConfig, BridgeSendConfig, BridgeStartConfig,
};
use config::ShipperConfig;
use pipeline::compressor::CompressionAlgo;
use shipping::client::ShipperClient;
use state::db::open_db;
use state::file_state::FileState;
use state::spool::Spool;

fn parse_compression_algo(s: &str) -> anyhow::Result<CompressionAlgo> {
    match s.to_lowercase().as_str() {
        "gzip" | "gz" => Ok(CompressionAlgo::Gzip),
        "zstd" | "zstandard" => Ok(CompressionAlgo::Zstd),
        _ => anyhow::bail!("Unknown compression: {}. Use 'gzip' or 'zstd'", s),
    }
}

#[derive(Parser)]
#[command(
    name = "longhouse-engine",
    version,
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

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "gzip")]
        compression: String,
    },

    /// Daemon mode: watch for file changes and ship incrementally
    Connect {
        /// API URL override (default: from ~/.claude/longhouse-url)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.claude/longhouse-device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "zstd")]
        compression: String,

        /// Flush interval in milliseconds (how long to coalesce file events)
        #[arg(long, default_value = "500")]
        flush_ms: u64,

        /// Fallback full scan interval in seconds
        #[arg(long, default_value = "300")]
        fallback_scan_secs: u64,

        /// Spool replay interval in seconds
        #[arg(long, default_value = "30")]
        spool_replay_secs: u64,

        /// Maximum compressed batch size in bytes before splitting/dead-lettering
        #[arg(long)]
        max_batch_bytes: Option<u64>,

        /// Log directory for rolling log files (default: ~/.claude/logs, or LONGHOUSE_LOG_DIR env)
        #[arg(long)]
        log_dir: Option<PathBuf>,

        /// Human-readable name for this machine (default: from ~/.claude/longhouse-machine-name or hostname)
        #[arg(long)]
        machine_name: Option<String>,
    },

    /// One-shot: scan all provider sessions and ship new events
    Ship {
        /// API URL override (default: from ~/.claude/longhouse-url)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.claude/longhouse-device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Ship a single file instead of scanning all providers
        #[arg(long)]
        file: Option<PathBuf>,

        /// Provider name override when using --file (claude, codex, gemini)
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

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "gzip")]
        compression: String,

        /// Maximum compressed batch size in bytes before splitting/dead-lettering
        #[arg(long)]
        max_batch_bytes: Option<u64>,

        /// Human-readable name for this machine (default: from ~/.claude/longhouse-machine-name or hostname)
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

        /// Provider name (claude, codex, gemini)
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
        machine_name: Option<String>,

        #[arg(long)]
        auto_approve: bool,

        #[arg(long)]
        state_root: Option<PathBuf>,

        #[arg(long)]
        log_file: Option<PathBuf>,

        #[arg(long, default_value_t = 25)]
        start_timeout_secs: u64,

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
        machine_name: Option<String>,

        #[arg(long)]
        auto_approve: bool,

        #[arg(long)]
        state_file: PathBuf,

        #[arg(long)]
        log_file: PathBuf,
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
}

fn resolve_log_dir(log_dir_arg: Option<&std::path::Path>) -> std::path::PathBuf {
    if let Some(p) = log_dir_arg {
        return p.to_path_buf();
    }
    if let Ok(dir) = std::env::var("LONGHOUSE_LOG_DIR") {
        return std::path::PathBuf::from(dir);
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    std::path::PathBuf::from(home).join(".claude").join("logs")
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

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // For Connect (daemon) mode: use rolling file appender.
    // For all other commands: log to stderr as usual.
    let _guard;
    match &cli.command {
        Commands::Connect { log_dir, .. } => {
            let log_path = resolve_log_dir(log_dir.as_deref());
            std::fs::create_dir_all(&log_path)?;
            prune_old_logs(&log_path, 7);

            let file_appender = tracing_appender::rolling::daily(&log_path, "engine.log");
            let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
            _guard = Some(guard);

            tracing_subscriber::fmt()
                .with_writer(non_blocking)
                .with_ansi(false)
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("longhouse_engine=info".parse()?),
                )
                .init();
        }
        _ => {
            _guard = None;
            tracing_subscriber::fmt()
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("longhouse_engine=info".parse()?),
                )
                .init();
        }
    }

    match cli.command {
        Commands::Connect {
            url,
            token,
            db,
            compression,
            flush_ms,
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
                flush_interval: std::time::Duration::from_millis(flush_ms),
                fallback_scan_secs,
                spool_replay_secs,
            };

            // Use current_thread runtime for minimal resource usage
            let rt = tokio::runtime::Builder::new_current_thread()
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
            cmd_parse(&path, offset, dump_events, compress)?;
        }
        Commands::Bench {
            level,
            compress,
            parallel,
            workers,
            compression,
        } => {
            let algo = parse_compression_algo(&compression)?;
            cmd_bench(&level, compress, parallel, workers, algo)?;
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
                // Load from config file (reads ~/.claude/longhouse-machine-name)
                let cfg = ShipperConfig::from_env().unwrap_or_default();
                pipeline::compressor::set_machine_name(&cfg.machine_name);
            }
            // Build tokio runtime for async HTTP
            let rt = tokio::runtime::Runtime::new()?;
            if let Some(path) = file.as_ref() {
                rt.block_on(cmd_ship_file(
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
                rt.block_on(cmd_ship(
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
            probe_thread_read,
            probe_thread_list,
            event_timeout_secs,
            log_jsonl,
            json,
            real_home,
            keep_home,
            verify_hooks,
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
                probe_thread_read,
                probe_thread_list,
                event_timeout_secs,
                log_jsonl,
                isolate_home: !real_home,
                keep_home,
                verify_hooks,
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
                    machine_name,
                    auto_approve,
                    state_root,
                    log_file,
                    start_timeout_secs,
                    json,
                } => {
                    let summary = rt.block_on(cmd_codex_bridge_start(BridgeStartConfig {
                        session_id,
                        cwd,
                        api_url: url,
                        api_token: token,
                        codex_bin,
                        approval_policy,
                        sandbox,
                        model,
                        machine_name,
                        auto_approve,
                        state_root,
                        log_file,
                        start_timeout_secs,
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
                    approval_policy: _,
                    sandbox: _,
                    model: _,
                    machine_name,
                    auto_approve,
                    state_file,
                    log_file,
                } => {
                    rt.block_on(cmd_codex_bridge_run(BridgeRunConfig {
                        session_id,
                        cwd,
                        api_url: url,
                        api_token: token,
                        codex_bin,
                        session_source,
                        machine_name,
                        auto_approve,
                        state_file,
                        log_file,
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
                    json,
                } => {
                    let summary = rt.block_on(cmd_codex_bridge_send(BridgeSendConfig {
                        session_id,
                        text,
                        state_root,
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
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// ship subcommand
// ---------------------------------------------------------------------------

async fn cmd_ship(
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    workers: usize,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
) -> anyhow::Result<()> {
    let start = Instant::now();

    // Load config
    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        if workers > 0 { Some(workers) } else { None },
        None,
        max_batch_bytes,
    );
    pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping to: {}", config.api_url);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    // Open state DB
    let conn = open_db(config.db_path.as_deref())?;

    let recovered = shipper::run_startup_recovery(&conn)?;
    if recovered > 0 && !json_output {
        eprintln!("Recovered {} unacked file gaps into spool", recovered);
    }

    // Discover files
    let all_files = bench::discover_session_files();
    if !json_output {
        eprintln!("Found {} session files", all_files.len());
    }

    // Filter to files with new content
    let file_state = FileState::new(&conn);
    let mut files_to_ship: Vec<(PathBuf, u64)> = Vec::new(); // (path, offset_to_start_from)

    for path in &all_files {
        let path_str = path.to_string_lossy();
        let current_offset = file_state.get_offset(&path_str)?;
        let file_size = match std::fs::metadata(path) {
            Ok(m) => m.len(),
            Err(_) => continue,
        };

        if file_size < current_offset {
            // File truncated (got smaller than our offset) — reset and re-ship
            tracing::warn!(
                "File truncated: {} (was {}, now {}), resetting",
                path_str,
                current_offset,
                file_size
            );
            file_state.reset_offsets(&path_str)?;
            files_to_ship.push((path.clone(), 0));
        } else if file_size > current_offset {
            // New content available
            files_to_ship.push((path.clone(), current_offset));
        }
        // file_size == current_offset: no new content, skip
    }

    if !json_output {
        eprintln!("{} files with new content to ship", files_to_ship.len());
    }

    if files_to_ship.is_empty() {
        let spool = Spool::new(&conn);
        let spool_pending = spool.pending_count()?;
        let spool_dead = spool.dead_count()?;
        let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "files_scanned": all_files.len(),
                "files_shipped": 0,
                "events_shipped": 0,
                "spool_pending": spool_pending,
                "spool_dead": spool_dead,
                "recent_dead_letters": recent_dead_letters,
                "total_seconds": start.elapsed().as_secs_f64(),
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            eprintln!("Nothing to ship — all files up to date.");
            if spool_dead > 0 {
                eprintln!("Dead-lettered ranges retained: {}", spool_dead);
                print_recent_dead_letters(&spool, 3, |line| eprintln!("{}", line))?;
            }
        }
        return Ok(());
    }

    // Create HTTP client (unless dry run)
    let client = if !dry_run {
        Some(ShipperClient::with_compression(&config, algo)?)
    } else {
        None
    };

    // Configure rayon thread pool
    let num_workers = if workers > 0 {
        workers
    } else {
        num_cpus::get()
    };
    rayon::ThreadPoolBuilder::new()
        .num_threads(num_workers)
        .build_global()
        .ok(); // Ignore if already initialized

    if !json_output {
        eprintln!(
            "Processing with {} workers{}",
            num_workers,
            if dry_run { " (dry run)" } else { "" }
        );
    }

    // Phase 1: Parse + compress in parallel (CPU-bound, embarrassingly parallel)
    // Collect results for sequential state writes + HTTP shipping.
    let files_done = AtomicUsize::new(0);
    let bytes_done = AtomicU64::new(0);
    let events_done = AtomicUsize::new(0);
    let total_files = files_to_ship.len();

    let prepared_files: Vec<Option<shipper::PreparedFile>> = files_to_ship
        .par_iter()
        .map(|(path, offset)| {
            let path_str = path.to_string_lossy().to_string();
            if std::fs::metadata(path).is_err() {
                return None;
            }

            let prepared = match shipper::prepare_path_from_offset(
                path,
                "claude",
                *offset,
                algo,
                config.max_batch_bytes,
            ) {
                Ok(result) => result,
                Err(e) => {
                    tracing::warn!("Skip {}: {}", path_str, e);
                    return None;
                }
            };

            let prepared = match prepared {
                Some(prepared) => prepared,
                None => return None,
            };
            let event_count = prepared.total_event_count();
            let new_offset = prepared.new_offset;

            // Progress reporting
            let done = files_done.fetch_add(1, Ordering::Relaxed) + 1;
            bytes_done.fetch_add(new_offset.saturating_sub(*offset), Ordering::Relaxed);
            events_done.fetch_add(event_count, Ordering::Relaxed);

            if !json_output && (done % 1000 == 0 || done == total_files) {
                let elapsed = start.elapsed().as_secs_f64();
                let mb = bytes_done.load(Ordering::Relaxed) as f64 / 1_048_576.0;
                let evts = events_done.load(Ordering::Relaxed);
                eprintln!(
                    "  [{}/{}] {} events, {:.1} MB, {:.1} MB/s",
                    done,
                    total_files,
                    evts,
                    mb,
                    mb / elapsed,
                );
            }

            Some(prepared)
        })
        .collect();

    let parse_compress_elapsed = start.elapsed();
    if !json_output {
        eprintln!(
            "Parse+compress done in {:.1}s, writing state...",
            parse_compress_elapsed.as_secs_f64()
        );
    }

    // Phase 2: Sequential state writes + HTTP shipping
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;
    let mut bytes_shipped = 0u64;
    let mut files_failed = 0usize;
    let mut files_skipped = 0usize;

    if dry_run {
        for prepared in &prepared_files {
            match prepared {
                Some(prepared) => {
                    files_shipped += 1;
                    events_shipped += prepared.total_event_count();
                    bytes_shipped += prepared.new_offset - prepared.offset;
                }
                None => {
                    files_skipped += 1;
                }
            }
        }
    }

    // Live HTTP shipping (skip if dry run — already handled above)
    if !dry_run {
        for prepared in prepared_files {
            let prepared = match prepared {
                Some(prepared) => prepared,
                None => {
                    files_skipped += 1;
                    continue;
                }
            };

            let client = client.as_ref().unwrap();
            let outcome = shipper::ship_prepared_file(prepared, client, &conn, None).await?;
            if outcome.events_shipped > 0 || outcome.dead_lettered > 0 {
                files_shipped += 1;
            }
            events_shipped += outcome.events_shipped;
            bytes_shipped += outcome.bytes_shipped;
            if !outcome.fully_processed {
                files_failed += 1;
            }
        }
    } // end if !dry_run

    // Replay spool (if not dry run)
    let mut spool_replayed = 0usize;
    if !dry_run {
        let client = client.as_ref().unwrap();
        let (ok, _failed) = shipper::replay_spool_batch_with_batch_bytes(
            &conn,
            client,
            algo,
            100,
            config.max_batch_bytes,
        )
        .await?;
        spool_replayed = ok;
    }

    let total_elapsed = start.elapsed();
    let spool = Spool::new(&conn);
    let spool_pending = spool.pending_count()?;
    let spool_dead = spool.dead_count()?;
    let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;

    if json_output {
        let summary = serde_json::json!({
            "status": "ok",
            "files_scanned": all_files.len(),
            "files_shipped": files_shipped,
            "files_failed": files_failed,
            "files_skipped": files_skipped,
            "events_shipped": events_shipped,
            "bytes_shipped": bytes_shipped,
            "spool_replayed": spool_replayed,
            "spool_pending": spool_pending,
            "spool_dead": spool_dead,
            "recent_dead_letters": recent_dead_letters,
            "total_seconds": total_elapsed.as_secs_f64(),
            "throughput_mb_s": bytes_shipped as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
            "dry_run": dry_run,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        eprintln!("\n=== Ship Results ===");
        eprintln!("Files shipped: {}", files_shipped);
        eprintln!("Events shipped: {}", events_shipped);
        eprintln!(
            "Bytes shipped: {:.2} MB",
            bytes_shipped as f64 / 1_048_576.0
        );
        if files_failed > 0 {
            eprintln!("Files failed (spooled): {}", files_failed);
        }
        if spool_replayed > 0 {
            eprintln!("Spool replayed: {}", spool_replayed);
        }
        if spool_dead > 0 {
            eprintln!("Dead-lettered ranges retained: {}", spool_dead);
            print_recent_dead_letters(&spool, 3, |line| eprintln!("{}", line))?;
        }
        eprintln!("Total: {:.3}s", total_elapsed.as_secs_f64());
        if bytes_shipped > 0 {
            eprintln!(
                "Throughput: {:.1} MB/s",
                bytes_shipped as f64 / 1_048_576.0 / total_elapsed.as_secs_f64()
            );
        }
    }

    Ok(())
}

fn detect_provider_for_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
) -> anyhow::Result<String> {
    if let Some(p) = provider_override {
        return Ok(p.to_lowercase());
    }

    let providers = discovery::get_providers();
    if let Some(p) = discovery::provider_for_path(path, &providers) {
        return Ok(p.to_string());
    }

    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_lowercase());

    match ext.as_deref() {
        Some("jsonl") => Ok("claude".to_string()),
        Some("json") => Ok("gemini".to_string()),
        _ => anyhow::bail!(
            "Unable to determine provider for {} (use --provider)",
            path.display()
        ),
    }
}

fn recent_dead_letter_json(
    spool: &Spool<'_>,
    limit: usize,
) -> anyhow::Result<Vec<serde_json::Value>> {
    Ok(spool
        .recent_dead(limit)?
        .into_iter()
        .map(|entry| {
            serde_json::json!({
                "provider": entry.provider,
                "file_path": entry.file_path,
                "start_offset": entry.start_offset,
                "end_offset": entry.end_offset,
                "range_bytes": entry.end_offset.saturating_sub(entry.start_offset),
                "session_id": entry.session_id,
                "last_error": entry.last_error,
                "created_at": entry.created_at,
            })
        })
        .collect())
}

fn print_recent_dead_letters(
    spool: &Spool<'_>,
    limit: usize,
    printer: impl Fn(&str),
) -> anyhow::Result<()> {
    for entry in spool.recent_dead(limit)? {
        let reason = entry.last_error.as_deref().unwrap_or("no error recorded");
        let line = format!(
            "  - [{}] {} {}..{} ({} bytes): {}",
            entry.provider,
            entry.file_path,
            entry.start_offset,
            entry.end_offset,
            entry.end_offset.saturating_sub(entry.start_offset),
            reason
        );
        printer(&line);
    }
    Ok(())
}

fn reported_ship_events(
    replay_events_shipped: usize,
    shipped_events: usize,
    require_reply_evidence: bool,
    reply_evidence_pending: bool,
) -> usize {
    if require_reply_evidence && reply_evidence_pending {
        return 0;
    }
    replay_events_shipped + shipped_events
}

async fn cmd_ship_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
    session_id_override: Option<&str>,
    require_reply_evidence: bool,
) -> anyhow::Result<()> {
    if !path.exists() {
        anyhow::bail!("File not found: {}", path.display());
    }

    let provider = detect_provider_for_file(path, provider_override)?;

    let config =
        ShipperConfig::from_env()?.with_overrides(url, token, db_path, None, None, max_batch_bytes);
    pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping file: {}", path.display());
        eprintln!("Provider: {}", provider);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    let conn = open_db(config.db_path.as_deref())?;

    let mut prepared = shipper::prepare_file_batches(
        path,
        &provider,
        algo,
        &conn,
        config.max_batch_bytes,
        session_id_override,
    )?;
    let mut reply_evidence_pending = false;
    if require_reply_evidence
        && prepared
            .as_ref()
            .is_some_and(|item| !item.has_reply_evidence)
    {
        reply_evidence_pending = true;
        prepared = None;
    }
    let mut replay_events_shipped = 0usize;
    let mut replay_failed = 0usize;
    let mut replay_had_connect_error = false;
    let mut replay_blocked_by_spool_backpressure = false;

    if prepared.is_none() && !dry_run {
        let path_str = path.to_string_lossy().to_string();
        let recovered_gap = shipper::recover_gap_for_path(&conn, path)?;
        let has_pending_replay = !Spool::new(&conn)
            .pending_entries_for_path_now(&path_str, 1)?
            .is_empty();
        replay_blocked_by_spool_backpressure =
            recovered_gap.had_gap && !recovered_gap.replay_ready && !has_pending_replay;
        if recovered_gap.replay_ready || has_pending_replay {
            let client = ShipperClient::with_compression(&config, algo)?;
            let replay = shipper::replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                &conn,
                &client,
                algo,
                path,
                32,
                config.max_batch_bytes,
                None,
            )
            .await?;
            replay_events_shipped = replay.events_shipped;
            replay_failed = replay.failed;
            replay_had_connect_error = replay.had_connect_error;
            prepared = shipper::prepare_file_batches(
                path,
                &provider,
                algo,
                &conn,
                config.max_batch_bytes,
                session_id_override,
            )?;
            if require_reply_evidence
                && prepared
                    .as_ref()
                    .is_some_and(|item| !item.has_reply_evidence)
            {
                reply_evidence_pending = true;
                prepared = None;
            }
        }
    }

    if dry_run {
        let prepared = match prepared {
            Some(prepared) => prepared,
            None => {
                let spool = Spool::new(&conn);
                let spool_dead = spool.dead_count()?;
                let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
                if json_output {
                    let summary = serde_json::json!({
                        "status": "ok",
                        "file": path.display().to_string(),
                        "events_shipped": 0,
                        "spool_dead": spool_dead,
                        "recent_dead_letters": recent_dead_letters,
                        "dry_run": true,
                        "reply_evidence_pending": reply_evidence_pending,
                    });
                    println!("{}", serde_json::to_string_pretty(&summary)?);
                } else {
                    println!("No new events");
                    if spool_dead > 0 {
                        println!("Dead-lettered ranges retained: {}", spool_dead);
                        print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                    }
                }
                return Ok(());
            }
        };
        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "file": prepared.path_str,
                "events_shipped": prepared.total_event_count(),
                "dry_run": true,
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            println!("Would ship {} events", prepared.total_event_count());
        }
        return Ok(());
    }

    let client = ShipperClient::with_compression(&config, algo)?;
    let prepared = match prepared {
        Some(prepared) => prepared,
        None => {
            let spool = Spool::new(&conn);
            let spool_dead = spool.dead_count()?;
            let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
            if json_output {
                let summary = serde_json::json!({
                    "status": "ok",
                    "file": path.display().to_string(),
                    "events_shipped": replay_events_shipped,
                    "spool_dead": spool_dead,
                    "recent_dead_letters": recent_dead_letters,
                    "dry_run": false,
                    "had_connect_error": replay_had_connect_error,
                    "replay_failed": replay_failed,
                    "spool_backpressure": replay_blocked_by_spool_backpressure,
                    "reply_evidence_pending": reply_evidence_pending,
                });
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else if replay_events_shipped > 0 {
                println!("Shipped {} events", replay_events_shipped);
                if replay_had_connect_error {
                    println!("Some replay work was deferred due to a connection error");
                } else if replay_failed > 0 {
                    println!(
                        "Some replay work was deferred after {} replay failure(s)",
                        replay_failed
                    );
                }
                if spool_dead > 0 {
                    println!("Dead-lettered ranges retained: {}", spool_dead);
                    print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                }
            } else {
                if replay_blocked_by_spool_backpressure {
                    println!("Replay deferred because the local spool is at capacity");
                } else if replay_had_connect_error {
                    println!("Replay deferred due to connection error");
                } else if replay_failed > 0 {
                    println!("Replay deferred after {} replay failure(s)", replay_failed);
                } else {
                    println!("No new events");
                }
                if spool_dead > 0 {
                    println!("Dead-lettered ranges retained: {}", spool_dead);
                    print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                }
            }
            return Ok(());
        }
    };
    let outcome = shipper::ship_prepared_file(prepared, &client, &conn, None).await?;
    let events_shipped = reported_ship_events(
        replay_events_shipped,
        outcome.events_shipped,
        require_reply_evidence,
        reply_evidence_pending,
    );
    let spool = Spool::new(&conn);
    let spool_dead = spool.dead_count()?;
    let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;

    if json_output {
        let summary = serde_json::json!({
            "status": "ok",
            "file": path.display().to_string(),
            "events_shipped": events_shipped,
            "replay_events_shipped": replay_events_shipped,
            "spool_dead": spool_dead,
            "recent_dead_letters": recent_dead_letters,
            "dry_run": false,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        println!("Shipped {} events", events_shipped);
        if spool_dead > 0 {
            println!("Dead-lettered ranges retained: {}", spool_dead);
            print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// parse subcommand
// ---------------------------------------------------------------------------

fn cmd_parse(path: &PathBuf, offset: u64, dump_events: bool, compress: bool) -> anyhow::Result<()> {
    let start = Instant::now();

    let file_size = std::fs::metadata(path)?.len();
    eprintln!(
        "Parsing {} ({:.2} MB) from offset {}",
        path.display(),
        file_size as f64 / 1_048_576.0,
        offset
    );

    let parse_start = Instant::now();
    let result = pipeline::parser::parse_session_file(path, offset)?;
    let parse_elapsed = parse_start.elapsed();

    eprintln!(
        "Parsed {} events, metadata extracted in {:.3}s",
        result.events.len(),
        parse_elapsed.as_secs_f64()
    );

    if let Some(ref meta) = result.metadata.cwd {
        eprintln!("  cwd: {}", meta);
    }
    if let Some(ref branch) = result.metadata.git_branch {
        eprintln!("  branch: {}", branch);
    }
    if let Some(ref started) = result.metadata.started_at {
        eprintln!("  started: {}", started);
    }
    if let Some(ref ended) = result.metadata.ended_at {
        eprintln!("  ended: {}", ended);
    }

    if dump_events {
        for event in &result.events {
            let json = serde_json::to_string(event)?;
            println!("{}", json);
        }
    }

    if compress {
        let compress_start = Instant::now();
        let source_path = path.to_string_lossy();
        let compressed = pipeline::compressor::build_and_compress_with_source_lines(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
            Some(&result.source_lines),
            None,
            CompressionAlgo::Gzip,
        )?;
        let compress_elapsed = compress_start.elapsed();

        // Calculate uncompressed size for ratio
        let payload = pipeline::compressor::build_payload_with_source_lines(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
            Some(&result.source_lines),
            None,
        );
        let uncompressed = serde_json::to_vec(&payload)?;

        eprintln!(
            "Compressed: {:.2} MB JSON → {:.2} MB gzip ({:.1}x ratio) in {:.3}s",
            uncompressed.len() as f64 / 1_048_576.0,
            compressed.len() as f64 / 1_048_576.0,
            uncompressed.len() as f64 / compressed.len() as f64,
            compress_elapsed.as_secs_f64()
        );
    }

    let bytes_processed = file_size - offset;
    let total_elapsed = start.elapsed();
    eprintln!(
        "\nTotal: {:.3}s, {:.1} MB/s, {} events/s",
        total_elapsed.as_secs_f64(),
        bytes_processed as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
        (result.events.len() as f64 / total_elapsed.as_secs_f64()) as u64
    );

    // Machine-readable JSON summary
    let summary = serde_json::json!({
        "file": path.display().to_string(),
        "file_size_bytes": file_size,
        "offset": offset,
        "bytes_processed": bytes_processed,
        "event_count": result.events.len(),
        "parse_seconds": parse_elapsed.as_secs_f64(),
        "total_seconds": total_elapsed.as_secs_f64(),
        "throughput_mb_s": bytes_processed as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
        "events_per_sec": (result.events.len() as f64 / total_elapsed.as_secs_f64()) as u64,
        "metadata": {
            "cwd": result.metadata.cwd,
            "git_branch": result.metadata.git_branch,
            "started_at": result.metadata.started_at.map(|t| t.to_rfc3339()),
            "ended_at": result.metadata.ended_at.map(|t| t.to_rfc3339()),
        }
    });
    // When --dump-events is active, stdout is a stream of event JSON lines.
    // Send the stats summary to stderr so callers can parse events cleanly.
    if dump_events {
        eprintln!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        println!("{}", serde_json::to_string_pretty(&summary)?);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn make_claude_file(dir: &tempfile::TempDir, name: &str, content: &str) -> PathBuf {
        let path = dir.path().join(name);
        std::fs::write(&path, content).unwrap();
        path
    }

    fn spawn_http_response_server(
        status_line: &str,
        body: &str,
    ) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let status_line = status_line.to_string();
        let body = body.to_string();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0_u8; 8192];
            let _ = stream.read(&mut buf);
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
                status_line,
                body.len(),
                body,
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        (format!("http://{}", addr), handle)
    }

    #[test]
    fn test_cmd_ship_file_dry_run_does_not_mutate_state() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "ffff1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"dry-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"dry-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            None,
            None,
            Some(&db_path),
            true,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            false,
        ))
        .unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        assert_eq!(
            file_state.get_offset(&file.to_string_lossy()).unwrap(),
            0,
            "dry-run should not advance acked_offset",
        );
        assert_eq!(
            file_state
                .get_queued_offset(&file.to_string_lossy())
                .unwrap(),
            0,
            "dry-run should not advance queued_offset",
        );
    }

    #[test]
    fn test_cmd_ship_file_recovers_explicit_file_gap_before_noop() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "eeee1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"gap-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"gap-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");
        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&file).unwrap().len();

        file_state
            .set_queued_offset(
                &file_str,
                file_len,
                "claude",
                "eeee1111-2222-3333-4444-555566667777",
                "eeee1111-2222-3333-4444-555566667777",
            )
            .unwrap();

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some(&url),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            false,
        ))
        .unwrap();
        handle.join().unwrap();

        let reopened = open_db(Some(&db_path)).unwrap();
        let reopened_state = FileState::new(&reopened);
        let spool = Spool::new(&reopened);
        assert_eq!(reopened_state.get_offset(&file_str).unwrap(), file_len);
        assert_eq!(
            reopened_state.get_queued_offset(&file_str).unwrap(),
            file_len
        );
        assert!(spool
            .pending_entries_for_path_now(&file_str, 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_cmd_ship_file_require_reply_evidence_skips_user_only_partial_turn() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "replyevidence1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"reply-gap-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"reply-gap-2","timestamp":"2026-02-15T10:00:01Z","message":{"con"#,
            ),
        );
        let db_path = dir.path().join("engine.db");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some("http://127.0.0.1:9"),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            true,
        ))
        .unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        assert_eq!(file_state.get_offset(&file_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), 0);
        assert!(spool
            .pending_entries_for_path_now(&file_str, 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_cmd_ship_file_require_reply_evidence_ships_complete_turn() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "replyevidence2222-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"reply-ok-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"reply-ok-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");
        let file_len = std::fs::metadata(&file).unwrap().len();
        let (url, handle) = spawn_http_response_server("200 OK", "{}");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some(&url),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            true,
        ))
        .unwrap();
        handle.join().unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        assert_eq!(file_state.get_offset(&file_str).unwrap(), file_len);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), file_len);
    }

    #[test]
    fn test_reported_ship_events_ignore_replay_while_reply_evidence_is_pending() {
        assert_eq!(reported_ship_events(3, 0, true, true), 0);
        assert_eq!(reported_ship_events(3, 0, true, false), 3);
        assert_eq!(reported_ship_events(0, 2, true, false), 2);
        assert_eq!(reported_ship_events(3, 2, false, true), 5);
    }
}

// ---------------------------------------------------------------------------
// bench subcommand
// ---------------------------------------------------------------------------

fn cmd_bench(
    level: &str,
    compress: bool,
    parallel: bool,
    workers: usize,
    algo: CompressionAlgo,
) -> anyhow::Result<()> {
    eprintln!("Discovering session files...");
    let all_files = bench::discover_session_files();
    eprintln!("Found {} non-empty JSONL files", all_files.len());

    let total_bytes: u64 = all_files
        .iter()
        .filter_map(|p| std::fs::metadata(p).ok())
        .map(|m| m.len())
        .sum();
    eprintln!(
        "Total: {:.2} GB on disk",
        total_bytes as f64 / 1_073_741_824.0
    );

    let files: Vec<PathBuf> = match level.to_uppercase().as_str() {
        "L1" => {
            // Single largest file
            all_files.into_iter().take(1).collect()
        }
        "L2" => {
            // 10% sample (top files by size)
            let count = (all_files.len() + 9) / 10;
            all_files.into_iter().take(count).collect()
        }
        "L3" => {
            // All files
            all_files
        }
        _ => {
            anyhow::bail!("Unknown level: {}. Use L1, L2, or L3", level);
        }
    };

    let sample_bytes: u64 = files
        .iter()
        .filter_map(|p| std::fs::metadata(p).ok())
        .map(|m| m.len())
        .sum();

    let num_workers = if workers == 0 {
        num_cpus::get()
    } else {
        workers
    };

    eprintln!(
        "\n--- {} benchmark: {} files, {:.2} GB ---",
        level.to_uppercase(),
        files.len(),
        sample_bytes as f64 / 1_073_741_824.0
    );
    eprintln!(
        "Mode: {}, Compress: {}",
        if parallel {
            format!("parallel ({} workers)", num_workers)
        } else {
            "sequential".to_string()
        },
        if compress { "yes" } else { "parse-only" }
    );

    let result = if parallel {
        bench::run_benchmark_parallel_with(&files, compress, num_workers, algo)
    } else {
        bench::run_benchmark_with(&files, compress, algo)
    };
    result.print_summary();

    Ok(())
}
