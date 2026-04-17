//! Bench command — benchmark file parsing and compression.

use std::path::PathBuf;

use crate::pipeline::compressor::CompressionAlgo;

pub fn cmd_bench(
    level: &str,
    compress: bool,
    parallel: bool,
    workers: usize,
    algo: CompressionAlgo,
) -> anyhow::Result<()> {
    eprintln!("Discovering session files...");
    let all_files = crate::bench::discover_session_files();
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
        "L1" => all_files.into_iter().take(1).collect(),
        "L2" => {
            let count = (all_files.len() + 9) / 10;
            all_files.into_iter().take(count).collect()
        }
        "L3" => all_files,
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
        crate::bench::run_benchmark_parallel_with(&files, compress, num_workers, algo)
    } else {
        crate::bench::run_benchmark_with(&files, compress, algo)
    };
    result.print_summary();

    Ok(())
}
