# Claude Terminal Runtime Outbox

Managed Claude graceful exit has two facts that the process scanner cannot
recover later:

- the provider wrapper observed the exit directly
- `exit_code=0` means `session_ended`, not generic `process_gone`

The wrapper must therefore hand this terminal event to Longhouse durably before
it tries any network fast path.

## Design

On provider exit, the Claude wrapper builds one `terminal_signal` runtime event.
It writes that event as a JSON file under:

```text
~/.longhouse/agent/runtime-events-outbox/
```

The wrapper then makes the existing short direct POST to
`/api/agents/runtime/events/batch`. If the direct POST succeeds, the wrapper
removes the queued file. If the direct POST fails or times out, the wrapper
exits promptly and leaves the file for the Machine Agent.

The Machine Agent drains the runtime-events outbox on its normal outbox tick:

1. Read complete `.json` files, skipping dot-prefixed temp files.
2. POST files in batches to `/api/agents/runtime/events/batch`.
3. Delete files after a 2xx response.
4. Leave files on network or server failure.
5. Delete malformed JSON files because they cannot be retried usefully.

Presence outbox files remain ephemeral and coalesced. Runtime terminal events
are durable and are not coalesced.

## Dedupe

The queued file stores the exact runtime event the wrapper would POST directly,
including its `source` and `dedupe_key`. If the direct POST succeeds but the
wrapper exits before deleting the queued file, the Machine Agent may replay the
same event. Server-side runtime ingest is idempotent for the same
`source + dedupe_key`, so replay is safe.

## Boundaries

The outbox lives in `~/.longhouse/agent/` because it is Longhouse-owned runtime
state. It must not live under `~/.claude/managed-local/`, which is provider
integration state.
