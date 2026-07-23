# Cursor Helm Stream Failure Analysis

**Status:** closed; no runtime change recommended
**Incident:** `51519fa8-57a9-4e05-85f5-b8c80c4bb9ea`, 2026-07-23

## Decision

The visible `WritableIterable is closed` error was a Cursor provider-stream
failure. Longhouse submitted each user prompt once and sent no interrupt or
terminate command. Cursor created several internal generations, displayed
duplicate completion text, and ended the turn with an error while leaving its
TUI usable.

An internet, VPN, or HTTP/2 interruption is possible, but the incident has no
associated connection, interface-change, or HTTP/2 error. Do not assign a
network root cause from the available evidence.

No Longhouse runtime change is justified by this one incident:

- do not replay prompts automatically;
- do not alter Cursor's network settings;
- do not replace or wrap Cursor's provider retry behavior;
- do not add PTY capture or a general failure framework;
- do not delay Helm startup to prove remote steering.

Retrying manually is the correct response to an isolated occurrence. Revisit
Longhouse failure presentation only if this becomes a repeated user problem.

## Evidence

All times are America/New_York.

| Time | Evidence |
| --- | --- |
| 13:39:31 | `lhcu` registered the session and launched stock Cursor `2026.07.20-8cc9c0b`. |
| 13:40:11 | The user submitted the `agent-traces` question once. |
| 13:43:44 | The user submitted `ok thanks, noteit` once. |
| 13:44:09–13:44:27 | Cursor emitted repeated completion/reasoning artifacts under different generation IDs. |
| 13:44:29 | Cursor emitted `stop(status=error)` and `turn_ended: WritableIterable is closed`. |
| 13:46:04 | Cursor emitted `sessionEnd`; the Helm wrapper posted `helm_exit` after the user exited. |

There were no hosted Longhouse interactions and no Longhouse send, interrupt,
or terminate commands. The error preceded the user's exit by about 95 seconds.
Cursor's provider JSONL, native store, and hook stream retained enough evidence
to diagnose the failure.

Longhouse already handles the important safety property: receipt-backed Cursor
projection retains raw retry artifacts but does not publish uncommitted retry
prose as several assistant messages.

## Incidental Machine Agent outage

The investigation also found that the local Machine Agent was not running
during this session. That did not cause Cursor's stream failure. It explains
why hook observations queued locally and the hosted session initially lacked a
transcript.

The Machine Agent restarted later, and its live and repair shipping lanes
recovered without errors.

The launch panel said `Steer from anywhere` after Runtime Host registration,
before the Machine Agent had published a session lease. This is optimistic
launch copy, not a useful incident fix. A definitive session lease cannot exist
until Cursor has started and the Machine Agent has observed it. Waiting for
that proof would add startup latency to every healthy launch for a rare degraded
case.

Decision:

- keep startup optimistic and fast;
- keep the existing conditional explanation that registration and the machine
  lease are required;
- let the web UI use the actual lease to enable or disable controls;
- add no launch-time lease wait or hedged `might become available` copy;
- consider a no-wait, known-offline warning only if this becomes a recurring
  product problem.

## Deferred ideas

These are not current work:

- A visible failed-turn marker in the hosted timeline. This crosses Cursor
  visibility projection and UI behavior; it is not needed for safe retention,
  and Cursor already displays the error locally.
- Timestamped PTY capture. Existing evidence was sufficient for this incident.
- A dedicated outage-recovery test. The existing repair lane recovered after
  restart; add a focused test only if recovery itself fails in a future case.

## External reports

Cursor users have reported the same error as a transient stream teardown and
other early stream-close failures. Cursor's usual guidance is to retry, then use
network diagnostics or its HTTP/1.1 setting only when failures repeat.

- `https://forum.cursor.com/t/cursor-cli-error-writableiterable-is-closed/165741`
- `https://forum.cursor.com/t/connection-failed-bug/155904`
- `https://forum.cursor.com/t/agent-cannot-reconnect-when-internet-switches/152241`

## Review history

Hatch Fable and Cursor Grok independently confirmed that Longhouse did not
resubmit the prompt, that the network cause was unproven, and that automatic
retry or Cursor-specific recovery machinery would be disproportionate. Their
initial launch-copy recommendation was reconsidered after weighing its limited
user value against startup latency and unnecessary uncertainty in the normal
path.

Related implementation context:

- `docs/specs/cursor-output-visibility-contract.md`
- `docs/specs/cursor-helm-launch-parity.md`
- `docs/specs/cursor-console-native-turns.md`
