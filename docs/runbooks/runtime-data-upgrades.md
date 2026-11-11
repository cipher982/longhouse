# Runtime data upgrades

Operator steps for upgrades that change stored data on a Runtime Host.

## Render branch counts

Upgrading to generation-wide abandoned-output counts needs an explicit backfill
of existing render history. Prepare a verified cache before the API cutover, and
apply it once the updated catalog writer is running:

```bash
longhouse-server db repair-render-counts --database /data/longhouse-live.db --cache /data/cache/render-counts.jsonl
longhouse-server db repair-render-counts --database /data/longhouse-live.db --cache /data/cache/render-counts.jsonl --apply
```

- Use your installation's catalog database and immutable-object root
  (`--object-root` overrides the Runtime Host setting).
- Preparation reads sealed render files, not raw provider archives. Applying
  uses the catalog writer and needs zero missing current facts. Neither phase
  rewrites source history.
- Keep the cache across container replacement.
- `--limit` samples preparation only and cannot be combined with `--apply`.

Catalog schema fingerprints reject mixed reader and writer versions. When the
writer cannot be upgraded independently, use a maintenance window: start the new
catalog, apply the prepared facts, then start the new API. Run `--apply` again
after the API upgrade to cover objects the old writer created in between.

## Historical indexing convergence

Upload receipts do not show that history is indexed. On the Runtime Host, give
the checker a JSON array of session UUIDs (or objects with `session_id`):

```bash
python -m zerg.cli.historical_convergence \
  --catalog-db /data/longhouse-live.db --search-db /data/search.db \
  --cohort /data/cache/cohort.json --output /data/cache/convergence.json \
  --wait --timeout 600
```

It opens both databases read-only, captures fixed target revisions, and checks
published search fences and active embedding completion. `--wait` fails on
incomplete convergence or timeout. Without it, a zero-exit `pending` result is a
diagnostic snapshot, not a pass. The output keeps counts and failure categories,
never transcript text. From a checkout: `make historical-convergence-check ARGS="..."`.
