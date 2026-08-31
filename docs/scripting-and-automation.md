# Scripting and Background Automation with Longhouse

Longhouse is mission control for your interactive agent sessions. This guide explains how Longhouse discovers sessions, and how to run background scripts, cron jobs, and CI workflows cleanly.

---

## 1. How Longhouse Discovers Sessions

By default, the Longhouse Machine Agent watches your local interactive provider directories:

* **Claude Code:** `~/.claude/projects/`
* **Codex CLI:** `~/.codex/sessions/`
* **OpenCode:** `~/.local/share/opencode/`
* **Cursor:** `~/.cursor/`

Longhouse treats these paths as your **interactive human history** (the AI-coding equivalent of `~/.bash_history`). Any native transcript discovered in these directories appears directly on your Longhouse timeline.

---

## 2. Running Background Scripts & Daemons

When running automated scripts, watchdogs, or cron jobs that invoke `claude`, `codex`, or `opencode`, you have two options depending on your goal:

### Option A: Tracked Automation (Log to Longhouse with Hidden Timeline Provenance)

If you want Longhouse to record, index, and make your automated runs searchable without cluttering your home timeline:

1. **Via Managed Launches or Hatch:** Run with `launch_actor="automation"` or `hatch` (which attaches automation provenance automatically).
2. **Via Canonical Machine API:**

```http
POST /api/agents/sessions
{
  "launch_actor": "automation",
  "launch_surface": "ci",
  "project": "my-project",
  "environment": "test"
}
```

* **Outcome:** The full transcript is permanently archived and searchable, but excluded from the default interactive timeline feed.

---

### Option B: Isolated State Roots for Raw CLI Scripts

When running bare provider CLIs in background scripts outside Longhouse's managed control path, isolate the provider's transcript directory from your interactive roots:

* **Claude Code:** Pass `--no-session-persistence` (keeps default login, skips writing session JSONL to `~/.claude/projects/`):
  ```bash
  claude --no-session-persistence -p "check build status"
  ```
* **Codex CLI:** Set a dedicated state directory:
  ```bash
  CODEX_HOME=$(mktemp -d) codex exec "check build status"
  ```
* **OpenCode:** Set a dedicated data directory:
  ```bash
  XDG_DATA_HOME=$(mktemp -d) opencode run "check build status"
  ```
---

## 3. Curating Existing Sessions

If an automated script or experimental command was run without flags and appears on your home timeline, you can curate it manually at any time:

* Click **"..."** on the session card $\rightarrow$ **"Hide from timeline"**.
* This sets `user_hidden_from_timeline=1`. The transcript remains 100% intact, permanently stored, and searchable, but is removed from your home feed.
