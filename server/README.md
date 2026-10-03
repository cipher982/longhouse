# Longhouse Backend

FastAPI backend and CLI package for Longhouse.

## Install

```bash
uv tool install longhouse
longhouse-server serve
```

Full docs and the recommended hosted/self-host flows live in the main repository README: https://github.com/cipher982/longhouse

## Provider capability evidence

Reference-based factory proofs use a dedicated read-only S3-compatible store,
configured through `PROVIDER_CAPABILITY_BLOB_S3_*` in the root `.env.example`.
This is separate from tenant media storage; no ambient AWS credentials are used.
An unconfigured resolver rejects reference-proof publication explicitly.

The Runtime Host verifies every referenced object's length and SHA-256 before
acceptance, retaining attested metadata rather than copying evidence bytes.
Existing inline proofs remain immutable and readable. Evidence downloads require
owner-capable authentication; managed-session credentials cannot read them.

`GET /api/agents/provider-capabilities` is the authenticated assurance diagnostic;
`GET /api/public/provider-certification` supplies the public provider chart.
These reads validate current qualifying proof candidates, or the latest attempt
when none qualifies, reusing publication facts within the request. They do not
hash unrelated blobs or run orphan-history audits. Full store integrity audits remain available;
missing or tampered supporting evidence still rejects a proof.

Live background qualification uses `claude_background_v1` and
`omp_background_v1` through the existing provider qualification dispatcher.
The oracles retain native source, read canonical workspace state rather than
summary DTOs, and publish exhaustive artifact manifests. Writer-disabled
controls require a matching fired receipt and healthy unrelated source,
identity and cleanup observations; missing source is never a passing control.
Claude's native fault seam is compiled only with `qa-fault-injection`.

## Archived child identity

Storage-v2 retains the source descriptor's native session ID even when a Shadow
archive has no live thread. Parent/child resolution is scoped to owner, machine
and provider and refuses ambiguous bindings. Child archives stay separate;
their lineage does not assert an active background registry. Older rows with no
retained native identity remain unknown until authoritative source re-ingress.


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

## Session discovery and recovery

Session listings search current catalog titles alongside transcript text. Title
updates are searchable without rebuilding the transcript index; title-only hits
do not invent a transcript event or source locator. Conversation recall remains
transcript-based. Owner, visibility, provider, project and explicit date filters
apply to title matches too.
Current-title matches prioritize session discovery. When the transcript matches
too, its snippet, source locator and lexical score remain authoritative.

An unknown Helm run may prepare a local Resume command when its machine is
online and its retained continuation contract is valid. Unknown is not ended:
native Resume still revalidates the exact provider state and atomically refuses
a live execution owner before admitting a replacement.

Local health reports Python-package, installed-native and running-engine
identities separately. Restart attribution compares the installed native engine
with the running engine, not the Python package. Stale provider-route receipts
are unknown evidence; fresh failures remain failures.
