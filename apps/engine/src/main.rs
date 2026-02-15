mod bench;
mod pipeline;

use std::path::PathBuf;
use std::time::Instant;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "longhouse-engine", version, about = "Longhouse session shipper (Rust engine)")]
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

        /// Include gzip compression in benchmark
        #[arg(long)]
        compress: bool,
    },
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("longhouse_engine=info".parse()?),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Parse {
            path,
            offset,
            dump_events,
            compress,
        } => {
            cmd_parse(&path, offset, dump_events, compress)?;
        }
        Commands::Bench { level, compress } => {
            cmd_bench(&level, compress)?;
        }
    }

    Ok(())
}

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
        let compressed = pipeline::compressor::build_and_compress(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
        )?;
        let compress_elapsed = compress_start.elapsed();

        // Calculate uncompressed size for ratio
        let payload = pipeline::compressor::build_payload(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
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
    println!("{}", serde_json::to_string_pretty(&summary)?);

    Ok(())
}

fn cmd_bench(level: &str, compress: bool) -> anyhow::Result<()> {
    eprintln!("Discovering session files...");
    let all_files = bench::discover_session_files();
    eprintln!("Found {} non-empty JSONL files", all_files.len());

    let total_bytes: u64 = all_files
        .iter()
        .filter_map(|p| std::fs::metadata(p).ok())
        .map(|m| m.len())
        .sum();
    eprintln!("Total: {:.2} GB on disk", total_bytes as f64 / 1_073_741_824.0);

    let files: Vec<PathBuf> = match level.to_uppercase().as_str() {
        "L1" => {
            // Single largest file
            all_files.into_iter().take(1).collect()
        }
        "L2" => {
            // 10% sample (every 10th file, keeps size distribution)
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
    eprintln!(
        "\n--- {} benchmark: {} files, {:.2} GB ---",
        level.to_uppercase(),
        files.len(),
        sample_bytes as f64 / 1_073_741_824.0
    );
    eprintln!("Compress: {}", if compress { "yes" } else { "parse-only" });

    let result = bench::run_benchmark(&files, compress);
    result.print_summary();

    Ok(())
}
