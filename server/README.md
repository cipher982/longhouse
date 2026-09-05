# Longhouse Backend

FastAPI backend and CLI package for Longhouse.

## Install

```bash
uv tool install longhouse
longhouse-server serve
```

Full docs and the recommended hosted/self-host flows live in the main repository README: https://github.com/cipher982/longhouse

## Repairing held interactions

The live catalog owns provider-wait lifecycle. Execution end revokes held
permissions and managed-push provider questions, but does not delete history or
expire durable Longhouse questions merely because activity is stale. Run-less
legacy exits are matched only to an unambiguous owning thread/run that started
before the exit; later executions remain untouched.

Catalog maintenance also repairs historical no-expiry waits, stale runtime
pointers, and run/connection rows contradicted by recorded execution exits.
Run this in the Runtime Host's environment so `catalogd_paths()` uses its active
live-store configuration. It talks to the single catalog writer, not SQLite:

```python
import json
from datetime import UTC, datetime
from zerg.catalogd.client import call_catalogd_sync
from zerg.services.catalogd_supervisor import catalogd_paths

report = call_catalogd_sync(
    catalogd_paths()[1],
    "interaction.expire_due.v2",
    params={"now": datetime.now(UTC).isoformat(), "dry_run": True},
    timeout_seconds=60,
)
print(json.dumps(report, indent=2))
```

Set `dry_run` to `False` to apply the currently evidenced repairs, or add
`session_id` to scope either operation to one session. Reports name affected
interactions and runs. Repeated application is idempotent. Neither this repair
nor normal lifecycle cleanup edits archive transcripts or activity timestamps.

## Repairing imported Cursor activity clocks

Older Cursor imports could use ingestion time as session activity. The engine
now reads provider metadata and matching managed turn receipts instead.
`longhouse-engine parse <agent-transcripts/...jsonl>` prints `last_activity`
from that same source resolver; a missing source clock is not evidence for a
historical correction.

After verifying the source clock, use the same catalog client with method
`storage.cursor.activity.repair.v2` and parameters `session_id`,
`expected_started_at`, `expected_last_activity_at` (the current stored values),
`source_started_at`, `source_last_activity_at` (the verified provider values),
`now`, and `dry_run`. Timestamps are ISO-8601. Preview with `dry_run=True`,
then apply with `False`. Both clocks are compared before writing, preventing
an older audit from overwriting concurrent updates. Source creation may move
earlier, never later, and must precede source activity. Source activity cannot
exceed the expected stored activity. This also repairs old imports that
fabricated both creation and activity clocks.
This corrects timeline recency without rewriting immutable transcript objects.
