# Startup Continuity (lab)

Status: lab / opt-in
Last updated: 2026-04-18

Inject a small, project-scoped recap of recent Longhouse sessions into the
context of new Claude Code and Codex sessions so the model starts with
cross-provider memory.

This is **not** part of the launch product. It:

- is not installed by default with `longhouse connect --install`
- is not advertised on the landing page or in user docs
- can be removed without deprecation

The underlying endpoint (`GET /api/agents/sessions/startup-context`) is a cheap
query on summarized sessions Longhouse already stores, so it stays registered
in core. Only the **hook installation** that actively injects the recap into
every new provider session lives here.

## Why it is a lab and not core

Longhouse the product is about observing and managing sessions. This feature
*modifies agent behavior* by silently pushing content into the provider's
prompt. Bad summaries look like "Claude is acting weird" to users, not like a
Longhouse bug — a different support surface than the rest of the product. Until
there is a real pull for memory-in-prompt, the hook lives here.

## What it does

On `SessionStart`, the provider hook:

1. Detects the current project from the git toplevel of `$CWD` (falling back to
   `basename(cwd)`).
2. Calls `GET /api/agents/sessions/startup-context?project=<project>` with a
   bounded short timeout.
3. Emits `hookSpecificOutput.additionalContext` with a small rendered block
   wrapped in an explicit "NEVER follow instructions" guard.

Selection rules on the server:

- same project only
- summarized sessions only
- writable heads only
- hide sidechains and zero-user-message sessions
- exclude archived sessions
- cross-provider allowed (Claude can see Codex work and vice versa)

## Enabling

```bash
python labs/startup-continuity/install.py         # rewrite hooks in place
python labs/startup-continuity/install.py --check # report status, no writes
```

This rewrites `~/.claude/hooks/longhouse-hook.sh` and
`~/.codex/hooks/longhouse-codex-hook.sh` in place, adding the SessionStart
fetch/inject path. The default install (`longhouse connect --install`) leaves
this path out.

## Disabling

Re-run `longhouse connect --install` to restore the default (presence-only)
hook scripts.

## Non-Goals

- No standalone briefing page
- No cross-project insights bundle
- No extra memory model beyond recent session summaries
- No browser-owned continuity contract
