# Zerg TODO

Active work items tracked outside of GitHub issues. For agent-tracked TODOs see AGENTS.md.

---

## High Priority (Bugs / UX Issues)

### Parallel spawn_commis Interrupt Bug
`_execute_tools_parallel()` doesn't raise `FicheInterrupted`, so runs finish SUCCESS instead of WAITING when multiple commis are spawned. Commis results only surface on next user turn.

**Location:** `services/oikos_react_engine.py`
**Fix:** Return `interrupt_value` dict for barrier creation, or raise `FicheInterrupted` like sequential path.

### Telegram Webhook Handler
`webhook_url` config sets remote webhook on Telegram but no local handler exists. Users configure webhook, Telegram sends updates, nothing receives them.

**Options:**
1. Implement `WebhookChannel` router at `/webhooks/channels/{channel_id}`
2. Remove `webhook_url` from UI until supported

**Location:** `channels/plugins/telegram.py`, need new `routers/channels_webhooks.py`

---

## Medium Priority (Performance / Architecture)

### Prompt Cache Optimization
Current message layout busts cache by injecting dynamic content early:
```
[system_prompt] → [connector_status] → [memory] → [conversation] → [user_msg]
                        ↑                 ↑
                   CACHE BUST!       CACHE BUST!
```

**Optimal layout:**
```
[system_prompt] → [conversation] → [dynamic_context + user_msg]
     cacheable       cacheable            per-turn only
```

**Key principles (from research):**
- Static content at position 0 (tools, system prompt)
- Conversation history next (extends cacheable prefix)
- Dynamic content LAST (connector status, RAG, timestamps)
- Canonical JSON serialization (sorted keys, stable whitespace)
- Never remove tools - return "disabled" instead

**References:**
- OpenAI: platform.openai.com/docs/guides/prompt-caching
- Anthropic: docs.anthropic.com/docs/build-with-claude/prompt-caching
- Paper: "Don't Break the Cache" (Jan 2026) - arxiv.org/abs/2601.06007

**Location:** `managers/fiche_runner.py` lines 340-405

### Workspace Commis Tool Events
Workspace commis emit only `commis_started` and `commis_complete` - no tool events. The events exist in the hatch session JSONL but aren't extracted.

**Question:** Should we extract tool events post-hoc from session log for UI consistency?

**Trade-off:**
- Status quo: Accept reduced visibility for headless execution
- Post-hoc: Parse session log on completion, emit `commis_tool_*` events retroactively

**Location:** `services/commis_job_processor.py` workspace execution path

---

## Low Priority (Nice to Have)

### Sauron /sync Reschedule
`/sync` reloads manifest but APScheduler doesn't reschedule existing jobs. New/changed jobs won't run until Sauron restarts.

**Location:** `apps/sauron/sauron/main.py`

---

## Done (Recent)

- [x] Learnings review - compacted 33 → 11 (2026-01-30)
- [x] Sauron gotchas documented in README (2026-01-30)
- [x] Life Hub agent migration complete - Zerg owns agents DB (2026-01-28)
- [x] Single-tenant enforcement in agents API (2026-01-29)
