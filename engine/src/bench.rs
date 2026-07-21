//! Multi-file benchmark harness for comparing against Python profiling baselines.

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use rayon::prelude::*;

use crate::pipeline;
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipping::client::ShipperClient;

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

pub struct SyntheticBenchFiles {
    #[allow(dead_code)]
    tempdir: SyntheticTempDir,
    pub files: Vec<PathBuf>,
}

struct SyntheticTempDir {
    path: PathBuf,
}

impl SyntheticTempDir {
    fn create() -> anyhow::Result<Self> {
        let dir = std::env::temp_dir().join(format!(
            "longhouse-bench-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::create_dir_all(&dir)?;
        Ok(Self { path: dir })
    }
}

impl Drop for SyntheticTempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

pub fn generate_synthetic_claude_files(
    file_count: usize,
    events_per_file: usize,
    bytes_per_event: usize,
) -> anyhow::Result<SyntheticBenchFiles> {
    let tempdir = SyntheticTempDir::create()?;
    let dir = tempdir.path.clone();
    let normalized_file_count = file_count.max(1);
    let normalized_events_per_file = events_per_file.max(1);
    let normalized_bytes_per_event = bytes_per_event.max(1);
    let mut files = Vec::with_capacity(normalized_file_count);

    for file_index in 0..normalized_file_count {
        let session_id = uuid::Uuid::new_v4().to_string();
        let path = dir.join(format!("{session_id}.jsonl"));
        let mut file = std::fs::File::create(&path)?;
        for event_index in 0..normalized_events_per_file {
            let role = if event_index % 2 == 0 {
                "user"
            } else {
                "assistant"
            };
            let content = synthetic_content(file_index, event_index, normalized_bytes_per_event);
            let message_content = if role == "assistant" {
                serde_json::json!([{"type": "text", "text": content}])
            } else {
                serde_json::json!(content)
            };
            let line = serde_json::json!({
                "type": role,
                "uuid": uuid::Uuid::new_v4().to_string(),
                "timestamp": "2026-06-03T00:00:00Z",
                "cwd": "/tmp/longhouse-bench",
                "gitBranch": "main",
                "message": {
                    "content": message_content,
                },
            });
            writeln!(file, "{}", line)?;
        }
        files.push(path);
    }

    Ok(SyntheticBenchFiles { tempdir, files })
}

fn synthetic_content(file_index: usize, event_index: usize, bytes_per_event: usize) -> String {
    let prefix = format!("synthetic bench file={file_index} event={event_index} ");
    if prefix.len() >= bytes_per_event {
        return prefix;
    }
    let mut content = String::with_capacity(bytes_per_event);
    content.push_str(&prefix);
    while content.len() < bytes_per_event {
        content.push_str("payload ");
    }
    content.truncate(bytes_per_event);
    content
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
    pub repair_envelopes_expected: usize,
    pub repair_receipts: usize,
    pub bytes_decoded: u64,
    pub bytes_wire: u64,
    pub events_shipped: usize,
    pub total_seconds: f64,
    pub ship_concurrency: usize,
    pub ship_latency_p50_ms: f64,
    pub ship_latency_p95_ms: f64,
    pub server_queue_wait_p50_ms: Option<f64>,
    pub server_queue_wait_p95_ms: Option<f64>,
    pub server_exec_p50_ms: Option<f64>,
    pub server_exec_p95_ms: Option<f64>,
    pub mixed_live_count: usize,
    pub live_latency_p50_ms: Option<f64>,
    pub live_latency_p95_ms: Option<f64>,
    pub live_failures: usize,
    pub failures: usize,
}

impl ShipBenchResult {
    pub fn archive_succeeded(&self) -> bool {
        self.repair_envelopes_expected > 0
            && self.repair_receipts == self.repair_envelopes_expected
            && self.events_shipped > 0
            && self.failures == 0
    }

    pub fn live_sla_passes(&self, max_p95_ms: f64) -> bool {
        if self.mixed_live_count == 0 {
            return true;
        }
        if self.live_failures > 0 {
            return false;
        }
        self.live_latency_p95_ms
            .is_some_and(|p95| p95 <= max_p95_ms)
    }

    pub fn print_summary(&self, live_max_p95_ms: f64) {
        let mb_decoded = self.bytes_decoded as f64 / 1_048_576.0;
        let mb_wire = self.bytes_wire as f64 / 1_048_576.0;
        eprintln!("\n=== Bench Mode B (network) ===");
        eprintln!("Files:          {}", self.files_processed);
        eprintln!(
            "Repair receipts: {}/{}",
            self.repair_receipts, self.repair_envelopes_expected
        );
        eprintln!("Concurrency:    {}", self.ship_concurrency);
        eprintln!("Decoded bytes:  {:.2} MB", mb_decoded);
        eprintln!("Wire bytes:     {:.2} MB", mb_wire);
        eprintln!("Events shipped: {}", self.events_shipped);
        eprintln!("Total:          {:.3}s", self.total_seconds);
        eprintln!(
            "Throughput:     {:.1} MB/s decoded, {:.1} MB/s on wire",
            mb_decoded / self.total_seconds.max(1e-9),
            mb_wire / self.total_seconds.max(1e-9)
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
        if self.mixed_live_count > 0 {
            eprintln!("\n=== Mixed Live Probes ===");
            eprintln!("Live probes:    {}", self.mixed_live_count);
            match (self.live_latency_p50_ms, self.live_latency_p95_ms) {
                (Some(p50), Some(p95)) => {
                    eprintln!("Live latency:   p50 {:.1}ms / p95 {:.1}ms", p50, p95)
                }
                _ => eprintln!("Live latency:   (no successful live probes)"),
            }
            if self.live_failures > 0 {
                eprintln!("Live failures:  {}", self.live_failures);
            }
            if self
                .live_latency_p95_ms
                .is_some_and(|p95| p95 > live_max_p95_ms)
            {
                eprintln!("Live SLA:       FAIL (p95 > {:.1}ms)", live_max_p95_ms);
            } else if self.live_failures > 0 {
                eprintln!("Live SLA:       FAIL (probe failures)");
            } else {
                eprintln!("Live SLA:       PASS");
            }
        }
    }
}

/// One pre-prepared Mode B envelope ready for concurrent shipping.
struct PreparedShipEnvelope {
    lane: &'static str,
    event_count: usize,
    decoded_bytes: u64,
    body: Vec<u8>,
    expected_envelope_id: String,
}

/// Run Mode B: negotiate storage-v2, prepare durable envelopes through the
/// production shipper/state path, then POST unique repair + live envelopes
/// through [`ShipperClient::ship_storage_v2_body`] (receipt-validated).
///
/// SLA math stays on client receipt RTT. There is no legacy ingest fallback
/// when the Runtime Host requires storage-v2 cutover.
pub fn run_benchmark_ship(
    files: &[PathBuf],
    api_url: &str,
    token: &str,
    machine_id: &str,
    concurrency: usize,
    mixed_live_count: usize,
    algo: CompressionAlgo,
) -> anyhow::Result<ShipBenchResult> {
    use crate::config::ShipperConfig;
    use crate::state::db::open_db;
    use crate::storage_v2_shipper::prepare_next_envelope_body_for_lane;

    if machine_id.trim().is_empty() {
        anyhow::bail!("--ship-machine-id is required when --ship-url is set");
    }

    let mut config = ShipperConfig::default();
    config.api_url = api_url.to_string();
    config.api_token = Some(token.to_string());

    let client = ShipperClient::with_compression(&config, algo)?;
    let state_dir = SyntheticTempDir::create()?;
    let db_path = state_dir.path.join("bench-state.db");
    let mut conn = open_db(Some(&db_path))?;

    let runtime = tokio::runtime::Runtime::new()?;
    let capabilities = runtime.block_on(async {
        client
            .storage_v2_capabilities(machine_id, Some(std::time::Duration::from_secs(10)))
            .await
    })?
    .ok_or_else(|| {
        anyhow::anyhow!(
            "Runtime Host does not advertise storage-v2 capabilities; Mode B requires storage-v2 (no legacy ingest fallback)"
        )
    })?;
    if !capabilities.cutover {
        anyhow::bail!(
            "Runtime Host has not cut over to storage-v2; Mode B refuses the legacy ingest path"
        );
    }

    eprintln!(
        "Negotiated storage-v2 (cutover={}); preparing durable envelopes...",
        capabilities.cutover
    );

    let mut prepared: Vec<PreparedShipEnvelope> = Vec::with_capacity(files.len());
    for path in files {
        let Some((body, envelope)) = prepare_next_envelope_body_for_lane(
            &mut conn,
            &capabilities,
            path,
            "claude",
            "repair",
        )?
        else {
            anyhow::bail!(
                "failed to prepare repair-lane benchmark envelope for {}",
                path.display()
            );
        };
        let decoded_bytes = std::fs::metadata(path)
            .map(|m| m.len())
            .unwrap_or(envelope.raw_bytes);
        prepared.push(PreparedShipEnvelope {
            lane: "repair",
            event_count: envelope.event_count,
            decoded_bytes,
            body,
            expected_envelope_id: envelope.envelope.expected_envelope_id,
        });
    }

    let live_holder = if mixed_live_count > 0 {
        Some(generate_synthetic_claude_files(mixed_live_count, 1, 256)?)
    } else {
        None
    };
    let mut live_prepared: Vec<PreparedShipEnvelope> = Vec::with_capacity(mixed_live_count);
    if let Some(live_files) = live_holder.as_ref() {
        for path in &live_files.files {
            let Some((body, envelope)) = prepare_next_envelope_body_for_lane(
                &mut conn,
                &capabilities,
                path,
                "claude",
                "live",
            )?
            else {
                anyhow::bail!(
                    "failed to prepare live-lane probe envelope for {}",
                    path.display()
                );
            };
            let decoded_bytes = std::fs::metadata(path)
                .map(|m| m.len())
                .unwrap_or(envelope.raw_bytes);
            live_prepared.push(PreparedShipEnvelope {
                lane: "live",
                event_count: envelope.event_count,
                decoded_bytes,
                body,
                expected_envelope_id: envelope.envelope.expected_envelope_id,
            });
        }
    }
    // Keep synthetic live sources alive until ships complete.
    let _live_holder = live_holder;
    let _state_dir = state_dir;

    let archive_count = prepared.len();
    let bytes_decoded: u64 = prepared.iter().map(|item| item.decoded_bytes).sum();
    let bytes_wire: u64 = prepared.iter().map(|item| item.body.len() as u64).sum();
    let repair_events: usize = prepared.iter().map(|item| item.event_count).sum();
    let all_source_bytes = bytes_decoded
        + live_prepared
            .iter()
            .map(|item| item.decoded_bytes)
            .sum::<u64>();
    let all_wire_bytes = bytes_wire
        + live_prepared
            .iter()
            .map(|item| item.body.len() as u64)
            .sum::<u64>();
    let total_events: usize = repair_events
        + live_prepared
            .iter()
            .map(|item| item.event_count)
            .sum::<usize>();
    eprintln!(
        "Prepared: {} repair + {} live envelopes, {:.2} MB source, {:.2} MB wire, {} events",
        archive_count,
        live_prepared.len(),
        all_source_bytes as f64 / 1_048_576.0,
        all_wire_bytes as f64 / 1_048_576.0,
        total_events
    );

    let ingest_path = capabilities.ingest_path.clone();
    let result = runtime.block_on(async move {
        use tokio::sync::Semaphore;
        use tokio::time::{sleep, Duration};

        let sem = Arc::new(Semaphore::new(concurrency.max(1)));
        let ship_latencies: Arc<Mutex<Vec<f64>>> =
            Arc::new(Mutex::new(Vec::with_capacity(archive_count)));
        let live_latencies: Arc<Mutex<Vec<f64>>> =
            Arc::new(Mutex::new(Vec::with_capacity(mixed_live_count)));
        let failures = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let live_failures = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let repair_receipts = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let events_shipped = Arc::new(std::sync::atomic::AtomicUsize::new(0));

        let overall_start = Instant::now();
        let mut handles = Vec::with_capacity(archive_count + live_prepared.len());

        for (i, item) in prepared.into_iter().enumerate() {
            let sem = sem.clone();
            let client = client.clone();
            let ingest_path = ingest_path.clone();
            let ship_latencies = ship_latencies.clone();
            let failures = failures.clone();
            let repair_receipts = repair_receipts.clone();
            let events_shipped = events_shipped.clone();

            handles.push((
                "repair",
                tokio::spawn(async move {
                    let _permit = sem.acquire_owned().await.unwrap();
                    let started = Instant::now();
                    let result = client
                        .ship_storage_v2_body(
                            &ingest_path,
                            item.lane,
                            item.body,
                            &item.expected_envelope_id,
                            None,
                        )
                        .await;
                    let latency_ms = started.elapsed().as_secs_f64() * 1000.0;
                    ship_latencies.lock().unwrap().push(latency_ms);
                    match result {
                        Ok(_) => {
                            repair_receipts.fetch_add(1, Ordering::Relaxed);
                            events_shipped.fetch_add(item.event_count, Ordering::Relaxed);
                        }
                        Err(error) => {
                            failures.fetch_add(1, Ordering::Relaxed);
                            eprintln!("  repair ship #{i} failed: {error:#}");
                        }
                    }
                }),
            ));
        }

        if live_prepared.is_empty() && mixed_live_count > 0 {
            live_failures.fetch_add(mixed_live_count, Ordering::Relaxed);
        }
        for (i, item) in live_prepared.into_iter().enumerate() {
            let client = client.clone();
            let ingest_path = ingest_path.clone();
            let live_latencies = live_latencies.clone();
            let live_failures = live_failures.clone();
            handles.push((
                "live",
                tokio::spawn(async move {
                    sleep(Duration::from_millis((i as u64).saturating_mul(100))).await;
                    let started = Instant::now();
                    let result = client
                        .ship_storage_v2_body(
                            &ingest_path,
                            item.lane,
                            item.body,
                            &item.expected_envelope_id,
                            None,
                        )
                        .await;
                    let latency_ms = started.elapsed().as_secs_f64() * 1000.0;
                    match result {
                        Ok(_) => live_latencies.lock().unwrap().push(latency_ms),
                        Err(error) => {
                            live_failures.fetch_add(1, Ordering::Relaxed);
                            eprintln!("  live probe #{i} failed: {error:#}");
                        }
                    }
                }),
            ));
        }

        for (lane, handle) in handles {
            if let Err(error) = handle.await {
                if lane == "live" {
                    live_failures.fetch_add(1, Ordering::Relaxed);
                } else {
                    failures.fetch_add(1, Ordering::Relaxed);
                }
                eprintln!("  {lane} ship task failed before completion: {error}");
            }
        }
        let total_seconds = overall_start.elapsed().as_secs_f64();

        let mut ship_lat = ship_latencies.lock().unwrap().clone();
        ship_lat.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let ship_p50 = pct(&ship_lat, 0.50).unwrap_or(0.0);
        let ship_p95 = pct(&ship_lat, 0.95).unwrap_or(0.0);
        let mut live_lat = live_latencies.lock().unwrap().clone();
        live_lat.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        ShipBenchResult {
            files_processed: archive_count,
            repair_envelopes_expected: archive_count,
            repair_receipts: repair_receipts.load(Ordering::Relaxed),
            bytes_decoded,
            bytes_wire,
            events_shipped: events_shipped.load(Ordering::Relaxed),
            total_seconds,
            ship_concurrency: concurrency.max(1),
            ship_latency_p50_ms: ship_p50,
            ship_latency_p95_ms: ship_p95,
            // storage-v2 receipts do not carry legacy X-Ingest-* timing headers.
            server_queue_wait_p50_ms: None,
            server_queue_wait_p95_ms: None,
            server_exec_p50_ms: None,
            server_exec_p95_ms: None,
            mixed_live_count,
            live_latency_p50_ms: pct(&live_lat, 0.50),
            live_latency_p95_ms: pct(&live_lat, 0.95),
            live_failures: live_failures.load(Ordering::Relaxed),
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
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/example".to_string());
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

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::{Arc, Mutex};

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use crate::shipping::storage_v2::{StorageV2Envelope, STORAGE_V2_LANE_HEADER};

    fn ship_bench_result(
        mixed_live_count: usize,
        live_latency_p95_ms: Option<f64>,
        live_failures: usize,
    ) -> ShipBenchResult {
        ShipBenchResult {
            files_processed: 0,
            repair_envelopes_expected: 0,
            repair_receipts: 0,
            bytes_decoded: 0,
            bytes_wire: 0,
            events_shipped: 0,
            total_seconds: 0.0,
            ship_concurrency: 1,
            ship_latency_p50_ms: 0.0,
            ship_latency_p95_ms: 0.0,
            server_queue_wait_p50_ms: None,
            server_queue_wait_p95_ms: None,
            server_exec_p50_ms: None,
            server_exec_p95_ms: None,
            mixed_live_count,
            live_latency_p50_ms: live_latency_p95_ms,
            live_latency_p95_ms,
            live_failures,
            failures: 0,
        }
    }

    #[test]
    fn live_sla_passes_when_no_mixed_probe_requested() {
        assert!(ship_bench_result(0, None, 0).live_sla_passes(10_000.0));
    }

    #[test]
    fn live_sla_fails_on_probe_failure_or_high_p95() {
        assert!(!ship_bench_result(5, Some(100.0), 1).live_sla_passes(10_000.0));
        assert!(!ship_bench_result(5, Some(12_000.0), 0).live_sla_passes(10_000.0));
        assert!(ship_bench_result(5, Some(500.0), 0).live_sla_passes(10_000.0));
    }

    #[test]
    fn archive_success_requires_receipted_work_without_failures() {
        let mut result = ship_bench_result(0, None, 0);
        assert!(!result.archive_succeeded());

        result.files_processed = 1;
        result.repair_envelopes_expected = 1;
        result.repair_receipts = 1;
        result.events_shipped = 2;
        assert!(result.archive_succeeded());

        result.repair_receipts = 0;
        assert!(!result.archive_succeeded());

        result.repair_receipts = 1;
        result.failures = 1;
        assert!(!result.archive_succeeded());
    }

    #[test]
    fn synthetic_claude_files_parse_with_expected_event_count() {
        let generated = generate_synthetic_claude_files(2, 3, 128).unwrap();

        assert_eq!(generated.files.len(), 2);
        for path in &generated.files {
            let parsed = pipeline::parser::parse_session_file(path, 0).unwrap();
            assert_eq!(parsed.events.len(), 3);
            assert_eq!(parsed.candidate_records, 3);
            assert!(uuid::Uuid::parse_str(&parsed.metadata.session_id).is_ok());
        }
    }

    async fn read_http_request(socket: &mut tokio::net::TcpStream) -> (String, String, Vec<u8>) {
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let read = socket.read(&mut buffer).await.unwrap();
            assert!(read > 0, "request closed before headers completed");
            bytes.extend_from_slice(&buffer[..read]);
            if let Some(offset) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                break offset + 4;
            }
        };
        let headers = String::from_utf8_lossy(&bytes[..header_end]).into_owned();
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().unwrap())
            })
            .unwrap_or(0);
        while bytes.len() - header_end < content_length {
            let read = socket.read(&mut buffer).await.unwrap();
            assert!(read > 0, "request closed before body completed");
            bytes.extend_from_slice(&buffer[..read]);
        }
        (
            headers.lines().next().unwrap_or_default().to_string(),
            headers,
            bytes[header_end..header_end + content_length].to_vec(),
        )
    }

    fn write_json_response(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
    }

    fn header_value(headers: &str, name: &str) -> Option<String> {
        headers.lines().find_map(|line| {
            let (header_name, value) = line.split_once(':')?;
            header_name
                .eq_ignore_ascii_case(name)
                .then(|| value.trim().to_string())
        })
    }

    #[tokio::test]
    async fn mode_b_negotiates_capabilities_and_ships_unique_repair_and_live_lanes() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let observed = Arc::new(Mutex::new(Vec::<(String, String, Vec<u8>)>::new()));
        let server_observed = observed.clone();
        let server = tokio::spawn(async move {
            // capabilities + 2 repair + 2 live
            for _ in 0..5 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, headers, body) = read_http_request(&mut socket).await;
                server_observed.lock().unwrap().push((
                    request_line.clone(),
                    headers.clone(),
                    body.clone(),
                ));
                if request_line.starts_with("GET /api/agents/storage/v2/capabilities") {
                    let response_body = serde_json::json!({
                        "protocol_version": 2,
                        "cutover": true,
                        "tenant_id": "tenant-bench",
                        "machine_id": "bench-machine",
                        "ingest_path": "/api/agents/storage/v2/envelopes",
                        "max_wire_body_bytes": 12 * 1024 * 1024,
                        "max_raw_record_bytes": 4 * 1024 * 1024,
                        "max_records": 10_000,
                        "media_claim_path": "/api/agents/storage/v2/media/claims",
                        "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
                        "max_media_bytes": 32 * 1024 * 1024,
                        "max_media_claims": 512,
                        "range_kinds": ["byte_offset", "record_ordinal"],
                        "lanes": ["live", "repair"],
                        "lane_header": STORAGE_V2_LANE_HEADER,
                    })
                    .to_string();
                    socket
                        .write_all(write_json_response(&response_body).as_bytes())
                        .await
                        .unwrap();
                    continue;
                }
                assert!(
                    request_line.starts_with("POST /api/agents/storage/v2/envelopes"),
                    "unexpected request: {request_line}"
                );
                assert!(
                    !request_line.contains("/api/agents/ingest"),
                    "Mode B must not fall back to legacy ingest"
                );
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                let response_body = serde_json::json!({
                    "v": 2,
                    "envelope_id": envelope.expected_envelope_id,
                    "object_hash": "b".repeat(64),
                    "commit_seq": "1",
                    "raw_state": "durable",
                    "render_state": "ready",
                    "media_state": "complete",
                    "missing_media_hashes": [],
                })
                .to_string();
                socket
                    .write_all(write_json_response(&response_body).as_bytes())
                    .await
                    .unwrap();
            }
        });

        let generated = generate_synthetic_claude_files(2, 3, 128).unwrap();
        let result = tokio::task::spawn_blocking(move || {
            run_benchmark_ship(
                &generated.files,
                &format!("http://{address}"),
                "bench-token",
                "bench-machine",
                2,
                2,
                CompressionAlgo::Gzip,
            )
        })
        .await
        .unwrap()
        .unwrap();

        server.await.unwrap();
        assert_eq!(result.failures, 0);
        assert_eq!(result.live_failures, 0);
        assert_eq!(result.mixed_live_count, 2);
        assert!(result.live_sla_passes(10_000.0));
        assert!(result.events_shipped >= 2);

        let requests = observed.lock().unwrap().clone();
        assert_eq!(requests.len(), 5);
        assert!(requests[0]
            .0
            .starts_with("GET /api/agents/storage/v2/capabilities"));
        assert!(
            header_value(&requests[0].1, "X-Longhouse-Machine-Id").as_deref()
                == Some("bench-machine"),
            "capabilities must negotiate with an explicit machine id"
        );

        let mut repair_ids = Vec::new();
        let mut live_ids = Vec::new();
        for (request_line, headers, body) in requests.into_iter().skip(1) {
            assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes"));
            let lane = header_value(&headers, STORAGE_V2_LANE_HEADER)
                .expect("storage-v2 lane header required");
            let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
            match lane.as_str() {
                "repair" => repair_ids.push(envelope.expected_envelope_id),
                "live" => live_ids.push(envelope.expected_envelope_id),
                other => panic!("unexpected lane {other}"),
            }
        }
        assert_eq!(repair_ids.len(), 2);
        assert_eq!(live_ids.len(), 2);
        assert_eq!(
            repair_ids.len(),
            repair_ids
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
        );
        assert_eq!(
            live_ids.len(),
            live_ids
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
        );
        for repair_id in &repair_ids {
            assert!(!live_ids.contains(repair_id));
        }
    }

    #[tokio::test]
    async fn mode_b_counts_receipt_failures_without_legacy_fallback() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let (request_line, _, _) = read_http_request(&mut socket).await;
            assert!(request_line.starts_with("GET /api/agents/storage/v2/capabilities"));
            let response_body = serde_json::json!({
                "protocol_version": 2,
                "cutover": true,
                "tenant_id": "tenant-bench",
                "machine_id": "bench-machine",
                "ingest_path": "/api/agents/storage/v2/envelopes",
                "max_wire_body_bytes": 12 * 1024 * 1024,
                "max_raw_record_bytes": 4 * 1024 * 1024,
                "max_records": 10_000,
                "media_claim_path": "/api/agents/storage/v2/media/claims",
                "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
                "max_media_bytes": 32 * 1024 * 1024,
                "max_media_claims": 512,
                "range_kinds": ["byte_offset", "record_ordinal"],
                "lanes": ["live", "repair"],
                "lane_header": STORAGE_V2_LANE_HEADER,
            })
            .to_string();
            socket
                .write_all(write_json_response(&response_body).as_bytes())
                .await
                .unwrap();

            let (mut socket, _) = listener.accept().await.unwrap();
            let (request_line, headers, _) = read_http_request(&mut socket).await;
            assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes"));
            assert_eq!(
                header_value(&headers, STORAGE_V2_LANE_HEADER).as_deref(),
                Some("repair")
            );
            let response = "HTTP/1.1 426 Upgrade Required\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}";
            socket.write_all(response.as_bytes()).await.unwrap();
        });

        let generated = generate_synthetic_claude_files(1, 2, 64).unwrap();
        let result = tokio::task::spawn_blocking(move || {
            run_benchmark_ship(
                &generated.files,
                &format!("http://{address}"),
                "bench-token",
                "bench-machine",
                1,
                0,
                CompressionAlgo::Gzip,
            )
        })
        .await
        .unwrap()
        .unwrap();

        server.await.unwrap();
        assert_eq!(result.failures, 1);
        assert_eq!(result.events_shipped, 0);
    }
}
