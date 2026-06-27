# Inline Media Backfill Rollout

**Status:** findings and rollout plan for review; no write backfill has run.
**Related spec:** `docs/specs/media-refs-client-rendering.md`
**Branch:** `feat/media-refs-renderer`

## PM Read

The renderer branch makes `media_refs` visible in web and iOS, but historical
value depends on repairing legacy inline `data:image/...` payloads into real
media objects. A full hosted dry-run against `david010` found enough media to
justify the work:

- 13,835,553 `source_lines` rows scanned.
- 9,894 inline media candidates found.
- 4,127,942,117 decoded bytes, about 3.84 GiB before SHA dedupe. This is a
  capacity signal, not an exact on-disk forecast.
- 719 malformed or unsupported candidates rejected.
- 0 byte-budget skips and 0 disk-floor skips in dry-run.
- Final cursor: `source_lines.id = 16,304,286`; final page scanned 553 rows.

This is not a cosmetic edge case. It is a product-memory fidelity repair:
screenshots and generated/attached images are already in transcript history, but
clients cannot render them until those inline payloads are converted into
`SessionMediaRef` + `MediaObject` rows.

The risk is operational, not architectural. The server endpoint already exists
and has guardrails; the missing piece is a controlled, resumable operator runner
instead of manually curling 13,836 batches. The apply run also needs explicit
backup, idempotency, and live-ingest contention gates before touching hosted
`david010`.

## What Is Already Accomplished

- `media_refs` now flow through the web hand-written event model.
- Web session timeline renders present-state event media using `thumb_url` when
  available and `blob_url` as the fallback/open target.
- Share-token timeline views suppress media for now, because `/api/media/*`
  authorizes with the browser owner cookie and does not yet accept share tokens.
- `media_refs` now flow through iOS `SessionEvent`, generated API adapters, and
  the WebView transcript payload.
- iOS injects managed auth cookies into the `WKWebView` cookie store before
  rendering media-bearing transcript HTML.
- `make test-frontend` and `make test-ios` passed on the renderer branch before
  this rollout investigation.
- Hosted dry-run evidence was collected without writes.

## Evidence Artifacts

One-off investigation scripts and output live outside the repo:

- `/tmp/longhouse-media-backfill-investigation/dry_run_sweep.zsh`
- `/tmp/longhouse-media-backfill-investigation/dry_run_resume.zsh`
- `/tmp/longhouse-media-backfill-investigation/dry_run_pages.jsonl`
- `/tmp/longhouse-media-backfill-investigation/dry_run_summary.json`

The first sweep hit a transient curl/TLS failure after 2,815 pages. The resume
script continued from the last recorded `last_source_line_id` and completed the
corpus. This is the strongest operational lesson from the investigation: apply
must be cursor-resumable and log every page.

The checked-in runner was then run in dry-run mode on 2026-06-27. It completed
against live hosted `david010`, recovered from transient request timeouts, and
matched the media totals from the prototype sweep exactly: 9,894 candidates,
4,127,942,117 decoded bytes, 719 rejected, and 0 budget skips. The live corpus
boundary had moved because new source lines landed between sweeps: 13,858 pages,
13,857,449 rows scanned, final cursor `16,326,182`, final page 449 rows. Treat
this checked-in runner output as the current canonical dry-run baseline; the
older `/tmp` prototype artifact remains useful as historical evidence.

## Endpoint Contract

Endpoint:

```text
POST /api/agents/media/backfill-inline-data-urls
```

Auth:

- `X-Agents-Token` machine token.
- Single-tenant guard.

Query parameters:

| Param | Dry-run value used | Write recommendation | Notes |
|---|---:|---:|---|
| `dry_run` | `true` | `false` | Writes require the backup gate. |
| `max_rows` | `1000` | `1000` | Router cap. |
| `max_bytes` | `52428800` | `52428800` | Router cap, 50 MiB decoded/page. |
| `after_id` | cursor | cursor | Use returned `last_source_line_id`. |
| `confirmed_backup_gate` | `false` | `true` | Required when writing. |
| `disk_floor_bytes` | default | set explicitly | Pick a PM/operator floor before apply. |

The endpoint scans `source_lines.id > after_id`, decodes supported
`data:image/...;base64` payloads, computes SHA-256, and, only when
`dry_run=false`, upserts `SessionMediaRef(original_kind=data_url_backfill)` and
stores a content-addressed blob. It never rewrites source rows.

## Dry-Run Findings

The full dry-run completed in 13,836 API batches. `last_source_line_id` must be
treated as the cursor; source-line IDs have gaps, so page count is not a durable
resume token.

Top decoded-byte pages stayed below the 50 MiB batch cap. The largest observed
page was 39,267,642 bytes, about 37.45 MiB, at cursor `9,031,733`. Because
`skipped_budget` stayed at 0, the current `max_bytes=50MiB` setting is adequate
for apply.

Candidate count and storage pressure are different signals. Some pages contain
many tiny or malformed inline payloads. For example, the highest candidate page
had 77 candidates but only about 3.63 MiB decoded; several tail pages had
candidate refs with zero valid decoded bytes because most candidates were
rejected. Operator logs should report accepted bytes, rejected candidates, and
budget skips separately.

`decoded_bytes` is counted before SHA dedupe, and dry-run does not exercise the
write path that would collapse duplicate media objects. Current
`store_media_blob` does not generate thumbnails, but the dry-run total still
should not be used as exact disk consumption. Treat it as a conservative raw
payload size, then choose `disk_floor_bytes` from verified live filesystem
headroom with a wide margin.

Media is spread across the corpus, not confined to early rows. Cursor bands near
1M, 2M, 6M, 9M, 10M, and 12M have meaningful volume, and valid media still
appears past cursor 15M. A recent-only or early-only backfill would leave visible
holes.

Approximate cursor-band distribution:

| Cursor band | Candidates | Decoded MiB | Rejected |
|---:|---:|---:|---:|
| 0M | 376 | 90.3 | 6 |
| 1M | 998 | 662.0 | 3 |
| 2M | 858 | 410.4 | 15 |
| 5M | 1,004 | 220.4 | 12 |
| 6M | 1,127 | 408.6 | 14 |
| 8M | 740 | 363.1 | 42 |
| 9M | 1,729 | 1,004.5 | 97 |
| 10M | 1,416 | 321.6 | 122 |
| 11M | 253 | 94.6 | 100 |
| 12M | 312 | 108.4 | 32 |
| 13M | 131 | 20.2 | 45 |
| 14M | 268 | 43.4 | 84 |
| 15M | 184 | 24.0 | 113 |

Bands not shown are either sparse, reject-only, or materially smaller. Full
machine-readable evidence is in the JSONL artifact.

## Recommended Rollout

1. Keep the renderer branch separate from the write backfill decision until this
   plan is reviewed.
2. Add a checked-in operator runner under `scripts/ops/`, modeled after existing
   one-off operational scripts but targeting the hosted API rather than local
   SQLite.
3. Runner requirements:
   - Supports `--api-url`, `--token-file`, `--dry-run`, `--apply`,
     `--after-id`, `--max-rows`, `--max-bytes`, `--disk-floor-bytes`, and
     `--out`.
   - Refuses apply unless explicit apply confirmation and explicit backup
     confirmation flags are present.
   - Writes one JSONL response per batch plus a final summary.
   - Resumes from the last non-null `last_source_line_id` in the JSONL by
     default, so a trailing zero-row page cannot reset the cursor to zero.
   - Retries transient curl/network failures with backoff.
   - Fails fast on non-retryable 4xx responses.
   - Stops cleanly when `scanned_source_lines < max_rows`.
   - Treats nonzero `skipped_budget` as a loud failure unless the operator
     explicitly acknowledges it.
   - Refuses `--apply` unless a dry-run summary matches the saved artifact's
     completion state, final cursor, and aggregate counts, or the operator
     explicitly supplies a new accepted baseline.
4. Re-run checked-in runner in dry-run mode once, and make parity with the
   `/tmp` artifact a hard gate before apply.
5. Before apply, verify and record a restorable DB backup plus media filesystem
   backup/snapshot. `confirmed_backup_gate=true` is only an API boolean; it does
   not prove a backup exists.
6. Run an apply preflight on a copy of the hosted DB/media directory:
   - apply one known candidate batch;
   - re-run the same batch and confirm `refs_upserted=0` and `stored_objects=0`
     or otherwise explain idempotent results;
   - exercise the delete-by-`original_kind=data_url_backfill` rollback script;
   - record per-batch latency.
7. Decide apply throttle/off-peak window from the preflight. The endpoint scans,
   decodes, stores blobs, and commits synchronously per request against SQLite;
   the write run should not starve live ingest.
8. Apply the whole corpus from `after_id=0` with page-level logging.
9. Verify after apply:
   - Runner totals show expected `refs_upserted`, `stored_objects`,
     `skipped_existing_refs`, `rejected`, and `skipped_*` counts.
   - `GET /api/agents/ingest-health` should not be the primary success counter:
     a clean write stores blobs and marks refs present in the same batch, so
     `media_repair_refs` may stay near zero rather than "rise then settle."
   - Sample source-line cursor bands with known candidates have present
     `media_refs` in session projection.
   - Browser owner view renders images.
   - iOS renders images after a local Xcode install, because iOS is not deployed
     by push.

## Stop And Rollback Posture

Stopping is safe between batches. Each endpoint call commits at most one batch,
and the runner can resume from the last returned `last_source_line_id`.

The endpoint does not mutate historical source rows. It adds
`SessionMediaRef(original_kind=data_url_backfill)` rows and content-addressed
`MediaObject` blobs. A targeted delete-by-`original_kind` cleanup script should
exist and be tested on a copy before apply; do not improvise deletes during the
hosted write run.

## Open Decisions

- Apply scope: recommended default is whole corpus, because valid media appears
  throughout the full cursor range.
- Disk floor: pick the minimum free bytes to leave on the hosted media
  filesystem before apply. The dry-run decoded total is 3.84 GiB before dedupe,
  not exact on-disk growth.
- Apply window and throttle: decide how aggressively to run against live hosted
  SQLite after measuring per-batch latency on a copy.
- Share-token media: recommended later. Owner-auth rendering is enough for the
  launch loop; public shared sessions can keep suppressing media until media
  authorization grows a share-token path.
- Noise filtering: recommended later. Backfill all supported image payloads now;
  inspect rendered samples after repair before adding thumbnail/noise heuristics.
- Commit/check-in runner before apply: recommended yes. The `/tmp` scripts proved
  the shape but should not be the write vehicle.

## Recommendation

Proceed in this order:

1. Get review on this rollout spec and the renderer spec.
2. Ask Hatch Opus to challenge the findings, safety gates, and PM tradeoffs.
3. Incorporate review notes.
4. Add the checked-in resumable runner.
5. Run the runner dry-run against hosted and compare summary to this artifact.
6. Verify backup/restore, idempotency, rollback, and per-batch latency on a
   copy.
7. Apply the backfill only after PM approval of whole-corpus scope, disk floor,
   and apply window.
