# Archive-Primary Rollback Runbook

Use this when Phase 7 archive-primary ingest is enabled and new raw transcript
data needs to fall back to legacy hot-DB raw writes.

## Flags

Archive-primary raw writes are opt-in:

```text
LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED=1
```

Legacy raw writes are enabled by default. Keep them enabled for the initial
archive-primary rollout:

```text
LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=1
```

Disable legacy raw writes only after archive manifests, chunk verification, and
projector comparison are healthy for both raw archive streams:

- `source_lines`: raw provider transcript/source lines;
- `events`: raw provider event payloads that may not be reconstructable from
  source lines.

```text
LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=0
```

`LONGHOUSE_DISABLE_LEGACY_RAW_WRITES=1` is a stronger kill switch. Do not set it
unless archive-primary writes are also enabled and verified.

## Normal Phase 7 Rollout

1. Confirm the backup gate in `docs/specs/reliability-data-plane.md` is still
   accepted for this work.
2. Enable archive-primary with legacy fallback still on:
   `LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED=1` and
   `LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=1`.
3. Restart the Runtime Host.
4. Verify `/api/readyz` returns ok.
5. Send a small ingest and confirm response headers:
   `X-Ingest-Archive-Primary: written` and
   `X-Ingest-Legacy-Raw: enabled`.
6. Verify new `archive_chunks` manifest rows and readable archive chunk files
   for both `source_lines` and `events` streams when the ingest includes both
   payload types.
7. Only then test legacy raw disable on a low-risk tenant or local fixture.

## Fallback Behavior

When archive-primary is enabled and legacy raw writes are also enabled:

- archive prepare/write success stores raw source bytes in the archive and keeps
  legacy raw writes on. The expected response headers are
  `X-Ingest-Archive-Primary: written` and `X-Ingest-Legacy-Raw: enabled`;
- archive prepare/write failure falls back to legacy raw writes;
- fallback responses expose `X-Ingest-Archive-Primary: fallback` and
  `X-Ingest-Legacy-Raw: enabled`;
- no request should depend on derived/archive availability for hot list,
  launch, health, heartbeat, or control paths.

When archive-primary is enabled and legacy raw writes are disabled:

- archive prepare/write success stores raw source bytes in the archive and skips
  legacy `source_lines` / raw event blob persistence. The expected response
  headers are `X-Ingest-Archive-Primary: written` and
  `X-Ingest-Legacy-Raw: disabled`;
- archive prepare/write failure fails closed with HTTP 503;
- the response exposes `X-Ingest-Archive-Primary: failed`;
- re-enable legacy raw writes or disable archive-primary before retrying ingest.

## Roll Back To Legacy Raw Writes

1. Set:

   ```text
   LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED=0
   LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=1
   LONGHOUSE_DISABLE_LEGACY_RAW_WRITES=0
   ```

2. Restart the Runtime Host.
3. Verify `/api/readyz` returns ok.
4. Send a small ingest and confirm:
   `X-Ingest-Archive-Primary: disabled` and
   `X-Ingest-Legacy-Raw: enabled`.
5. Confirm legacy raw rows resume:
   - new `source_lines.raw_json_z` rows for source-line ingests;
   - new `events.raw_json_z` rows for event ingests with raw JSON.
6. Leave archive files and manifests in place. Do not delete, compact, vacuum,
   or rewrite raw data as part of rollback.

## Pause Points

Pause the rollout and keep legacy raw writes enabled if any of these occur:

- archive chunk verification fails;
- projector checkpoints stop advancing;
- archive lag grows without explanation;
- ingest responses show repeated `X-Ingest-Archive-Primary: fallback`;
- hot list, health, launch, heartbeat, or control tests touch archive/derived
  stores.

## Prohibited In Phase 7

- deleting legacy raw rows;
- `VACUUM` or `VACUUM INTO` on the hosted dogfood DB;
- table rebuilds of raw legacy tables;
- treating archive-primary as approval to compact or reclaim storage;
- disabling legacy raw writes on a tenant without a validated archive path.
