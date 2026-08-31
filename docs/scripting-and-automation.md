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

### Option A: Ingest and Log to Longhouse, but Keep Home Timeline Clean (Recommended)

If you want Longhouse to store, index, and make your automated background runs searchable via full-text search and `recall`, tag the execution as automation:

```bash
# Tag the run as automation so it is archived but stays off your primary home timeline
LONGHOUSE_LAUNCH_ACTOR=automation LONGHOUSE_ORIGIN_KIND=hatch_automation my_script.sh
```

Or when calling the Longhouse Machine API directly:

```http
POST /api/agents/sessions
{
  "launch_actor": "automation",
  "launch_surface": "ci",
  "project": "my-project",
  "environment": "test"
}
```

* **Outcome:** The full transcript is permanently logged and indexed in Longhouse, but excluded from the default interactive timeline.

---

### Option B: Ephemeral Execution (Do Not Save History to Disk)

If your automated script is disposable and should not save any history files to disk:

* **Claude Code:** Pass `--no-session-persistence`:
  ```bash
  claude --no-session-persistence -p "check build status"
  ```
* **Codex CLI:** Set an ephemeral scratch directory:
  ```bash
  CODEX_HOME=$(mktemp -d) codex exec "check build status"
  ```
* **OpenCode:** Set a temporary data directory:
  ```bash
  XDG_DATA_HOME=$(mktemp -d) opencode run "check build status"
  ```

---

## 3. Curating Existing Sessions

If an automated script or experimental command was run without flags and appears on your home timeline, you can curate it manually at any time:

* Click **"..."** on the session card $\rightarrow$ **"Hide from timeline"**.
* This sets `user_hidden_from_timeline=1`. The transcript remains 100% intact, permanently stored, and searchable, but is removed from your home feed.
