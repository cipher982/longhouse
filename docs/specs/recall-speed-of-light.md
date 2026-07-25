# Recall: speed-of-light analysis

**Date:** 2026-07-25
**Question:** what is the physical floor for "an agent retrieves a fact from its own
history", how far is Longhouse from it, and what design closes the gap under our
constraints.
**Method:** measure the real corpus and the real system, compute the floor from first
principles, attribute the gap. Every number is measured on `cinder` against hosted
`david010`. Extrapolations are labelled; one of them was wrong and is corrected in §2.

Follows `cross-session-recall-postmortem.md`, which fixed *reachability*. This is about
*optimality*.

---

## 1. The floor

An agent needs a passage of ~200-500 tokens that exists somewhere in its history. The
irreducible work:

| Step | Floor | Why irreducible |
|---|---|---|
| Locate | one index probe | you must find it |
| Deliver | ~500 tokens | the answer's own size |
| Judge | one model pass over 500 tokens | the agent must read it |
| Transport | **zero** | the data was generated on this machine |

That last row is the analysis. Longhouse's corpus is produced by agents on the user's own
hardware. Nothing about retrieval requires a network. The transport floor is not "fast
network" — it is *no network*.

**Speed of light ≈ one local index probe + ~500 tokens + one model pass.**

Measured floors on this laptop, against a purpose-built **10.8 M-row** FTS5 index
(1.56 GB, built in 87 s):

| Operation | p50 | p99 |
|---|---|---|
| FTS5 rare term, limit 20 | **0.014 ms** | 0.222 ms |
| FTS5 rare term + snippet | 0.093 ms | 0.272 ms |
| FTS5 two-term AND | 0.018 ms | 0.028 ms |
| FTS5 common term, limit 20, unranked | **0.011 ms** | 0.021 ms |
| Exact brute-force vector top-k, 480 K × 256d f32 | 4.9 ms | — |

So the floor is **~0.02 ms lexical, ~5-50 ms semantic, ~500 tokens, no round trip.**

---

## 2. How big is the corpus, really

I made an extrapolation error worth recording, because the design conclusion depends on
the answer and the two estimates differ by 11x.

**Estimate 1 (low), from the local durable DB.** 23,040 events holding 2.1 MB of
`content_text` = 91 bytes/event; × ~10.8 M events ≈ **0.98 GB text, ~480 K chunks**.

**Estimate 2 (high), from provider transcripts on disk.** Measured directly:

```
~/.claude/projects   4.8 GB   5,318 .jsonl files   2026-01-08 → 2026-07-24
~/.codex/sessions     17 GB   4,277 .jsonl files
                     ─────
                      22 GB   9,595 files
```

Sampling 30 Claude files: message content is **51.3%** of raw bytes. Extrapolated:
**~11.3 GB of text, ~2.8 B tokens, ~5.5 M chunks.**

The low estimate is almost certainly depressed by the stale April snapshot and by
truncated `content_text`. The true figure is somewhere in **480 K – 5.5 M chunks**, and
this needs settling by counting the real index rather than extrapolating. It matters:

| Corpus | Exact brute-force top-k (256d f32) | Resident |
|---|---|---|
| 480 K chunks | **4.9 ms** | 0.49 GB |
| 1 M chunks | 10.1 ms | 1.02 GB |
| 5.5 M chunks | **53.7 ms** | 5.63 GB |

At the low end, exact search is free and there is nothing to decide. At the high end,
53.7 ms is still 35x better than today's semantic path, but 5.6 GB resident on a laptop
is a real cost and a real decision.

**Measured trap: quantization is not a free win.** In numpy on this CPU:

| Shape | f32 | int8 | f16 |
|---|---|---|---|
| 5.5 M × 256 | 53.7 ms / 5.63 GB | **489 ms** / 1.41 GB | — |
| 1 M × 256 | 10.1 ms / 1.02 GB | 88 ms / 0.26 GB | — |
| 480 K × 384 | 7.9 ms / 0.74 GB | — | **243 ms** / 0.37 GB |

int8 is **9x slower** than f32 and f16 is **30x slower**, because neither has a fused
GEMV path here — the upcast materializes. A hand-written NEON kernel with `vdotq_s32`
would change this, but that is an unmeasured claim and must not be assumed. Today, on
this hardware, **f32 is the fast path and quantization buys memory at a large latency
cost.**

The headline remains: at 10^5–10^6 chunks, a flat matrix and one matmul returns **exact**
top-k in single-digit milliseconds. No ANN index, no vector database, no recall@k to
regress. Revisit above ~2 M chunks.

---

## 3. Actual

Measured, 5 samples each, `cinder` → hosted `david010`:

| Call | p50 | Payload |
|---|---|---|
| `search_sessions` narrow (project + 14d) | **260 ms** | 21 KB (~5.3 K tokens) |
| `search_sessions` broad (90d, no project) | **880 ms** | 54 KB (~13.5 K tokens) |
| `tail` (roles-filtered, 8 events) | 300 ms | 7 KB (~1.7 K tokens) |
| `tail` (raw, 100 events) | 300 ms | 69 KB (~17 K tokens) |
| `recall` (natural language) | 330 ms | 41 KB (~10 K tokens) |
| **`search_sessions` semantic** | **1,350–1,900 ms** | — |

The two-call journey shipped in the postmortem costs **~560 ms and ~7,000 tokens** to
deliver a ~200-token answer.

### The gap

| Axis | Floor | Actual | Gap |
|---|---|---|---|
| Latency, lexical | 0.02 ms local probe | 560 ms (2 calls) | **~28,000x** |
| Latency, semantic | 5–54 ms | 1,900 ms | **35–380x** |
| Context per answer | ~500 tok | 7,000 tok | **14x** |
| Context, broad search | ~500 tok | 13,500 tok | **27x** |
| Round trips | 1 | 2 | 2x |

None of this is algorithmic. SQLite is not slow and the corpus is not large. The gap is
**topology and projection**.

---

## 4. Where the gap comes from

### 4.1 The data crosses a WAN to be read back by the machine that wrote it

An agent on `cinder` generates a transcript. The engine ships it to hosted. Later an
agent on `cinder` wants it back and pays 260–1,900 ms of WAN round trip, TLS, FastAPI,
SQLite contention and JSON serialization to read data that was born on local NVMe.

The local durable store is dead:

```
~/.longhouse/longhouse.db   235 MB   59 sessions   23,040 events
                                     newest event: 2026-04-23
```

Three months stale against 21,194 sessions hosted, plus ~650 MB of abandoned
`longhouse.pre-subagent-*` copies nothing reads.

`VISION.md` holds that "user-owned machines and self-hosting are the default truth;
hosted is convenience." In the *write* path that is true. In the **read** path it is
inverted: hosted is the only real corpus and the user-owned machine is a husk.

**But the raw data is already local.** The engine's pipeline is parse → compress → batch
→ ship (`engine/src/pipeline/`); it reads the provider's own `.jsonl` transcripts. Those
22 GB of files are the durable local corpus, they already exist, and Longhouse already
treats them as the ingest source of truth. So closing this gap does not require building
local durable storage. **It requires indexing files the engine already reads.** The
compressor already parses every event; writing them into a local index is incremental
work on a pass that is already happening.

### 4.2 The fast index already exists, on the far side of the network

`server/zerg/searchd/` is a Unix-socket daemon that exclusively owns a **disposable**
SQLite FTS5 database (`search.db`: `events_fts`, `searchable_fts`, `session_index`,
`indexed_objects`, generation tracking). The factoring is right — single-writer,
disposable, rebuildable from the durable store, separate from the transactional DB.

It runs with the Runtime Host, so the correctly-designed fast index sits on the wrong
side of the WAN from every query. Locally `search.db` exists at 124 KB, empty.

The recommendation is therefore not "build an index" but "run the index that already
exists where the queries originate."

### 4.3 94% of the retrieval payload is liveness metadata

Measured on a 95-row `search_sessions` response:

```
95 rows, 547,089 bytes (~136,772 tokens)
5,758 bytes/row, 66 fields/row
recall-relevant: ~300 bytes/row  →  ~94% overhead
```

The payload carries `active_tool`, `presence_state`, `presence_tool`,
`presence_updated_at`, `launch_state`, `launch_error_code`, `launch_error_message`,
`control`, `capabilities`, `runtime_phase`, `runtime_source`, `display_phase`,
`execution_lifetime`, `loop_mode`, `is_writable_head`, `continuation_kind`.

Every one is for steering a live session in the timeline UI. None helps decide *which
past session discussed wireless ADB*. `search_sessions` inherited the projection built
for the live wall, so a history query pays for a steering payload. A 95-row search
returns 137 K tokens, exceeding most context budgets on its own.

### 4.4 Semantic retrieval pays a remote embedder

`config/models.json` configures embeddings as OpenRouter →
`openai/text-embedding-3-small`, 256 dims. Every semantic query makes a network round
trip to a third party *just to embed the query string*, before any search runs. That is
most of the measured 1,350–1,900 ms.

This is the binding constraint on local-first semantic search: a 5 ms local vector search
is pointless behind a 300 ms remote embed.

### 4.5 The ranking cliff (new, and a landmine)

Measured on the 10.8 M-row index:

| Query | p50 |
|---|---|
| rare term, unranked, limit 20 | 0.014 ms |
| rare term, `ORDER BY bm25()`, limit 20 | 0.134 ms |
| common term, unranked, limit 20 | 0.011 ms |
| **common term, `ORDER BY bm25()`, limit 20** | **4,954 ms** |

A 380,000x cliff driven purely by term selectivity. `ORDER BY bm25()` must score every
match before returning top-20, and the common term matched 4,854,060 rows.

Mitigation is not as easy as it looks. Bounding the candidate set first:

| Approach | p50 |
|---|---|
| bm25 over a bounded 1 K candidate window | 187 ms |
| bm25 over a bounded 5 K candidate window | 192 ms |
| **no ranking, match order, limit 20** | **0.014 ms** |

Even a 1 K-row bounded window costs 187 ms. **All of the cost is in ranking, none is in
finding.** This would have shipped looking fine — every test query a developer types by
hand is a rare term — and then hung for five seconds the first time an agent searched for
"error" or "test" with ranking on.

---

## 5. Optimal design under our constraints

Constraints, unrelaxed:

- SQLite is the only core database.
- The device product is two paired **Rust** binaries; the installer never puts Python,
  `uv`, or a server command on a device (`native-device-runtime.md`, enforced by a
  hermetic smoke that traps `python`/`uv`/`pip`).
- One session, one execution owner; no silent fallbacks.
- Design for cold restart; durable state and reconstructable context.
- Longhouse owns raw agent history.
- Prefer deletion and obvious seams over clever abstractions.
- Keep judgment in the model (`agents-know-best`).

The SOL-optimal shape:

> **Retrieval is a local, disposable, Rust-owned FTS5 index over transcripts the engine
> already reads. It finds candidates in microseconds and does not rank them. The model
> ranks.**

That last sentence is the design conclusion, and §4.5 is why it is not merely an
ideological preference. Finding costs 0.014 ms; ranking costs 187–4,954 ms. The model is
already reading the candidates in its forward pass, and ranking relevance is what it is
best at. Moving ranking out of SQL removes the single largest cost in the system and the
only cliff in it.

Concretely:

1. **Index locally, from the ingest pass the engine already runs.** Disposable and
   rebuildable, so relocation carries no migration risk — worst case is a rebuild.
2. **Return raw candidates in match order, with snippets.** No bm25, no reranker, no
   summarization layer. Cap candidates and say so in the response.
3. **A recall-shaped projection**: `session_id`, `project`, `when`, `match_snippet`,
   `match_event_id`, `match_role`. ~300 bytes, not 5,758.
4. **Semantic only if the embedder is local.** Either a Rust embedder
   (`fastembed-rs`/`candle`, +30-100 MB model, no Python, fits the device contract) over a
   flat f32 matrix — exact, 5–54 ms depending on where the corpus actually lands — or
   defer semantic entirely. Never a local index behind a remote embedder.
5. **No ANN index, no vector database** below ~2 M chunks. And if memory forces
   quantization, write and measure a real SIMD kernel; do not assume int8 is faster,
   because measured here it is 9x slower.

### What this deliberately does not add

Ranking pipelines, learned rerankers, query classifiers, summarization between the agent
and the transcript. A 0.014 ms search that returns raw passages lets the model expand its
own query and iterate three times for less latency than one of today's calls.

---

## 6. Paths forward

Ranked by payoff per unit of risk. Independently shippable.

### A. Recall-shaped projection — small, pure win
Only recall-relevant fields in history results. **15-20x context reduction**
(5,758 → ~300 bytes/row). No architectural change, no migration, no new component. This
is the cheapest large win available and should ship regardless of everything else.

### B. Remove ranking from the hot path — small, prevents a 5-second hang
Return match-order candidates; drop `ORDER BY bm25()` or bound it explicitly and
document the cost. Prevents a latent 4,954 ms stall that current testing would never
catch. Cheap, and it is a correctness fix as much as a performance one.

### C. Count the corpus — one afternoon, unblocks the semantic decision
The 11x disagreement in §2 decides whether semantic search is a 5 ms freebie or a 54 ms /
5.6 GB engineering decision. Build the real index and count. Everything in D depends on
this number.

### D. Decide the semantic story
Two coherent options; the current state is neither:
- **Local Rust embedder**: 1,900 ms → 5–54 ms, exact, offline-capable, +30-100 MB model.
- **Drop semantic**, keep lexical + model-driven query expansion. Zero new dependencies,
  and the evidence mildly favours it: `recall` scored 0.018 on a well-formed query while
  lexical answered the same question in one call.

### E. Device-side index — the structural fix
Port the FTS5 index to the Rust engine, populated from the ingest pass. **260 ms →
0.014 ms**, and correctness improves because retrieval stops depending on WAN
reachability. This is what moves the system from ~28,000x off SOL to roughly 1x. Bigger
than A–D and worth staging behind them, but it is the answer.

### F. Within-session retrieval — the largest *functional* gap
`search_sessions` finds a session; `tail` reads its end. Neither searches *inside* one.
On the 8,135-event session in the sample, or the 902-tool-call g55 session, finding a
specific decision is still a needle problem, and `tail` cannot paginate
(`window_exhausted` reports the wall rather than moving it). A and E make this cheaper to
attack. This is where the launch story about session memory either lands or does not.

### G. Reclaim ~650 MB of dead local backups
`longhouse.pre-subagent-backfill-*` and `longhouse.pre-subagent-rehome-*`. Trivial, free,
unrelated.

---

## 7. The one-line version

The index is already correctly designed, exact search over the whole corpus is
microseconds for finding and seconds for ranking, and we spend 260–1,900 ms shipping
queries across a WAN to read transcripts sitting in `~/.claude/projects`. The gap to
speed of light is not algorithmic: retrieval runs in the wrong place, returns the wrong
shape, and pays SQLite to do the one job the model should do itself.
