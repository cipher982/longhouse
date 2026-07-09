# Demo Marketing Data

Status: Active

Marketing captures must demonstrate the product's actual session states. They
are product evidence, not a generic transcript fixture.

## First-Viewport Contract

The default timeline capture contains ten sessions. Its first viewport must
make these states legible without opening a row:

| Count | Product state | Expected row presentation |
|---:|---|---|
| 4 | Helm / Console with a fresh attached control path | `Live control` plus a real runtime phase such as `Running Bash`, `Thinking`, or `Idle` |
| 1 | Managed session whose host can be reattached | `Reattach`; it must not pretend to be live |
| 1 | Shadow / observe-only session | `Observe only` with a fresh activity signal |
| 4 | Historical imported sessions | `Search only`; these are the only rows allowed to have `No live signal` |

The live-managed rows must include **Claude, Codex, Cursor, and OpenCode**.
Antigravity supplies the observe-only row. Session titles, projects, machines,
branches, provider icons, timestamps, and runtime phases must all agree.

## Seed Truth

Demo control states are seeded through the same kernel records production
uses: `SessionThread`, `SessionRun`, `SessionConnection`, and
`SessionRuntimeState`. Do not fake `session.capabilities` in a browser route.

- Attached `spawned_control` connection with a fresh health lease → `Live control`.
- Detached `spawned_control` connection → `Reattach`.
- Attached `observe_only` tail connection → `Observe only`.
- No managed connection → `Search only`.

The capture process disables LLM title generation, so all public titles and
summaries come from the deterministic demo presentation map.
