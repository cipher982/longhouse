# Scripting and background automation

Longhouse timelines are for the sessions you drove yourself. Cron jobs, watchdogs, and CI
runs still deserve an archive — they just don't belong in the feed you scroll. This is how to
get both.

## What Longhouse watches

The Machine Agent discovers unmanaged (Shadow) sessions by watching each provider's own
history directory:

- Claude Code — `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/`)
- Codex CLI — `~/.codex/sessions/`
- Antigravity CLI — `~/.gemini/antigravity-cli/brain/`, `~/.gemini/antigravity/brain/`, `~/.gemini/tmp/`
- OpenCode — `~/.local/share/opencode/`
- Cursor — `~/.cursor/chats/`, `~/.cursor/projects/`, `$XDG_CONFIG_HOME/cursor/chats/`

Anything a provider writes there is your interactive history, the AI-coding equivalent of
`~/.bash_history`, and it lands on your timeline. A background script that shells out to
`claude` writes to the same place, which is why it shows up next to work you actually did.

## Keep automation archived but off the timeline

Tag the run as automation. Longhouse still ingests, indexes, and full-text-searches the whole
transcript; it just stays out of the default feed.

For a raw provider CLI, set the provenance in the environment:

```bash
LONGHOUSE_ORIGIN_KIND=hatch_automation \
LONGHOUSE_LAUNCH_ACTOR=automation \
LONGHOUSE_LAUNCH_SURFACE=ci \
  ./nightly-triage.sh
```

The engine reads those three variables when it ships the session, so every session the script
starts inherits the provenance without touching the script itself.

When you create the session through the machine API, declare it on the request instead:

```http
POST /api/agents/sessions
{
  "launch_actor": "automation",
  "launch_surface": "ci",
  "project": "my-project"
}
```

`launch_surface` must be one of `test`, `e2e`, `product-e2e`, `qa`, `ci`, `canary`, or
`factory_assurance`. That declaration is what hides the session — a project name that merely
looks internal will not. Read hidden sessions back with `include_test=true`.

## Skip the archive entirely

If the run is disposable and should not write history to disk at all, point the provider at a
throwaway state root:

```bash
claude --no-session-persistence -p "check build status"
CODEX_HOME=$(mktemp -d) codex exec "check build status"
XDG_DATA_HOME=$(mktemp -d) opencode run "check build status"
```

Nothing is ingested, because nothing is written where the Machine Agent looks.

Antigravity has no equivalent switch — the agent resolves its brain directory from `$HOME`
with no override, so tag the run as automation instead.

## Curating after the fact

For the script you already ran without any of the above: open the session and press **Hide**
in the detail header. That sets `user_hidden_from_timeline` and drops it from your feed. The
transcript stays stored and searchable, and **Restore** puts it back.
