# Near-Instant AI Session Titles

Status: Accepted
Last updated: 2026-07-16

## Decision

Every eligible human-started session gets a short, stable, AI-generated title.
The first durable user message starts generation immediately. Until the model
returns, clients render a sanitized prompt-derived fallback so the row is never
blank or blocked on “Naming session…”.

The fallback is presentation only. It must never be persisted or reported as a
completed AI title.

## Measured model lane and SLA

The `session_title` use case uses OpenRouter's
`google/gemini-3.1-flash-lite:nitro`. A live 2026-07-16 benchmark over seven
representative prompts and two rounds completed 14/14 calls with a 463 ms
median and 723 ms p90.

Targets are measured from the first durable user-message commit:

- prompt fallback visible in the same response/commit;
- AI title ready p50 under 750 ms;
- AI title ready p90 under 1.5 s;
- no eligible title debt older than 30 s without a recorded retry reason.

Provider latency is not allowed to delay transcript ingest or control traffic.

## Contract

```text
first durable meaningful user message
  -> commit prompt preview immediately
  -> expose sanitized prompt fallback with title_state=pending
  -> call the session_title model outside the catalog transaction
  -> compare-and-set anchor_title once
  -> publish session/timeline invalidation
  -> expose title_state=ready, title_source=ai
```

Failures record a durable retry obligation. A process restart, missed in-memory
task, timeout, or provider outage must converge without another transcript
event.

## Ownership

Catalogd/storage-v2 owns title state because it owns the durable first user
message. `anchor_title` is the write-once AI title. `summary_title` remains a
compatibility/search field and cannot satisfy the title obligation.

The Runtime Host owns model calls and retries; catalogd exposes bounded claim,
completion, and failure operations. The Machine Agent may cache the immediate
prompt fallback, but it cannot label that fallback `ready`.

## Client behavior

All clients render the server-resolved `timeline_title`:

- `pending` or `generating`: prompt fallback;
- `ready`: AI `anchor_title`;
- `degraded`: prompt fallback while durable retry remains active;
- `awaiting_input`: project/provider context before user intent exists.

Clients must not replace a usable fallback with “Naming session…”, and must not
infer `ready` from a non-empty prompt-derived string.

## Invariants

- AI judgment produces the title; deterministic code handles transport,
  sanitization, timeout, compare-and-set, retries, and display fallback.
- Only the first durable non-warmup user message is model input.
- Assistant/tool output and later prompts cannot rename the session.
- The model call runs outside catalogd and outside ingest transactions.
- `anchor_title IS NULL` on an eligible session is title debt.
- Test, e2e, provider-proof, and autonomous no-user sessions are exempt.
- Web, iOS, widgets, CLI, and macOS agree on title text and provenance.

## Acceptance tests

- First-user commit schedules generation without delaying its receipt.
- Prompt fallback is immediate but reports `pending`/`prompt`.
- Successful generation compare-and-sets `anchor_title` and reports
  `ready`/`ai`.
- Later ingest cannot overwrite the AI title.
- Failure records retry state and the repair loop later converges.
- Storage-v2, iOS, and menu bar never treat prompt truncation as an AI title.
