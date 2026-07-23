# Cursor Helm Stream Failure Analysis

**Status:** reviewed recommendation
**Incident:** `51519fa8-57a9-4e05-85f5-b8c80c4bb9ea`, 2026-07-23
**Scope:** Cursor Helm failure reporting and launch honesty

## Decision summary

The visible `WritableIterable is closed` error came from Cursor's provider
loop, not from Longhouse control. Longhouse did not submit the prompt more than
once and did not send an interrupt or terminate command. Cursor created several
internal response generations after the single user submission, displayed
duplicate completion text, then ended the turn with an error while leaving the
TUI usable.

The incident also exposed an independent Longhouse bug: the Machine Agent was
stopped before launch, but successful Runtime Host registration was enough for
the launch panel to say `Steer from anywhere`. The session therefore looked
remotely controllable even though no fresh machine lease existed. Hook events
queued locally and the hosted session received only its later terminal event;
its transcript never arrived during the incident.

The smallest useful response is:

1. Stop mapping successful registration to `Steer from anywhere` in the
   launch panel. The panel renders before a fresh lease can exist.
2. Surface Cursor's existing `stop(error)` evidence as one failed-turn status
   without ending the Helm session or inventing assistant messages.
3. Add one cheap reconciliation test for queued evidence after a Machine Agent
   outage.

Do not add automatic prompt replay, network switching, a parallel Cursor
runtime, or a general recovery subsystem.

## What happened

All times are America/New_York.

| Time | Evidence |
| --- | --- |
| 12:42:32 | Machine Agent last successful ship. |
| 12:42:51 | Machine Agent log ends without a panic or fatal error. Its launchd service was later absent. |
| 13:39:31 | `lhcu` created the Longhouse session and launched stock Cursor `2026.07.20-8cc9c0b`. |
| 13:40:11 | The user submitted the `agent-traces` question once. |
| 13:43:44 | The user submitted `ok thanks, noteit` once. |
| 13:44:09–13:44:27 | Cursor emitted repeated completion/reasoning artifacts under different generation IDs and noted that it had duplicated its response. |
| 13:44:29 | Cursor emitted `stop(status=error)` and wrote `turn_ended: WritableIterable is closed`. |
| 13:46:04 | Cursor emitted `sessionEnd`; the Helm wrapper posted `helm_exit` after the user exited. |

There were no hosted interactions and no Longhouse send, interrupt, or
terminate command for this session. The failure preceded session exit by about
95 seconds.

Local evidence retained:

- Cursor's provider JSONL, including the failed terminal record;
- the native Cursor store;
- Longhouse's hook-event stream;
- 55 queued Longhouse presence observations for this session.

Hosted evidence showed a terminalized session skeleton with no transcript or
turns. This is consistent with a stopped Machine Agent plus the wrapper's
independent registration and terminal-event HTTP paths.

## Likely cause

`WritableIterable is closed` is a Cursor transport symptom, not a precise root
cause. Cursor staff have associated similar reports with both client stream
teardown and an early server-side stream close. This incident contains no
associated `ConnectError`, network-disconnect, HTTP/2, or interface-change
evidence. Its shape matches Longhouse's retained Grok failure fixture: multiple
internal generation attempts, no `afterAgentResponse` commit receipt, a
displayed response, and `stop(error)`.

Conclusion: classify this as a Cursor provider-stream failure. Do not claim a
network root cause from the available evidence.

## What Longhouse already does correctly

- It launches the user's stock Cursor CLI and leaves Cursor responsible for
  provider execution and retry behavior.
- A provider turn failure does not automatically end the Helm session.
- Receipt-backed Cursor projection retains raw artifacts while suppressing
  uncommitted retry prose. The duplicate candidates do not become several
  ordinary assistant messages in Longhouse.
- The wrapper reports PTY closure and provider exit separately from turn
  state.
- Local hooks and provider storage remain useful evidence during a Machine
  Agent outage.

These behaviors should remain. In particular, Longhouse currently does not
replay the user's prompt and should not start doing so.

## Gaps

### 1. Registration is presented as control readiness

`cursor_helm._panel_capability_for_registration()` maps successful Runtime Host
registration directly to `steerable`. Registration proves that the hosted row
exists. It does not prove that the Machine Agent has observed the local socket,
published a fresh lease, or can execute a command.

This was the largest Longhouse failure in the incident because the product made
a promise it could not keep.

### 2. Failed turns are preserved but not explained

The hook stream records `stop(error)`, Cursor visibility retains `stop_status`,
and the provider JSONL records the error text. Receipt projection correctly
fails closed and existing Longhouse state already supports failed turns. The
missing piece is one user-visible result such as `Cursor turn failed; session is
still available` for a native Helm turn.

### 3. Recovery needs one focused proof

Hook observations safely accumulated in the outbox, but the session stayed
hidden and transcript-empty while the engine was down. Recovery should remain
a property of the existing outbox and source-reconciliation paths. This
incident does not justify a Cursor-specific recovery service or a new live
outage harness.

## Deferred evidence gap

The Helm wrapper owns the PTY master but currently forwards bytes without a
durable tee. When Cursor displays output and then fails without an
`afterAgentResponse` receipt, Longhouse can preserve provider artifacts but
cannot prove which text appeared in the TUI. The existing output-visibility
spec already identifies timestamped PTY capture as the evidence needed for
that distinction.

PTY capture is useful forensic evidence, but it is not required to fix launch
honesty or show a failed-turn status. The provider JSONL, hook stream, and
native store were sufficient to diagnose this incident. Keep capture deferred
until terminal-only recovery or repeated incidents create a concrete need.

## Minimal improvements

### P0: honest launch copy

The panel prints before the child has published `ready=true`, so a fresh lease
cannot exist at first paint. Do not add a launch-time lease waiter. Change the
initial capability mapping so it distinguishes only:

- **Registered / registering:** the hosted row exists; remote control goes live
  when the machine confirms it.
- **Local only:** registration failed; archive and remote control are currently
  unavailable.

The stock Cursor TUI should still launch in local-only mode. Print one direct
warning that archive and remote control are unavailable. Do not repair, restart,
or reconfigure the Machine Agent implicitly from the provider wrapper.

Reserve `Steer from anywhere` for later surfaces, such as the web UI, that can
observe the existing fresh-lease facts. Do not add another heartbeat,
readiness model, blocking check, or mid-TUI panel update.

### P1: failed-turn status

Project Cursor's existing `stop_status=error` evidence as one visible failed
turn while keeping session liveness independent:

- preserve all raw provider artifacts;
- publish no assistant prose without a unique commit receipt;
- show a small `Cursor turn failed; session still available` status, with the
  raw provider error when available;
- keep send/interrupt availability based on the fresh phase and lease, not on
  the previous turn result.

Reuse the receipt-backed Cursor visibility and existing failed-turn state. Do
not assume that the request-ID-oriented `mark_session_turn_failed` helper is the
right seam for native Helm turns; choose the smallest adapter into the existing
projection. Add no incident table, error hierarchy, or failure taxonomy.

### P2: verify ordinary recovery

Add one fixture-backed integration test that starts with queued hook/store
evidence and verifies that the existing outbox/source-reconciliation paths
converge the original session without duplicate user or assistant rows. Fix the
generic path if this fails. Escalate to a live Machine Agent stop/restart test
only if the cheap test passes but cannot explain observed recovery failures.

### Deferred: PTY evidence capture

Add timestamped PTY chunks only if terminal-only output is needed for real
user-facing recovery or repeated incident diagnosis. If that need arrives,
start with an exact byte tee plus dimensions and exit metadata. Do not build a
frame database, terminal search index, or semantic terminal parser until the
byte evidence proves those are necessary.

## Explicitly rejected

- Automatically replaying or resubmitting a failed prompt.
- Switching Cursor to HTTP/1.1 automatically.
- Retrying or replacing Cursor's internal provider stream.
- Treating `WritableIterable is closed` as proof of a network failure.
- Marking the whole Helm session ended when one provider turn fails.
- Publishing the first or last retry artifact as the assistant answer.
- Building a Longhouse-owned Cursor runtime or patching the provider.
- Adding a generic policy engine for provider errors.
- Blocking Cursor launch solely because Longhouse is temporarily unavailable.

## User guidance

For a one-off failure, retry the request or resume the Cursor conversation. If
the same conversation fails repeatedly after resume while a fresh conversation
works, treat the provider conversation as damaged and start a new one. If
failures repeat across conversations, use Cursor's network diagnostics and
test its documented HTTP/1.1 CLI setting. Longhouse should surface this advice,
not change the setting.

## Validation

The change is done when:

1. A registered session with no fresh machine lease never says `Steer from
   anywhere`.
2. The web UI presents normal Helm control when the existing facts show a fresh
   lease; the launch panel makes no premature claim.
3. A Cursor `stop(error)` produces one failed-turn status, no fabricated
   assistant message, and no session termination.
4. Queued evidence reconciles into the original bound session without duplicate
   turns.

## Independent review disposition

Hatch Fable and Cursor Grok independently reviewed this document, the related
specs, and the implementation. Both accepted the incident interpretation and
rejected a network conclusion or Longhouse retry system. Both reduced P0 to a
copy/mapping correction, P1 to wiring existing failure evidence into existing
projection, P2 to one cheap reconciliation test, and kept PTY capture deferred.

They differed only on the exact P1 helper. Fable suggested the existing failed
turn service; Grok noted that its request-ID contract may not fit native Helm
turns. This recommendation therefore preserves the existing state model but
leaves the adapter choice to implementation evidence.

## Related material

- `docs/specs/cursor-output-visibility-contract.md`
- `docs/specs/cursor-helm-launch-parity.md`
- `docs/specs/cursor-console-native-turns.md`
- Cursor forum: `https://forum.cursor.com/t/cursor-cli-error-writableiterable-is-closed/165741`
- Cursor forum: `https://forum.cursor.com/t/connection-failed-bug/155904`
- Cursor forum: `https://forum.cursor.com/t/agent-cannot-reconnect-when-internet-switches/152241`
