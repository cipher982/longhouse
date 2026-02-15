//! Multi-file benchmark harness for comparing against Python profiling baselines.

use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::pipeline;

pub struct BenchResult {
    pub files_processed: usize,
    pub total_bytes: u64,
    pub total_events: usize,
    pub parse_seconds: f64,
    pub compress_seconds: f64,
    pub total_seconds: f64,
    pub peak_rss_mb: f64,
}

impl BenchResult {
    pub fn print_summary(&self) {
        let mb = self.total_bytes as f64 / 1_048_576.0;
        eprintln!("\n=== Benchmark Results ===");
        eprintln!("Files:      {}", self.files_processed);
        eprintln!("Bytes:      {:.2} MB", mb);
        eprintln!("Events:     {}", self.total_events);
        eprintln!("Parse:      {:.3}s ({:.1}%)", self.parse_seconds, self.parse_seconds / self.total_seconds * 100.0);
        eprintln!("Compress:   {:.3}s ({:.1}%)", self.compress_seconds, self.compress_seconds / self.total_seconds * 100.0);
        eprintln!("Total:      {:.3}s", self.total_seconds);
        eprintln!("Throughput: {:.1} MB/s", mb / self.total_seconds);
        eprintln!("Events/s:   {:.0}", self.total_events as f64 / self.total_seconds);
        eprintln!("Peak RSS:   {:.1} MB", self.peak_rss_mb);
    }
}

/// Run benchmark on a list of files with parse + compress.
pub fn run_benchmark(files: &[PathBuf], compress: bool) -> BenchResult {
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
            let _ = pipeline::compressor::build_and_compress(
                &result.metadata.session_id,
                &result.events,
                &result.metadata,
                &source_path,
                "claude",
            );
            compress_time += compress_start.elapsed().as_secs_f64();
        }

        total_bytes += file_size;
        total_events += result.events.len();
        files_ok += 1;

        // Progress every 500 files
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

    // Get RSS (macOS-specific)
    let peak_rss_mb = get_rss_mb();

    BenchResult {
        files_processed: files_ok,
        total_bytes,
        total_events,
        parse_seconds: parse_time,
        compress_seconds: compress_time,
        total_seconds,
        peak_rss_mb,
    }
}

/// Discover all JSONL session files under ~/.claude/projects/
pub fn discover_session_files() -> Vec<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/davidrose".to_string());
    let projects_dir = PathBuf::from(home).join(".claude").join("projects");

    let mut files = Vec::new();
    if let Ok(entries) = walkdir(projects_dir) {
        files = entries;
    }

    // Sort by size descending (biggest first, like profiling script)
    files.sort_by(|a, b| {
        let sa = std::fs::metadata(a).map(|m| m.len()).unwrap_or(0);
        let sb = std::fs::metadata(b).map(|m| m.len()).unwrap_or(0);
        sb.cmp(&sa)
    });

    files
}

fn walkdir(dir: PathBuf) -> std::io::Result<Vec<PathBuf>> {
    let mut results = Vec::new();
    for entry in walkdir::WalkDir::new(dir).follow_links(false).into_iter().filter_map(|e| e.ok()) {
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
    // macOS: use mach APIs via libc
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        let pid = std::process::id();
        if let Ok(output) = Command::new("ps").args(["-o", "rss=", "-p", &pid.to_string()]).output() {
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
