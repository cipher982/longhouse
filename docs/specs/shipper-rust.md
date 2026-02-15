# Shipper Rust Rewrite: Architecture & Implementation Spec

**Status:** Draft
**Author:** David + Claude
**Date:** 2026-02-15
**Prereq:** Shipper v2 (Python) — completed, 217 tests passing

## Motivation

The shipper runs 24/7 on user machines. After Python v2 optimizations (orjson, single-pass metadata), profiling shows:

| Metric | Python v2 (current) |
|--------|-------------------|
| Full backfill (9.6GB, 8621 files) | 206s |
| Throughput | 48 MB/s, 3441 events/s |
| RSS peak | 3.4 GB |
| CPU utilization | 95.5% |
| Phase: gzip_compress | **78.9%** of wall |
| Phase: parse | **18.1%** of wall |
| Phase: json_serialize | 1.7% of wall |

**Gzip is 79% of wall time.** Python materializes the entire JSON payload as a byte string, then compresses it in one call. This is the #1 bottleneck and the primary Rust target.

### Why Rust, not just better Python

1. **GIL blocks parallel file processing.** 8621 files processed serially. Rust's `rayon` trivially parallelizes across files.
2. **Python object churn kills throughput at scale.** 37 MB/s at full dataset vs 121 MB/s for a single large file — GC pressure from 3.4GB of Python objects.
3. **No zero-copy path in Python.** Every JSONL line becomes a Python dict, then a new dict (payload), then a JSON string, then gzip bytes. Four copies minimum.
4. **Streaming compression is impossible in Python.** `gzip.compress()` requires the full input upfront. Rust can stream `serde → GzEncoder → HTTP body` with zero intermediate allocation.
5. **Binary distribution.** A 5MB static binary vs 200MB Python venv + all backend dependencies.

### Expected Gains (Conservative)

| Metric | Python v2 | Rust (estimated) | Speedup |
|--------|-----------|-------------------|---------|
| Full 9.6GB backfill | 206s | 10-30s | 7-20x |
| Throughput (MB/s) | 48 | 400-800 | 8-16x |
| Events/sec | 3,441 | 30,000-80,000 | 9-23x |
| RSS peak | 3.4 GB | 50-200 MB | 17-68x |
| Single 993MB file | 3.48s | 0.3-0.8s | 4-12x |
| Binary size | ~200MB (venv) | 5-8 MB | 25-40x |
| Startup time | ~2s (Python imports) | <50ms | 40x+ |

---

## Architecture

### Core Design: Streaming Pipeline

The fundamental architectural change is moving from "load all, then process" to a streaming pipeline with bounded memory:

```
file reader (mmap or buffered read)
  → line splitter (zero-copy, yields &[u8] slices)
    → serde_json parser (deserialize to typed struct)
      → event extractor (yield ParsedEvent per content item)
        → payload builder (serde Serialize, streaming)
          → GzEncoder writer (flate2, streaming compression)
            → reqwest streaming body upload
```

**Key property:** At no point is the full payload materialized in memory. The `serde_json::to_writer()` call writes directly into a `GzEncoder<Vec<u8>>` (or `GzEncoder<os_pipe::PipeWriter>` for truly streaming HTTP).

### Memory Model

- **Per-file buffer:** 64 KB read buffer (not file-size-proportional)
- **Compression buffer:** 128 KB output buffer per active compression stream
- **Event accumulator:** Bounded to `max_batch_bytes` (5 MB default) of source data per batch
- **Total RSS target:** <200 MB regardless of input size
- **No `Vec<ParsedEvent>` accumulation** for the entire file. Events are processed in batches, flushed, and dropped.

### Concurrency Model

```
Main thread
  ├── File discovery (walk provider dirs)
  ├── File processing (rayon thread pool)
  │     ├── worker 0: parse → build → compress → queue
  │     ├── worker 1: parse → build → compress → queue
  │     └── worker N: ...
  └── HTTP shipper (tokio async, connection pool)
        ├── upload compressed batches
        └── handle retries, rate limits, spool
```

**Why hybrid (rayon + tokio)?** File parsing and gzip compression are CPU-bound (95.5% CPU utilization in profiling). These belong on OS threads, not async tasks. HTTP upload is I/O-bound and benefits from async connection pooling with `reqwest`. The boundary is a bounded channel: `rayon` workers produce compressed payloads, `tokio` runtime consumes and uploads them.

Alternative considered: pure `tokio::spawn_blocking`. Rejected because `rayon`'s work-stealing is purpose-built for CPU parallelism and handles load balancing across files of vastly different sizes (80 KB median vs 993 MB max).

---

## Crate Stack

| Crate | Version | Purpose | Why this one |
|-------|---------|---------|-------------|
| `tokio` | 1.49+ | Async runtime for HTTP | Standard, no alternative worth considering |
| `reqwest` | 0.13+ | HTTP client | Connection pooling, streaming body, rustls (no OpenSSL) |
| `serde` + `serde_json` | 1.0 | JSON parsing + serialization | Standard. `to_writer()` enables streaming serialize. |
| `flate2` | 1.1+ | Gzip compression | C-backed (miniz_oxide or zlib-ng), streaming `GzEncoder<W>` |
| `rusqlite` | 0.38+ | SQLite state + spool | Same schema as Python v2 (forward-compatible) |
| `tokio-rusqlite` | 0.7+ | Async SQLite wrapper | Moves SQLite ops off the async runtime |
| `rayon` | 1.10+ | Parallel file processing | Work-stealing for heterogeneous file sizes |
| `notify` | 8.0+ | Filesystem watching | Native FSEvents (macOS) / inotify (Linux) |
| `memmap2` | 0.9+ | Memory-mapped file I/O | Zero-copy reads for large files |
| `crossbeam-channel` | 0.5+ | Bounded channel (rayon → tokio) | Lock-free, bounded, backpressure |
| `clap` | 4.5+ | CLI argument parsing | Standard |
| `tracing` + `tracing-subscriber` | 0.1/0.3 | Structured logging | Standard, spans for per-file timing |
| `signal-hook` | 0.3+ | Graceful shutdown | SIGTERM/SIGINT for daemon mode |

### Crates Considered and Rejected

| Crate | Reason for rejection |
|-------|---------------------|
| `simd-json` | Requires mutable `&mut [u8]` buffer — incompatible with read-only mmap. `serde_json` is fast enough (parse is only 18% of wall). |
| `serde_json_borrow` | Zero-alloc borrowed parsing (1.8x speedup), but requires `#[serde(borrow)]` annotations throughout. Consider for v2 optimization if parse becomes bottleneck after gzip is solved. |
| `zstd` | Better compression ratio, but server expects gzip. Would need server-side changes. Future optimization. |
| `ureq` | Sync HTTP, simpler. Rejected because we need connection pooling and streaming upload for backfill throughput. |
| `async-compression` | Async gzip wrapper. Rejected — compression is CPU-bound, should run on rayon threads, not in async context. Use `flate2` directly. |

---

## Module Structure

```
longhouse-engine/
├── Cargo.toml
├── src/
│   ├── main.rs              # CLI entry, clap, signal handling
│   ├── config.rs             # Config struct (mirrors Python ShipperConfig)
│   ├── pipeline/
│   │   ├── mod.rs
│   │   ├── discovery.rs      # Provider-based file discovery
│   │   ├── parser.rs         # JSONL/JSON line parser → ParsedEvent
│   │   ├── payload.rs        # Event → ingest payload builder (serde Serialize)
│   │   └── compressor.rs     # serde::to_writer → GzEncoder → Vec<u8>
│   ├── providers/
│   │   ├── mod.rs            # Provider trait + registry
│   │   ├── claude.rs         # Claude Code: ~/.claude/projects/**/*.jsonl
│   │   ├── codex.rs          # Codex: ~/.codex/sessions/**/*.jsonl
│   │   └── gemini.rs         # Gemini: ~/.gemini/sessions/**/*.json
│   ├── shipping/
│   │   ├── mod.rs
│   │   ├── client.rs         # reqwest HTTP client, gzip upload, 429 retry
│   │   └── worker.rs         # rayon → channel → tokio upload orchestration
│   ├── state/
│   │   ├── mod.rs
│   │   ├── db.rs             # SQLite connection (WAL, shared)
│   │   ├── file_state.rs     # file_state table (dual offsets)
│   │   └── spool.rs          # spool_queue table (pointer-based retry)
│   ├── watch/
│   │   ├── mod.rs
│   │   └── watcher.rs        # notify-based filesystem watcher + polling fallback
│   └── service/
│       ├── mod.rs
│       └── install.rs        # launchd/systemd plist/unit generation
```

---

## Data Model

### ParsedEvent (Rust)

```rust
#[derive(Debug, Clone, Serialize)]
pub struct ParsedEvent {
    pub uuid: String,
    pub session_id: String,
    pub timestamp: DateTime<Utc>,
    pub role: Role,                    // User | Assistant | Tool
    pub content_text: Option<String>,
    pub tool_name: Option<String>,
    pub tool_input_json: Option<serde_json::Value>,
    pub tool_output_text: Option<String>,
    pub source_offset: u64,
    pub raw_type: String,
    pub raw_line: Option<String>,      // Only first event per source line
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
}
```

### IngestPayload (Rust)

```rust
#[derive(Serialize)]
pub struct IngestPayload<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub environment: &'a str,
    pub project: Option<&'a str>,
    pub device_id: &'a str,
    pub cwd: Option<&'a str>,
    pub git_repo: Option<&'a str>,
    pub git_branch: Option<&'a str>,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub provider_session_id: &'a str,
    pub events: Vec<EventIngest<'a>>,
}

#[derive(Serialize)]
pub struct EventIngest<'a> {
    pub role: &'a str,
    pub content_text: Option<&'a str>,
    pub tool_name: Option<&'a str>,
    pub tool_input_json: Option<&'a serde_json::Value>,
    pub tool_output_text: Option<&'a str>,
    pub timestamp: String,
    pub source_path: &'a str,
    pub source_offset: u64,
    pub raw_json: Option<&'a str>,
}
```

Note: liberal use of `&'a str` borrows. The payload is serialized while the source `ParsedEvent` values are still alive, so no cloning needed.

### SQLite Schema (Identical to Python v2)

```sql
-- Same DB: ~/.claude/longhouse-shipper.db
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    queued_offset INTEGER NOT NULL DEFAULT 0,
    acked_offset INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    provider_session_id TEXT,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spool_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    last_error TEXT,
    status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_spool_status ON spool_queue(status, next_retry_at);
```

Forward-compatible: the Rust binary reads/writes the same DB the Python daemon uses. Users can switch between `longhouse connect` (Python) and `longhouse-engine connect` (Rust) without migration.

---

## Streaming Compression: The 79% Fix

This is the single highest-impact change. Here's the exact pattern:

### Python (current) — 3 allocations, 79% of wall time

```python
# Step 1: Build payload dict (in memory)
payload = {"events": [...], ...}

# Step 2: Serialize to JSON bytes (full materialization)
json_bytes = orjson.dumps(payload)  # Allocation #1: full JSON string

# Step 3: Compress all at once
compressed = gzip.compress(json_bytes)  # Allocation #2: full compressed output
# json_bytes is now garbage but still in memory until GC

# Step 4: Upload
await client.post(url, content=compressed)  # Allocation #3: HTTP body copy
```

### Rust (target) — 1 allocation, streaming

```rust
// Events are in a Vec<ParsedEvent> (bounded by max_batch_bytes)
let payload = IngestPayload {
    events: events.iter().map(|e| e.to_event_ingest(source_path)).collect(),
    ..metadata_fields
};

// Serialize directly into gzip compressor — no intermediate JSON string
let mut gz = GzEncoder::new(Vec::with_capacity(64 * 1024), Compression::default());
serde_json::to_writer(&mut gz, &payload)?;  // Streams JSON tokens → gzip
let compressed = gz.finish()?;  // Single Vec<u8> output

// Upload compressed bytes
let response = client
    .post(url)
    .header("Content-Encoding", "gzip")
    .header("Content-Type", "application/json")
    .body(compressed)
    .send()
    .await?;
```

**Why this is faster:**
1. `serde_json::to_writer` never materializes the full JSON string. It writes JSON tokens directly into the `GzEncoder`'s write buffer.
2. `GzEncoder` compresses in 32 KB chunks as data arrives — it never holds the full uncompressed input.
3. Only one allocation: the final compressed `Vec<u8>`, which is typically 5-20x smaller than the JSON representation.
4. For a 10 MB payload: Python allocates ~10 MB (JSON) + ~2 MB (gzip) = 12 MB. Rust allocates ~2 MB (gzip output only).

### Truly Streaming HTTP (Future Optimization)

For very large batches, we can avoid even holding the compressed output in memory:

```rust
use tokio::io::DuplexStream;

let (writer, reader) = tokio::io::duplex(64 * 1024);

// Spawn compression on rayon
rayon::spawn(move || {
    let mut gz = GzEncoder::new(writer, Compression::default());
    serde_json::to_writer(&mut gz, &payload).unwrap();
    gz.finish().unwrap();
});

// Stream to HTTP as it compresses
let body = reqwest::Body::wrap_stream(tokio_util::io::ReaderStream::new(reader));
client.post(url).body(body).send().await?;
```

This streams compress → upload with only 64 KB buffered at any time. Not needed for v1 (5 MB batch limit means compressed output is ~500 KB–1 MB) but documents the path for unbounded streaming.

---

## Parsing: The 18% Target

### Line Splitting (Zero-Copy)

```rust
use memmap2::Mmap;

let file = File::open(path)?;
let mmap = unsafe { Mmap::map(&file)? };
let data = &mmap[offset..];

// Split on newlines without copying
for line in data.split(|&b| b == b'\n') {
    if line.is_empty() { continue; }
    let obj: serde_json::Value = serde_json::from_slice(line)?;
    // Process...
}
```

**mmap trade-off:** Excellent for large files (993 MB), but OS may over-page for small files (80 KB median). Strategy: mmap files > 1 MB, `BufReader` for smaller files.

```rust
fn read_lines(path: &Path, offset: u64) -> Box<dyn Iterator<Item = (u64, &[u8])>> {
    let size = path.metadata()?.len();
    if size > 1_048_576 {
        // mmap for large files
        Box::new(MmapLineIter::new(path, offset)?)
    } else {
        // buffered read for small files
        Box::new(BufReaderLineIter::new(path, offset)?)
    }
}
```

### Typed Parsing (Avoid `serde_json::Value`)

Instead of parsing every field, use `#[serde(deny_unknown_fields)]` with a minimal struct:

```rust
#[derive(Deserialize)]
struct RawLine {
    r#type: Option<String>,
    timestamp: Option<String>,
    uuid: Option<String>,
    cwd: Option<String>,
    #[serde(rename = "gitBranch")]
    git_branch: Option<String>,
    version: Option<String>,
    message: Option<RawMessage>,
}

#[derive(Deserialize)]
struct RawMessage {
    content: serde_json::Value,  // Content can be string or array — keep as Value
}
```

Using typed deserialization where possible and `Value` only for the polymorphic `content` field gives most of the performance of full typing without the complexity of modeling every content variant.

---

## File Discovery & Provider Abstraction

```rust
pub trait SessionProvider: Send + Sync {
    fn name(&self) -> &str;
    fn discover_files(&self) -> Vec<PathBuf>;
    fn parse_file(&self, path: &Path, offset: u64) -> Result<Vec<ParsedEvent>>;
    fn extract_metadata(&self, path: &Path) -> Result<SessionMetadata>;
}
```

Provider implementations:

| Provider | Discovery Path | Format |
|----------|---------------|--------|
| Claude | `~/.claude/projects/**/*.jsonl` | JSONL, appendable |
| Codex | `~/.codex/sessions/**/*.jsonl` | JSONL, appendable |
| Gemini | `~/.gemini/sessions/**/*.json` | JSON (full replace) |

The Claude provider handles the vast majority of data (9.6 GB in profiling). Codex and Gemini are small by comparison.

---

## Orchestration Flow

### One-Shot Backfill (`longhouse-engine ship`)

```
1. Open SQLite DB (WAL mode)
2. Startup recovery: re-enqueue gaps (queued > acked)
3. Discover files across all providers
4. Filter to files with new content (compare offsets)
5. Sort by size descending (process biggest files first for better rayon utilization)
6. Process files in parallel (rayon, N = num_cpus):
   a. Read file from offset
   b. Parse JSONL lines → events + metadata (single pass)
   c. Batch events by max_batch_bytes (5 MB)
   d. For each batch:
      - Build IngestPayload (borrows from events)
      - serde_json::to_writer → GzEncoder → Vec<u8>
      - Send compressed payload via channel → upload worker
7. Upload worker (tokio):
   a. Receive compressed payloads from channel
   b. POST to API with connection pool
   c. On success: update file_state (both offsets)
   d. On failure: enqueue pointer to spool, update queued_offset only
   e. Handle 429 with exponential backoff + Retry-After
8. Replay spool (after live shipping completes)
9. Report results
```

### Watch Mode (`longhouse-engine connect`)

```
1. Steps 1-4 from above (initial catch-up)
2. Start filesystem watcher (notify)
3. On file change event:
   a. Debounce (100ms) to batch rapid writes
   b. Check if file has new content vs stored offset
   c. Process new content through pipeline
4. Polling fallback: every 30s, re-scan for files notify might have missed
5. Spool replay: every 30s, attempt to replay pending spool entries
6. Graceful shutdown on SIGTERM/SIGINT:
   a. Stop accepting new file events
   b. Flush in-progress batches
   c. Close SQLite connection
```

---

## Error Handling & Resilience

### Error Categories

| Error | Action | Spool? |
|-------|--------|--------|
| File read error | Skip file, log warning | No |
| JSON parse error (single line) | Skip line, continue file | No |
| JSON parse error (file corrupt) | Skip file, log error | No |
| HTTP ConnectError / Timeout | Spool pointer, advance queued_offset | Yes |
| HTTP 429 | Retry with backoff (3 attempts), then spool | Yes |
| HTTP 401/403 | Hard fail, don't spool (auth broken) | No |
| HTTP 5xx | Spool pointer for retry | Yes |
| HTTP 4xx (other) | Skip (bad payload), advance offset | No |
| File truncated (size < stored offset) | Reset offsets, re-process from 0 | No |
| File deleted (spool replay) | Mark spool entry dead | Dead |

### Spool Lifecycle

Same as Python v2:
- Max 10,000 rows (backpressure)
- Exponential backoff: `min(base * 2^retry_count, 3600s)`
- Dead after 50 retries or 7 days
- Cleanup removes dead entries > 7 days old

### Crash Safety

- SQLite WAL mode ensures atomic writes
- Offsets advanced only after confirmed success
- On crash: restart reads SQLite state, re-enqueues any queued > acked gaps
- No in-memory state that isn't also in SQLite

---

## CLI Interface

```
longhouse-engine 0.1.0
Longhouse session shipper (Rust engine)

USAGE:
    longhouse-engine <COMMAND>

COMMANDS:
    ship        One-shot: scan and ship all new events
    connect     Watch mode: continuous shipping daemon
    status      Show state summary (files tracked, spool size, offsets)
    reset       Reset state for a file or all files
    version     Print version

OPTIONS:
    --url <URL>            API URL (default: from ~/.claude/longhouse-url)
    --token <TOKEN>        API token (default: from ~/.claude/longhouse-token)
    --db <PATH>            SQLite DB path (default: ~/.claude/longhouse-shipper.db)
    --workers <N>          Parallel workers (default: num_cpus)
    --batch-bytes <BYTES>  Max source bytes per batch (default: 5242880)
    --log-level <LEVEL>    Log level (default: info)
    --json                 JSON output (for machine consumption)
```

### Integration with Python CLI

The Python `longhouse` CLI remains the user-facing tool. It delegates to the Rust engine when available:

```python
# In connect.py
def _find_engine() -> Path | None:
    """Find longhouse-engine binary."""
    # Check PATH
    engine = shutil.which("longhouse-engine")
    if engine:
        return Path(engine)
    # Check alongside longhouse binary
    self_dir = Path(__file__).resolve().parent
    engine = self_dir / "longhouse-engine"
    if engine.exists():
        return engine
    return None

async def connect(url, token, use_python=False):
    engine = _find_engine()
    if engine and not use_python:
        # Delegate to Rust engine
        subprocess.run([str(engine), "connect", "--url", url, "--token", token])
    else:
        # Fall back to Python daemon
        await _python_connect_loop(url, token)
```

---

## Binary Distribution

### Build Targets

| Target | OS | Arch | Notes |
|--------|----|------|-------|
| `aarch64-apple-darwin` | macOS | Apple Silicon | Primary (David's machine) |
| `x86_64-apple-darwin` | macOS | Intel | Legacy Macs |
| `x86_64-unknown-linux-gnu` | Linux | x86_64 | Servers, CI |
| `aarch64-unknown-linux-gnu` | Linux | ARM64 | RPi, ARM servers |

### Release Workflow

```yaml
# .github/workflows/engine-release.yml
on:
  push:
    tags: ['engine-v*']

jobs:
  build:
    strategy:
      matrix:
        include:
          - target: aarch64-apple-darwin
            os: macos-latest
          - target: x86_64-apple-darwin
            os: macos-latest
          - target: x86_64-unknown-linux-gnu
            os: ubuntu-latest
          - target: aarch64-unknown-linux-gnu
            os: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with:
          targets: ${{ matrix.target }}
      - run: cargo build --release --target ${{ matrix.target }}
      - uses: softprops/action-gh-release@v2
        with:
          files: target/${{ matrix.target }}/release/longhouse-engine
```

### Install Path

```bash
# Auto-install for current platform
longhouse engine install

# Downloads to ~/.local/bin/longhouse-engine (or ~/.claude/bin/)
# Verifies SHA256 checksum
# Makes executable
```

---

## Migration Plan

### Phase 1: Core Engine (ship command)

Build the one-shot `ship` subcommand first. This is testable independently:

```bash
# Python baseline
time longhouse ship --dry-run  # 206s for full 9.6GB

# Rust engine
time longhouse-engine ship --dry-run  # Target: <30s
```

**Scope:** File discovery, JSONL parsing, payload building, streaming gzip, HTTP shipping, SQLite state. No filesystem watching yet.

**Validation:**
- Ship the same 9.6GB dataset with both Python and Rust
- Compare: event counts must match exactly
- Compare: server-side session/event IDs must match
- Profile Rust version at all 3 scale levels (L1/L2/L3)

### Phase 2: Watch Mode (connect command)

Add filesystem watching, polling fallback, spool replay, graceful shutdown.

**Scope:** `notify` integration, debouncing, 30s poll cycle, SIGTERM handling, launchd/systemd service generation.

**Validation:**
- Run Rust daemon for 24h alongside Python daemon (different API tokens)
- Compare event delivery completeness
- Monitor RSS, CPU over 24h

### Phase 3: Feature Parity & Cutover

- Add `status`, `reset` subcommands
- Update `longhouse connect` to auto-detect and delegate to Rust engine
- Add `--python` fallback flag
- Update launchd plist to use Rust binary
- Update documentation

### Phase 4: Python Deprecation

- Remove Python shipper code (parser.py stays — used by MCP server)
- Remove Python daemon dependencies (httpx, etc.)
- Shipper becomes Rust-only

---

## Testing Strategy

### Unit Tests (Rust)

```
tests/
├── parser_test.rs        # JSONL parsing, edge cases, encoding
├── payload_test.rs       # Ingest payload construction
├── compressor_test.rs    # Streaming gzip correctness
├── state_test.rs         # SQLite file_state operations
├── spool_test.rs         # SQLite spool_queue operations
├── discovery_test.rs     # Provider file discovery
└── integration_test.rs   # Full pipeline: file → compressed payload
```

### Compatibility Tests

Python-Rust cross-validation:

```python
# tests/services/shipper/test_rust_compat.py
def test_rust_python_parity():
    """Ship same files with Python and Rust, compare results."""
    # Parse with Python
    py_events, py_offset, py_meta = parse_session_file_full(test_file)

    # Parse with Rust (via subprocess)
    result = subprocess.run(
        ["longhouse-engine", "ship", "--dry-run", "--json", str(test_file)],
        capture_output=True
    )
    rust_output = json.loads(result.stdout)

    # Compare event counts, offsets, metadata
    assert len(py_events) == rust_output["event_count"]
    assert py_offset == rust_output["last_offset"]
```

### Benchmark Suite

```rust
// benches/pipeline_bench.rs (criterion)
fn bench_parse_large_file(c: &mut Criterion) { ... }
fn bench_gzip_streaming(c: &mut Criterion) { ... }
fn bench_full_pipeline(c: &mut Criterion) { ... }
```

Compare against Python profiling baselines at all 3 scale levels.

---

## Open Questions

1. **zstd vs gzip?** zstd is faster and compresses better. But the server currently only accepts gzip (`Content-Encoding: gzip`). Adding zstd support server-side is trivial (FastAPI middleware) but requires coordination. Decision: ship with gzip for compatibility, add zstd as opt-in later.

2. **Workspace layout?** Options:
   - Standalone repo (`longhouse-engine/`)
   - In zerg monorepo (`apps/engine/`)

   Recommendation: `apps/engine/` in monorepo. Keeps CI, specs, and tests co-located. Cargo workspace if we add more Rust crates later.

3. **mmap safety?** `memmap2::Mmap::map()` is `unsafe` because another process could modify the file while mapped. For JSONL files being actively appended, this means we could read partial lines or corrupted data. Mitigation: always validate JSON parse results, treat parse errors as "partial line at EOF" (same as Python's behavior).

4. **Cross-compilation for CI?** GitHub Actions macOS runners are x86_64 (or Apple Silicon via M1 runners). Cross-compiling `aarch64-apple-darwin` from x86_64 requires Xcode command line tools. Alternative: use `cross` crate for Linux targets, native builds for macOS.

---

## Success Criteria

1. `longhouse-engine ship` processes 9.6GB dataset in <30s (vs 206s Python)
2. RSS stays <200 MB for full dataset (vs 3.4 GB Python)
3. Event counts match Python exactly (zero data loss)
4. SQLite DB is forward/backward compatible between Python and Rust
5. Binary <10 MB stripped
6. Startup <100ms
7. Watch mode latency: file change → server receipt <2s
8. 7-day soak test: daemon stable, spool bounded, no leaks
