# macOS Menu Bar State Model

Status: implementation target for launch.

## Product job

The Longhouse menu bar is the fastest truthful answer to two questions:

1. What agent sessions are alive on this Mac?
2. Does the user need to do anything?

It is not a general telemetry dashboard and it is not a narrator. Current
session and product promises outrank historical maintenance.

## Independent facts

The panel must preserve these axes instead of collapsing them into one health
scalar:

- **Sessions:** working, needs user, ready, background, or unavailable.
- **Control:** connected, limited, or unavailable.
- **Durable upload:** clear, pending within budget, blocked, or unknown.
- **Transport:** connected, retry scheduled, or unavailable.
- **Freshness:** fresh, updating after wake, stale, or unknown.
- **Archive projection:** idle, scanning, uploading, pending, paused, or failed.

Archive projection is historical maintenance. Scanning or draining never makes
current product health degraded, never badges the status item, and never
displaces a session headline. Dead historical ranges are inspectable amber,
not proof that current sessions are unsafe.

## Attention and promotion

The presentation reducer applies one deterministic priority order:

1. **Repair now / red:** a current promise is broken and a concrete repair
   exists (local agent stopped with retained work, immutable source blocked,
   promised control path lost).
2. **Needs user / blue:** a session explicitly awaits input or permission.
3. **Inspect / amber:** persistent limitation or cleanup that does not threaten
   current durable work.
4. **Unknown / gray:** evidence is stale or unavailable after the wake grace
   period.
5. **Normal / unbadged:** fresh facts, including active work, young durable
   pending records, transient retry, and archive scanning.

Durability failure outranks session input because it can affect multiple
sessions and requires restoring a product invariant. Ordinary work never adds
an activity badge; the sessions themselves provide that signal inside the
panel.

## Header contract

The header is reduced from current facts, never copied from generic backend
prose.

- `Durable upload blocked for 1 source`
- `1 session needs you`
- `Remote control unavailable for 1 session`
- `3 sessions active`
- `No sessions running`
- `Updating status after wake…`
- `Status unavailable · last local update 3m ago`

The subline contains counts and freshness, for example
`2 working · 1 ready · updated 4s ago`. Remove `WATCHING` and move build identity
to details/overflow.

## Panel structure

1. Header and refresh.
2. `Needs you` sessions, when present.
3. `Working` sessions.
4. `Ready and background` sessions.
5. `Other agent processes`, when present.
6. `System facts`: local agent, remote control, durable upload, freshness.
7. `Background activity`, only when maintenance exists.
8. One contextual primary action plus overflow for Logs, Doctor, Copy JSON,
   version, and Quit.

Session rows use an honest fallback such as `Codex session in longhouse`, never
`Naming session…`. UI presence and control availability remain separate facts.

## Copy rules

- State observations, not intent: `Retry scheduled in 18s`, not `retrying
  quietly`.
- Name the plane: `Durable upload`, `Remote control`, `Archive projection`.
- Include scope and age where relevant.
- Mark last-known evidence explicitly after sleep or refresh failure.
- Never claim recovery without observed progress.
- Do not use `WATCHING`, `quietly`, `should`, or generic `What's happening`
  prose as status.

## Required fixtures

- all clear with no sessions
- active working/ready sessions
- session needs permission
- healthy background Console session
- control unavailable
- orphan bridge cleanup
- archive scanning while live work is healthy
- archive pending while idle
- archive dead letters
- young immutable pending work
- immutable source conflict
- transient Wi-Fi retry
- wake recovery and stale cached snapshot
- stopped local agent with retained work
- setup required
- unknown session phase

## Acceptance criteria

- Benign archive work never changes the icon, badge, headline, or session
  placement.
- No generic count combines spool, immutable outbox, hook outbox, and archive
  projection.
- Every promoted warning has a scope, provenance, age, and relevant action.
- Background presence is not degraded control.
- Unknown row-level phase does not become a global red failure.
- Header, badge, and panel accent come from one presentation reducer.
- Current local facts render within 50 ms; refresh does not depend on network.
- Every combined state above has a fixture, reducer assertion, and inspected
  full-frame PNG.

## Implementation phases

1. Add the presentation reducer and lock its state matrix with Swift tests.
2. Stop active archive maintenance from degrading backend local health.
3. Recompose the panel around session groups, system facts, and background
   activity; remove legacy watching/queue presentation.
4. Expand fixtures, render all states, inspect full frames, and tune copy and
   spacing.
5. Run final design review, ship, and dogfood the installed app.
