//! Parse command — parse and optionally compress a JSONL file.

use std::path::PathBuf;
use std::time::Instant;

use crate::pipeline::compressor::{self, CompressionAlgo};

pub fn cmd_parse(
    path: &PathBuf,
    offset: u64,
    dump_events: bool,
    compress: bool,
) -> anyhow::Result<()> {
    let start = Instant::now();

    let file_size = std::fs::metadata(path)?.len();
    eprintln!(
        "Parsing {} ({:.2} MB) from offset {}",
        path.display(),
        file_size as f64 / 1_048_576.0,
        offset
    );

    let parse_start = Instant::now();
    let result = crate::pipeline::parser::parse_session_file(path, offset)?;
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
        let compressed = compressor::build_and_compress_with_source_lines(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
            Some(&result.source_lines),
            None,
            CompressionAlgo::Zstd,
        )?;
        let compress_elapsed = compress_start.elapsed();

        let payload = compressor::build_payload_with_source_lines(
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
            "Compressed: {:.2} MB JSON → {:.2} MB zstd ({:.1}x ratio) in {:.3}s",
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
    if dump_events {
        eprintln!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        println!("{}", serde_json::to_string_pretty(&summary)?);
    }

    Ok(())
}
