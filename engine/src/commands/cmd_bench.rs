//! Bench command — benchmark file parsing and compression.

use std::path::PathBuf;

use crate::pipeline::compressor::CompressionAlgo;

#[allow(clippy::too_many_arguments)]
pub fn cmd_bench(
    level: &str,
    compress: bool,
    parallel: bool,
    workers: usize,
    algo: CompressionAlgo,
    ship_url: Option<&str>,
    ship_token: Option<&str>,
    ship_concurrency: usize,
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

    let mode_label = match (parallel, ship_url) {
        (_, Some(url)) => format!("ship -> {} (concurrency {})", url, ship_concurrency),
        (true, None) => format!("parallel ({} workers)", num_workers),
        (false, None) => "sequential".to_string(),
    };

    eprintln!(
        "\n--- {} benchmark: {} files, {:.2} GB ---",
        level.to_uppercase(),
        files.len(),
        sample_bytes as f64 / 1_073_741_824.0
    );
    eprintln!(
        "Mode: {}, Compress: {}",
        mode_label,
        if compress { "yes" } else { "parse-only" }
    );

    if let Some(url) = ship_url {
        let token = ship_token
            .ok_or_else(|| anyhow::anyhow!("--ship-token is required when --ship-url is set"))?;
        let result = crate::bench::run_benchmark_ship(&files, url, token, ship_concurrency, algo)?;
        result.print_summary();
    } else if parallel {
        let result = crate::bench::run_benchmark_parallel_with(&files, compress, num_workers, algo);
        result.print_summary();
    } else {
        let result = crate::bench::run_benchmark_with(&files, compress, algo);
        result.print_summary();
    }

    Ok(())
}
