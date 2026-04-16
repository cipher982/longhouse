mod bench;
mod codex_app_server_canary;
mod codex_bridge;
mod commands;
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
mod text;
mod watcher;

use std::path::PathBuf;

use clap::{Parser, Subcommand};

use codex_app_server_canary::{
    parse_app_server_transport, run as run_codex_app_server_canary, CanaryConfig,
};
use codex_bridge::{
    cmd_codex_bridge_attach, cmd_codex_bridge_interrupt, cmd_codex_bridge_run,
    cmd_codex_bridge_send, cmd_codex_bridge_start, cmd_codex_bridge_stop, BridgeAttachConfig,
    BridgeInterruptConfig, BridgeRunConfig, BridgeSendConfig, BridgeStartConfig, BridgeStopConfig,
};
use config::ShipperConfig;
use pipeline::compressor::CompressionAlgo;
use state::db::open_db;

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

    /// Stop a running managed bridge and its local Codex app-server child
    Stop {
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
            commands::cmd_parse::cmd_parse(&path, offset, dump_events, compress)?;
        }
        Commands::Bench {
            level,
            compress,
            parallel,
            workers,
            compression,
        } => {
            let algo = parse_compression_algo(&compression)?;
            commands::cmd_bench::cmd_bench(&level, compress, parallel, workers, algo)?;
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
                CodexBridgeCommands::Stop {
                    session_id,
                    state_root,
                } => {
                    rt.block_on(cmd_codex_bridge_stop(BridgeStopConfig {
                        session_id,
                        state_root,
                    }))?;
                }
            }
        }
    }

    Ok(())
}
