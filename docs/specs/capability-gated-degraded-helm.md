# Capability-gated Degraded Helm

Status: Draft (Terra-refined) → implement Cursor proving ground
Date: 2026-07-13
Product sentence: **Helm always gives you the real provider terminal; Longhouse adds durability and remote control only when it can prove them live.**

## Product decision (locked)

**Posture:** Capability-gated Degraded Helm.

- Local provider TUI is authoritative and must start for a valid local Helm launch.
- The session remains **Helm** (managed launch ownership), not Shadow.
- Remote/durable capabilities are independently gated by live proof.
- Console stays hard-managed (Longhouse is the only UI).
- Shadow stays unmanaged/observe-only and never becomes Helm by silence.

### Non-negotiable Helm guarantees

1. A valid local launch always reaches the unchanged provider TUI.
2. Longhouse remote/catalog failure never strands the user without local interaction.
3. The session has one stable local identity that later convergence must reuse.
4. Degradation is explicit in the terminal (and later UI).
5. Remote send/interrupt/steer are enabled only from current capability proof.
6. Recovery upgrades the same session identity — no replacement session.

### Hard-fail vs soft-fail

**Still hard-fail (local prerequisites — these are not “Longhouse is sick”):**
- Non-interactive terminal when Helm requires TTY
- Missing provider binary / inability to establish local PTY
- Missing Longhouse URL/token configuration (machine not enrolled)
- Local machine-name / URL contract mismatches that would misroute control

**Soft-fail / async (remote plane):**
- Runtime Host unreachable / timeout / 5xx
- catalogd / managed-local registration rejects
- Timeline/ingest unavailable after launch

### What must never be silently claimed

- Durability before persistence is confirmed
- Live remote control from launch success or local socket bind alone
- Full Helm health when only the local TUI is up
- Unmanaged/Shadow fallback disguised as Helm
- A second session id created during recovery

## Mode boundaries

| Mode | Launch gate | UI truth |
|---|---|---|
| Helm | Local TUI must start; remote plane optional | Capabilities from live proof |
| Console | Control path + initial visibility required | Hard fail if Longhouse cannot be the UI |
| Shadow | No Longhouse launch ownership | Observe-only; never implies control |

## Cursor proving-ground lifecycle (authoritative)

Registration must **not** gate the TUI — including hanging on a long HTTP timeout.

```text
1. Validate local prerequisites (TTY, cursor-agent on PATH, enrolled URL+token, local contract preflight)
2. Mint stable session_id locally (UUID v4)
3. Bind local control socket + write local state:
     registration=pending|degraded, same session_id
4. Print launch panel with steerable=False until host registration succeeds
   (local socket bind is NOT remote proof)
5. Start provider TUI immediately
6. Background registration thread (bounded retries):
     POST /api/sessions/managed-local/this-device with client session_id
     on success: registration=registered on state file (same id)
     on exhaustion: registration=degraded + last_error; leave warning in scrollback if still useful
7. Out of scope this PR: mid-session panel upgrade, Runtime Host capability projector redesign
```

### Identity / reconciliation

- Client mints `session_id` once at launch.
- API accepts optional `session_id` and materializes that id (idempotent).
- Background retries always reuse the same id.
- If registration never succeeds: local Helm session still ran; no remote durability/control; no alternate id invented later.
- Terminal/runtime events posted best-effort; failure does not kill the TUI.

### Capability claims (Cursor proving ground)

| Claim | When true in this PR |
|---|---|
| Local TUI running | After pty.fork child starts |
| Local control socket up | After bind (engine can attach locally) |
| Host registration | After successful managed-local POST |
| Remote steer from web/iOS | Requires host registration **and** engine lease observation (existing path); do **not** claim in launch panel until registration succeeds |

Launch panel uses `steerable=False` until registration succeeds **before** the panel is printed. If registration is still in-flight when the panel prints (because TUI starts first), default to `steerable=False` / “Watch on your timeline” / explicit “registering with Longhouse…” warning. Do not print “Steer from anywhere” on socket bind alone.

Optional short race: start background register immediately after mint, wait up to ~300ms for a fast success before printing the panel; never block the TUI on the full HTTP timeout.

### Explicit non-goals (this PR)

- Claude/Codex/OpenCode/Antigravity launchers
- Console policy changes
- Full remote capability state machine / stale-proof downgrade across web/iOS
- Offline remote-command queuing
- Redesigning catalog persistence internals
- Making transcript ingest part of the capability gate

## Tests

1. Cursor: when registration HTTP fails/times out, launcher still proceeds to socket bind + would fork (mock provider bin / stop before fork if needed).
2. API: client-minted `session_id` accepted and returned unchanged.
3. Launch panel / registration helper: steerable false while registration pending/failed.
4. Existing Cursor Helm launcher unit tests still pass.
5. Hard-fail paths unchanged (missing binary, non-interactive).

## Terra must-fixes (locked before implement)

1. **Registration retry idempotency:** catalog `session.launch.local.create.v2` replay for an existing `managed-local-{session_id}` must succeed when identity fields match, **without** requiring identical `started_at` / `expires_at`. Lost HTTP responses + background retries must return the original launch, not conflict.

2. **Shutdown reconciliation:** background registration is cancelled when the Helm process is exiting. If a register request already committed after exit, immediately best-effort terminalize that session; never leave a falsely-live host session. Do not block process exit on the full HTTP timeout.

3. **Readiness before live lease:** engine Cursor Helm scanner must treat `ready=true` (and live launcher pid + socket) as the live signal. Launcher sets `ready=true` only after the provider child is running. Socket may exist earlier; it must not imply remote steer.

4. **Honest terminal copy:** do not say “Watch on your timeline” or “thread saved” when registration never succeeded. Use registering / local-only / non-durable exit copy.

5. **Tests** cover: idempotent replay with new timestamps; registration aborted/terminalized after provider exit; no live observation when `ready=false`; soft remote failure still starts TUI path.

- Host unreachable / catalog reject / delayed registration: Cursor TUI still starts.
- No false “Steer from anywhere” before host registration.
- One local session_id end-to-end; successful register uses that id.
- Mode remains Helm (managed local state + control socket), never silent Shadow.

## Implementation sequence

1. Land this spec.
2. API optional `session_id` + lite tests.
3. Cursor launcher: mint → bind/state → background register → TUI; panel steerable gated; tests.
4. DeepSeek check-ins between commits; final Terra review; push.
