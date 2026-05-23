//! Multi-file benchmark harness for comparing against Python profiling baselines.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use rayon::prelude::*;

use crate::pipeline;
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipping::client::{ServerIngestTiming, ShipResult, ShipperClient};

pub struct BenchResult {
    pub files_processed: usize,
    pub total_bytes: u64,
    pub total_events: usize,
    pub parse_seconds: f64,
    pub compress_seconds: f64,
    pub total_seconds: f64,
    pub peak_rss_mb: f64,
    pub parallel: bool,
    pub workers: usize,
}

impl BenchResult {
    pub fn print_summary(&self) {
        let mb = self.total_bytes as f64 / 1_048_576.0;
        eprintln!("\n=== Benchmark Results ===");
        if self.parallel {
            eprintln!("Mode:       parallel ({} workers)", self.workers);
        } else {
            eprintln!("Mode:       sequential");
        }
        eprintln!("Files:      {}", self.files_processed);
        eprintln!("Bytes:      {:.2} MB", mb);
        eprintln!("Events:     {}", self.total_events);
        if !self.parallel {
            // Per-phase timing only meaningful in sequential mode
            eprintln!(
                "Parse:      {:.3}s ({:.1}%)",
                self.parse_seconds,
                self.parse_seconds / self.total_seconds * 100.0
            );
            eprintln!(
                "Compress:   {:.3}s ({:.1}%)",
                self.compress_seconds,
                self.compress_seconds / self.total_seconds * 100.0
            );
        }
        eprintln!("Total:      {:.3}s", self.total_seconds);
        eprintln!("Throughput: {:.1} MB/s", mb / self.total_seconds);
        eprintln!(
            "Events/s:   {:.0}",
            self.total_events as f64 / self.total_seconds
        );
        eprintln!("Peak RSS:   {:.1} MB", self.peak_rss_mb);
    }
}

/// Per-file result collected from parallel workers.
struct FileResult {
    bytes: u64,
    events: usize,
    parse_secs: f64,
    compress_secs: f64,
}

/// Run benchmark sequentially with specified compression.
pub fn run_benchmark_with(files: &[PathBuf], compress: bool, algo: CompressionAlgo) -> BenchResult {
    let overall_start = Instant::now();
    let mut total_bytes: u64 = 0;
    let mut total_events: usize = 0;
    let mut parse_time: f64 = 0.0;
    let mut compress_time: f64 = 0.0;
    let mut files_ok: usize = 0;

    for (i, path) in files.iter().enumerate() {
        let file_size = match std::fs::metadata(path) {
            Ok(m) => m.len(),
            Err(_) => continue,
        };

        let parse_start = Instant::now();
        let result = match pipeline::parser::parse_session_file(path, 0) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("  SKIP {}: {}", path.display(), e);
                continue;
            }
        };
        parse_time += parse_start.elapsed().as_secs_f64();

        if compress && !result.events.is_empty() {
            let compress_start = Instant::now();
            let source_path = path.to_string_lossy();
            let _ = pipeline::compressor::build_and_compress_with(
                &result.metadata.session_id,
                &result.events,
                &result.metadata,
                &source_path,
                "claude",
                None,
                algo,
            );
            compress_time += compress_start.elapsed().as_secs_f64();
        }

        total_bytes += file_size;
        total_events += result.events.len();
        files_ok += 1;

        if (i + 1) % 500 == 0 {
            let elapsed = overall_start.elapsed().as_secs_f64();
            let mb = total_bytes as f64 / 1_048_576.0;
            eprintln!(
                "  [{}/{}] {:.1} MB, {} events, {:.1} MB/s",
                i + 1,
                files.len(),
                mb,
                total_events,
                mb / elapsed,
            );
        }
    }

    let total_seconds = overall_start.elapsed().as_secs_f64();

    BenchResult {
        files_processed: files_ok,
        total_bytes,
        total_events,
        parse_seconds: parse_time,
        compress_seconds: compress_time,
        total_seconds,
        peak_rss_mb: get_rss_mb(),
        parallel: false,
        workers: 1,
    }
}

/// Run benchmark with rayon parallel file processing and specified compression.
pub fn run_benchmark_parallel_with(
    files: &[PathBuf],
    compress: bool,
    workers: usize,
    algo: CompressionAlgo,
) -> BenchResult {
    // Configure rayon thread pool
    rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build_global()
        .ok(); // Ignore if already initialized

    let overall_start = Instant::now();

    // Atomic counters for progress reporting
    let files_done = AtomicUsize::new(0);
    let bytes_done = AtomicU64::new(0);
    let events_done = AtomicUsize::new(0);
    let total_files = files.len();

    // Process files in parallel, collect results
    let results: Vec<FileResult> = files
        .par_iter()
        .filter_map(|path| {
            let file_size = match std::fs::metadata(path) {
                Ok(m) => m.len(),
                Err(_) => return None,
            };

            let parse_start = Instant::now();
            let result = match pipeline::parser::parse_session_file(path, 0) {
                Ok(r) => r,
                Err(_) => return None,
            };
            let parse_secs = parse_start.elapsed().as_secs_f64();

            let compress_secs = if compress && !result.events.is_empty() {
                let compress_start = Instant::now();
                let source_path = path.to_string_lossy();
                let _ = pipeline::compressor::build_and_compress_with(
                    &result.metadata.session_id,
                    &result.events,
                    &result.metadata,
                    &source_path,
                    "claude",
                    None,
                    algo,
                );
                compress_start.elapsed().as_secs_f64()
            } else {
                0.0
            };

            let event_count = result.events.len();

            // Update progress atomically
            let done = files_done.fetch_add(1, Ordering::Relaxed) + 1;
            bytes_done.fetch_add(file_size, Ordering::Relaxed);
            events_done.fetch_add(event_count, Ordering::Relaxed);

            if done % 1000 == 0 || done == total_files {
                let elapsed = overall_start.elapsed().as_secs_f64();
                let mb = bytes_done.load(Ordering::Relaxed) as f64 / 1_048_576.0;
                let evts = events_done.load(Ordering::Relaxed);
                eprintln!(
                    "  [{}/{}] {:.1} MB, {} events, {:.1} MB/s",
                    done,
                    total_files,
                    mb,
                    evts,
                    mb / elapsed,
                );
            }

            Some(FileResult {
                bytes: file_size,
                events: event_count,
                parse_secs,
                compress_secs,
            })
        })
        .collect();

    let total_seconds = overall_start.elapsed().as_secs_f64();

    // Aggregate results
    let files_processed = results.len();
    let total_bytes: u64 = results.iter().map(|r| r.bytes).sum();
    let total_events: usize = results.iter().map(|r| r.events).sum();
    let parse_seconds: f64 = results.iter().map(|r| r.parse_secs).sum();
    let compress_seconds: f64 = results.iter().map(|r| r.compress_secs).sum();

    BenchResult {
        files_processed,
        total_bytes,
        total_events,
        parse_seconds,
        compress_seconds,
        total_seconds,
        peak_rss_mb: get_rss_mb(),
        parallel: true,
        workers,
    }
}

/// Result of a Mode B (network ship) benchmark run.
pub struct ShipBenchResult {
    pub files_processed: usize,
    pub bytes_decoded: u64,
    pub bytes_compressed: u64,
    pub events_shipped: usize,
    pub total_seconds: f64,
    pub ship_concurrency: usize,
    pub ship_latency_p50_ms: f64,
    pub ship_latency_p95_ms: f64,
    pub server_queue_wait_p50_ms: Option<f64>,
    pub server_queue_wait_p95_ms: Option<f64>,
    pub server_exec_p50_ms: Option<f64>,
    pub server_exec_p95_ms: Option<f64>,
    pub failures: usize,
}

impl ShipBenchResult {
    pub fn print_summary(&self) {
        let mb_decoded = self.bytes_decoded as f64 / 1_048_576.0;
        let mb_compressed = self.bytes_compressed as f64 / 1_048_576.0;
        eprintln!("\n=== Bench Mode B (network) ===");
        eprintln!("Files:          {}", self.files_processed);
        eprintln!("Concurrency:    {}", self.ship_concurrency);
        eprintln!("Decoded bytes:  {:.2} MB", mb_decoded);
        eprintln!("Compressed:     {:.2} MB", mb_compressed);
        eprintln!("Events shipped: {}", self.events_shipped);
        eprintln!("Total:          {:.3}s", self.total_seconds);
        eprintln!(
            "Throughput:     {:.1} MB/s decoded, {:.1} MB/s on wire",
            mb_decoded / self.total_seconds.max(1e-9),
            mb_compressed / self.total_seconds.max(1e-9)
        );
        eprintln!(
            "Events/s:       {:.0}",
            self.events_shipped as f64 / self.total_seconds.max(1e-9)
        );
        eprintln!(
            "Ship latency:   p50 {:.1}ms / p95 {:.1}ms",
            self.ship_latency_p50_ms, self.ship_latency_p95_ms
        );
        match (self.server_queue_wait_p50_ms, self.server_queue_wait_p95_ms) {
            (Some(p50), Some(p95)) => {
                eprintln!("Server queue:   p50 {:.1}ms / p95 {:.1}ms", p50, p95)
            }
            _ => eprintln!("Server queue:   (no X-Ingest-* headers seen)"),
        }
        match (self.server_exec_p50_ms, self.server_exec_p95_ms) {
            (Some(p50), Some(p95)) => {
                eprintln!("Server exec:    p50 {:.1}ms / p95 {:.1}ms", p50, p95)
            }
            _ => {}
        }
        if self.failures > 0 {
            eprintln!("Failures:       {}", self.failures);
        }
    }
}

/// Run Mode B: parse + compress + actually POST to the ingest URL.
///
/// Phase 1 instrumentation: this is the speed-of-light bench. It reuses
/// the production [`ShipperClient`] so the wire format, compression, and
/// retry behavior match real shipping. Server timing headers (parsed via
/// [`crate::shipping::client::parse_server_timing`]) feed the same EWMA
/// the daemon will use in phase 2.
pub fn run_benchmark_ship(
    files: &[PathBuf],
    api_url: &str,
    token: &str,
    concurrency: usize,
    algo: CompressionAlgo,
) -> anyhow::Result<ShipBenchResult> {
    use crate::config::ShipperConfig;

    let mut config = ShipperConfig::default();
    config.api_url = api_url.to_string();
    config.api_token = Some(token.to_string());

    let client = ShipperClient::with_compression(&config, algo)?;

    // Pre-parse + compress everything sequentially first so we measure pure
    // network throughput in the timed loop. Compression is cheap relative to
    // a 50ms RTT POST and we want the bench to surface real ingest cost.
    eprintln!("Pre-compressing {} payloads...", files.len());
    let prepared: Vec<(usize, u64, Vec<u8>)> = files
        .par_iter()
        .filter_map(|path| {
            let parse_result = pipeline::parser::parse_session_file(path, 0).ok()?;
            if parse_result.events.is_empty() {
                return None;
            }
            let source_path = path.to_string_lossy();
            let compressed = pipeline::compressor::build_and_compress_with(
                &parse_result.metadata.session_id,
                &parse_result.events,
                &parse_result.metadata,
                &source_path,
                "claude",
                None,
                algo,
            )
            .ok()?;
            let event_count = parse_result.events.len();
            let decoded_bytes = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
            Some((event_count, decoded_bytes, compressed))
        })
        .collect();

    let bytes_decoded: u64 = prepared.iter().map(|(_, b, _)| *b).sum();
    let bytes_compressed: u64 = prepared.iter().map(|(_, _, c)| c.len() as u64).sum();
    let total_events: usize = prepared.iter().map(|(e, _, _)| *e).sum();
    eprintln!(
        "Prepared: {} payloads, {:.2} MB decoded, {:.2} MB compressed, {} events",
        prepared.len(),
        bytes_decoded as f64 / 1_048_576.0,
        bytes_compressed as f64 / 1_048_576.0,
        total_events
    );

    // Drive Mode B inside a tokio runtime. We use a Semaphore to cap
    // concurrent in-flight POSTs and a shared Mutex<Vec<_>> for samples.
    let runtime = tokio::runtime::Runtime::new()?;
    let result = runtime.block_on(async move {
        use tokio::sync::Semaphore;

        let sem = Arc::new(Semaphore::new(concurrency.max(1)));
        let ship_latencies: Arc<Mutex<Vec<f64>>> =
            Arc::new(Mutex::new(Vec::with_capacity(prepared.len())));
        let server_queue: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));
        let server_exec: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));
        let failures = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let events_shipped = Arc::new(std::sync::atomic::AtomicUsize::new(0));

        let overall_start = Instant::now();
        let mut handles = Vec::with_capacity(prepared.len());
        for (i, (event_count, _bytes, payload)) in prepared.into_iter().enumerate() {
            let permit = sem.clone().acquire_owned().await.unwrap();
            let client = client.clone();
            let ship_latencies = ship_latencies.clone();
            let server_queue = server_queue.clone();
            let server_exec = server_exec.clone();
            let failures = failures.clone();
            let events_shipped = events_shipped.clone();

            handles.push(tokio::spawn(async move {
                let _permit = permit;
                let started = Instant::now();
                let result = client.ship(payload).await;
                let latency_ms = started.elapsed().as_secs_f64() * 1000.0;
                ship_latencies.lock().unwrap().push(latency_ms);
                match result {
                    ShipResult::Ok { server_timing } => {
                        events_shipped.fetch_add(event_count, Ordering::Relaxed);
                        let ServerIngestTiming {
                            queue_wait_ms,
                            exec_ms,
                            ..
                        } = server_timing;
                        if let Some(v) = queue_wait_ms {
                            server_queue.lock().unwrap().push(v);
                        }
                        if let Some(v) = exec_ms {
                            server_exec.lock().unwrap().push(v);
                        }
                    }
                    other => {
                        failures.fetch_add(1, Ordering::Relaxed);
                        eprintln!("  ship #{i} failed: {other:?}");
                    }
                }
            }));
        }
        for h in handles {
            let _ = h.await;
        }
        let total_seconds = overall_start.elapsed().as_secs_f64();

        let mut ship_lat = ship_latencies.lock().unwrap().clone();
        ship_lat.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let ship_p50 = pct(&ship_lat, 0.50).unwrap_or(0.0);
        let ship_p95 = pct(&ship_lat, 0.95).unwrap_or(0.0);

        let mut sq = server_queue.lock().unwrap().clone();
        sq.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mut se = server_exec.lock().unwrap().clone();
        se.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        ShipBenchResult {
            files_processed: ship_lat.len(),
            bytes_decoded,
            bytes_compressed,
            events_shipped: events_shipped.load(Ordering::Relaxed),
            total_seconds,
            ship_concurrency: concurrency.max(1),
            ship_latency_p50_ms: ship_p50,
            ship_latency_p95_ms: ship_p95,
            server_queue_wait_p50_ms: pct(&sq, 0.50),
            server_queue_wait_p95_ms: pct(&sq, 0.95),
            server_exec_p50_ms: pct(&se, 0.50),
            server_exec_p95_ms: pct(&se, 0.95),
            failures: failures.load(Ordering::Relaxed),
        }
    });

    Ok(result)
}

fn pct(sorted: &[f64], q: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    Some(sorted[idx.min(sorted.len() - 1)])
}

/// Discover all JSONL session files under ~/.claude/projects/
pub fn discover_session_files() -> Vec<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/davidrose".to_string());
    let projects_dir = PathBuf::from(home).join(".claude").join("projects");

    let mut files = Vec::new();
    if let Ok(entries) = walk_directory(projects_dir) {
        files = entries;
    }

    // Sort by size descending (biggest first — helps rayon work-stealing)
    files.sort_by(|a, b| {
        let sa = std::fs::metadata(a).map(|m| m.len()).unwrap_or(0);
        let sb = std::fs::metadata(b).map(|m| m.len()).unwrap_or(0);
        sb.cmp(&sa)
    });

    files
}

fn walk_directory(dir: PathBuf) -> std::io::Result<Vec<PathBuf>> {
    let mut results = Vec::new();
    for entry in walkdir::WalkDir::new(dir)
        .follow_links(false)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let path = entry.path();
        if path.extension().map_or(false, |ext| ext == "jsonl") {
            if let Ok(meta) = path.metadata() {
                if meta.len() > 0 {
                    results.push(path.to_path_buf());
                }
            }
        }
    }
    Ok(results)
}

fn get_rss_mb() -> f64 {
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        let pid = std::process::id();
        if let Ok(output) = Command::new("ps")
            .args(["-o", "rss=", "-p", &pid.to_string()])
            .output()
        {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(kb) = s.trim().parse::<f64>() {
                    return kb / 1024.0;
                }
            }
        }
        0.0
    }
    #[cfg(not(target_os = "macos"))]
    {
        0.0
    }
}
