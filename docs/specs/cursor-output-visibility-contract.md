# Cursor Output Visibility Contract

**Status:** Receipt-backed managed projection implemented and regression-tested;
terminal-presentation promotion remains future work

**Owner:** Longhouse

**Last updated:** 2026-07-21

## Decision

Longhouse must not infer what Cursor presented to the user from adjacency or
ordering in `store.db` or the provider JSONL. Cursor can persist several
assistant artifacts for one reconnecting request while its TUI presents only
one response. Those artifacts remain raw forensic evidence; they are not, by
themselves, user-visible transcript events.

The integration has three separate observation domains:

1. **Provider artifacts** — everything Cursor persisted in `store.db` and its
   JSONL. This is the lossless archive and may include retries, partial work,
   duplicate candidates, and malformed output.
2. **Provider commit receipts** — `afterAgentResponse`, terminal turn status,
   stream-json result records, and other upstream events that explicitly
   declare a completed semantic response.
3. **Terminal presentation** — bytes sent to the PTY and the rendered terminal
   frames produced from those bytes. This answers what the interactive TUI
   presented, including output Cursor showed before a failed turn.

No one domain is canonical for all three questions. Raw preservation and
user-facing projection must remain separate.

## Incident Findings

The original managed session and two retained/live canaries establish the same
failure shape. A fresh reproduction used stock Cursor
`2026.07.17-3e2a980`, model `cursor-grok-4.5-high`, on 2026-07-21.

| Surface | Healthy Codex lane | Failed Grok lane |
| --- | --- | --- |
| TUI | One response | One response, then `WritableIterable is closed` |
| JSONL | One assistant artifact per turn | Four assistant artifacts per turn; some retry content diverged or was malformed |
| Hooks | `afterAgentResponse` plus `stop(completed)` | Four retry/thought waves, no `afterAgentResponse`, `stop(error)` |
| Final VT frame | One response | One response and the error |
| Process | Remains usable | Returns to follow-up prompt after the error |

The current build passed the full stock-PTY Gate 0 suite on
`gpt-5.3-codex-low`: create/resume identity, follow-up send, native resume,
post-cancel recovery, and allow/deny/ask permission paths. The Grok lane failed
twice in the focused interactive probe with the four-attempt reconnect cadence.

Additional facts:

- `stop(completed)` and `afterAgentResponse` can arrive in either order on a
  successful turn. Correlation must use stable identity such as generation ID,
  not arrival order.
- A missing `afterAgentResponse` does not prove the TUI showed no response.
- A response present in a final rendered frame does not identify which of
  several byte-identical provider artifacts produced it.
- A raw PTY substring is not enough to prove presentation because TUIs redraw.
  The byte stream must be replayed through a compatible terminal emulator.
- The existing Gate 0 terminal artifact was sufficient to reconstruct the
  final 40x132 screen and mechanically confirm one displayed response.

## Rejected Approach

“Keep the first consecutive text-only assistant response” is rejected.

It destroys distinctions between retries, legitimate multi-message turns,
tool-mediated turns, and future provider behavior. It also has no evidence for
choosing the first artifact: in the reproduced failure, retry artifacts could
be duplicated, divergent, or malformed. The experimental branch containing
that heuristic must not be merged or deployed.

## Evidence Record

Each managed interactive provider turn should eventually produce a visibility
record alongside—not inside—the raw provider transcript:

```json
{
  "provider": "cursor",
  "provider_version": "2026.07.17-3e2a980",
  "provider_session_id": "...",
  "turn_generation_id": "...",
  "input": {
    "accepted_at": "...",
    "prompt_sha256": "..."
  },
  "provider_artifacts": [
    {
      "source": "cursor_jsonl",
      "source_index": 6,
      "content_sha256": "...",
      "first_observed_at": "..."
    }
  ],
  "commit_receipts": [
    {
      "source": "afterAgentResponse",
      "content_sha256": "...",
      "observed_at": "..."
    }
  ],
  "terminal": {
    "raw_byte_log": "raw/terminal.bin",
    "timed_chunks": "raw/terminal-chunks.jsonl",
    "frames": "terminal/frames.jsonl",
    "dimensions": {"columns": 132, "lines": 40}
  },
  "terminal_status": {
    "source": "cursor_jsonl",
    "status": "error",
    "error": "WritableIterable is closed"
  },
  "correlations": [
    {
      "content_sha256": "...",
      "artifact_count": 4,
      "commit_receipt": false,
      "rendered_frame_occurrences": 1,
      "binding": "ambiguous"
    }
  ]
}
```

Every field is an observation or mechanical correlation. The record must not
contain a hidden `include_in_transcript` classifier.

### Terminal capture requirements

The Helm launcher already owns the PTY master, so it is the correct capture
point. It should tee, without altering forwarding behavior:

- exact raw bytes;
- timestamped chunks with byte offsets;
- initial dimensions and every resize event;
- periodic/damage-driven rendered frames from a VT-compatible emulator;
- process exit, signal, and close timestamps.

Final-frame replay is sufficient for the current fixture but not the complete
contract. Content may be presented and later overwritten, so timed intermediate
frames are required before terminal presentation can be a durable semantic
source.

## User-Facing Projection

Managed Cursor sessions now use the hook stream as a fail-closed semantic
receipt:

- keep all Cursor storage-v2 blobs in the raw archive;
- require one unique ordered alignment between the complete hook prompt
  sequence and store turns;
- wait while the newest hook turn is unsettled, including the valid
  `stop(completed)`-before-`afterAgentResponse` race;
- project assistant prose only when the `afterAgentResponse.text` receipt has
  exactly one ordered decomposition across the turn's store text blocks;
- retain tool calls/results in their original store order;
- do not choose the first or last artifact when the receipt match is missing or
  ambiguous;
- treat a missing hook file on a managed binding as incomplete evidence, not as
  permission to expose every store artifact;
- represent a failed interactive turn as failed and expose its raw provider
  artifacts in forensic mode;
- where terminal capture exists, expose terminal replay/frame evidence as a
  separate presentation surface rather than pretending it is a committed
  assistant message.

The renderer revision is `cursor-store-render-v3-receipts`. Upgrading an
already-captured Cursor source rotates to a replacement epoch and replays raw
records from ordinal zero, so previously published v2 render objects are
retired rather than left beside corrected output. An obsolete, unattempted
pending v2 envelope is rebuilt before shipping; an attempted envelope remains
an exact-retry authority until the host receipts it.

This is deliberately conservative. It prevents four uncommitted retries from
appearing as four agent replies without deleting them, and it does not claim
that a displayed-but-uncommitted response was never shown. Terminal evidence
is still required before such displayed-but-uncommitted text can be promoted
to an ordinary assistant message.

Promotion of terminal-presented text into a normal assistant message requires
one of:

1. an upstream Cursor event that binds the displayed response to a stable
   message/content ID; or
2. a tested provider-adapter rule based on timed terminal frames and semantic
   TUI regions, with unknown layouts producing an explicit ambiguity instead
   of a guessed message.

## Harness Integration

This belongs in the universal agent harness, not a permanent parallel Cursor
test family. Cursor owns the adapter mechanics; the scenario vocabulary is
cross-provider.

Add these universal observations/actions:

- `output_commit_receipt`
- `terminal_presentation_capture`
- `artifact_presentation_binding`
- `failed_turn_diagnostics`

Add these scenarios:

| Scenario | Assertion |
| --- | --- |
| `successful_output_visibility` | One committed response correlates with the provider artifact and rendered terminal frame; hook order may vary. |
| `failed_retry_visibility` | All retry artifacts survive; terminal frames are retained; ambiguous artifacts do not become multiple transcript replies. |
| `divergent_retry_artifacts` | Distinct retry contents remain distinct and no ordinal winner is guessed. |
| `terminal_redraw_replay` | Intermediate and final frames reflect overwrite/clear/resize behavior, not substring presence. |
| `tool_turn_visibility` | Tool calls/results and later prose remain ordered without a text-only adjacency rule. |
| `cancel_then_recover` | Interrupted output is retained and the next completed turn binds independently. |
| `resume_visibility_continuity` | Reattach keeps provider identity while starting a new capture epoch. |
| `missing_hook_and_store_lag` | Late or absent surfaces become explicit incomplete evidence, not fabricated success. |

Run the matrix across:

- stock interactive TUI and exact `longhouse cursor` launch;
- supported one-shot `stream-json` as a control lane;
- healthy and failing model lanes;
- success, reconnect failure, cancel, permission, tool, steer, resume, process
  crash, and machine-agent restart;
- default transport and isolated HTTP/1 configuration where supported;
- current candidate and accepted provider versions.

Each live run must retain the universal evidence package: raw terminal,
timestamped chunks, hooks, provider JSONL, store snapshot, process metadata,
normalized observations, Longhouse ingest/projection, and assertion results.

## Terminal-Presentation Promotion Gates

No promotion of terminal-only, uncommitted text into the ordinary transcript
is eligible until all of these hold:

1. The retained failed canary and current Grok reproduction replay with the
   expected one-presented/four-artifact ambiguity.
2. Healthy turns across at least two model lanes bind one commit receipt, one
   provider artifact, and one terminal presentation without relying on hook
   order.
3. Tool, cancel, permission, resume, and multi-turn fixtures prove that no
   adjacency rule drops legitimate events.
4. Resize and redraw fixtures prove frame reconstruction from timestamped PTY
   chunks.
5. Parser upgrades can replay old raw evidence into a new render generation;
   merely bumping a renderer revision after the source cursor advanced is not
   sufficient.
6. Unknown Cursor layouts/events remain raw and surface a yellow/ambiguous
   result rather than silently changing the transcript.
7. The universal release proof records the provider version, adapter version,
   fixture version, evidence completeness, and old/new baseline diff.

## Implemented Receipt Projection and Investigation Tooling

The engine implementation lives in `engine/src/cursor_visibility.rs` and the
Cursor storage-v2 renderer. Regression coverage proves:

- the retained four-artifact/error shape remains fully raw but renders no
  fabricated assistant replies;
- successful hook receipts decompose exactly across multiple progress and
  final text blocks while tool events retain their position;
- duplicate hook rows and reversed successful terminal-hook order are safe;
- missing and ambiguous receipt matches fail closed;
- stale parser revisions replay through a replacement source epoch;
- stale unattempted pending renders are rebuilt before shipping.

Read-only validation against the 2026-07-20 retained healthy Cursor store found
two completed turns whose hook receipt exactly equaled the concatenation of
four and three store text blocks respectively. Its intervening error turn had
five store text blocks and no receipt. The 2026-07-21 incident store had four
assistant retry blobs, one submitted prompt, `stop(error)`, and no response
receipt.

`server/zerg/qa/cursor_visibility_evidence.py` and
`scripts/qa/cursor-visibility-evidence.py` replay a terminal byte stream,
preserve every JSONL assistant artifact, correlate hook response digests, and
report ambiguity. The QA tool does not modify the renderer; the engine
integration above owns the production receipt projection.

The first tests cover:

- success when `stop` arrives before `afterAgentResponse`;
- four failed retry artifacts correlated to one rendered response;
- terminal redraw semantics.

Next implementation work should add timestamped PTY chunk capture and immutable
store/JSONL snapshots to Gate 0, then migrate the visibility scenarios into the
universal Cursor adapter. Those artifacts gate terminal-only promotion, not the
receipt-backed correction shipped here.
