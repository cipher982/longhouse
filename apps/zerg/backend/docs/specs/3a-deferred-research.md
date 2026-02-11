# Phase 3a Deferred Items — Research Notes

**Date:** 2026-02-11
**Status:** Research complete, recommendations below

Two items were deferred from Phase 3a (Slim Oikos): the Oikos dispatch contract and infinite-thread context management via compaction. This doc evaluates both.

---

## 1. Current Dispatch State

Oikos dispatch today is implicit. The ReAct loop in `oikos_react_engine.py` calls the LLM with all bound tools, and the LLM decides what to do. There is no explicit routing layer — the model picks:

- **Direct response** — LLM returns text, no tool calls
- **Quick tool** — LLM calls a builtin tool (search, memory, email, etc.)
- **CLI delegation** — LLM calls `spawn_commis` or `spawn_workspace_commis`

Backend selection for commis is partially exposed via the `model` arg on `spawn_commis`, but the user cannot say "use Codex" in natural language and have Oikos map that to a backend. The prompt guidance still references legacy patterns.

Repo vs scratch delegation is partially implemented: `spawn_commis` accepts `git_repo` and creates a workspace clone when present. Without `git_repo`, it falls back but there is no clean "scratch mode" contract in the tool schema.

## 2. Dispatch Contract Evaluation

**What the spec envisions:** Explicit intent routing where Oikos parses user intent ("use Claude Code", "use Codex") and maps to backend selection. Plus explicit repo-vs-scratch delegation modes.

**Is this needed now?** Partially. The LLM is already smart enough to route between direct/tool/delegation without a keyword parser. What is missing:

1. **Backend intent mapping** — Oikos prompt should instruct the model to pass `backend` (or `model`) args to `spawn_commis` when the user specifies a preference. This is a prompt change, not code.
2. **Scratch mode** — Adding `scratch=True` to `spawn_commis` schema (skip git clone) would be clean but is low-urgency since most real work targets a repo.

**Recommendation: Defer further.** The dispatch "contract" is mostly a prompt engineering task. The model already routes correctly 90% of the time. When backend intent mapping becomes a real user need (multiple CLI backends in production), update the Oikos system prompt to include backend selection guidance and add a `backend` param to `spawn_commis`. No new code architecture needed.

## 3. Compaction API Evaluation

**What it is:** Anthropic's server-side context management feature (beta, `compact-2026-01-12`). When input tokens exceed a configurable threshold (default 150K, min 50K), Claude automatically generates a summary of older conversation turns. The summary is returned as a `compaction` content block. On subsequent turns, passing the compaction block back causes the API to ignore all earlier messages, effectively replacing history with the summary.

**Availability:** Beta as of 2026-01-12. Enabled via `context_management.edits` in the Messages API request + beta header. Supported on Claude Opus 4.6.

**Pricing:** No separate fee. The compaction step is an additional billed sampling iteration. Token costs at standard model rates ($5/MTok input, $25/MTok output for Opus 4.6). Reusing a previous compaction block on later turns does not re-incur compaction cost.

**How it would fit Oikos:**

Oikos maintains one long-lived thread per user. Today, context grows unboundedly — old turns are loaded from the DB into the message array on every call. Compaction would:

1. Add `context_management: {edits: [{type: "compact_20260112"}]}` to the Oikos LLM call
2. When the API returns a `compaction` block, store it alongside the thread
3. On next turn, include the compaction block — API ignores everything before it
4. Old messages remain in DB (lossless archive) but are not sent to the model

**Fit assessment:** Good. This directly solves the "infinite thread" problem without building a custom summarizer. The Oikos message flow already assembles messages from DB — the change is: (a) detect compaction blocks in responses, (b) store the latest compaction, (c) on next call, truncate the message array at the compaction boundary.

**Concerns:**
- Beta API — could change. The `compact_20260112` type string is versioned, suggesting stability intent.
- Anthropic-only — if Oikos uses OpenAI models, need a fallback (custom summarizer or sliding window).
- Compaction summary quality is opaque — can't control what gets preserved vs dropped.
- The extra sampling step adds latency (~2-5s) when it triggers.

## 4. Recommendations

| Item | Recommendation | Rationale |
|------|---------------|-----------|
| Dispatch contract | **Defer** — revisit when multi-backend commis is actively used | LLM already routes well. Backend intent is a prompt tweak, not architecture. |
| Compaction API | **Implement when needed** — good fit, low effort, but not blocking anything today | Oikos threads are not yet long enough to hit context limits in practice. When they are, compaction is the right solution. Estimate: ~1 day of work. |

**When to revisit:**
- Dispatch contract: when a second CLI backend (Codex or Gemini) is used in production by real users
- Compaction: when Oikos threads regularly exceed ~50K tokens (monitor via LLM audit logs)

Both items should stay in TODO as deferred, not removed. They are real needs that will surface with usage growth.
