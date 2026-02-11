# Session Processing & Briefing Discovery

**Date:** 2026-02-11
**Session:** Claude Opus 4.6 (~90 min deep dive)
**Spec produced:** `docs/specs/session-processing-module.md`

## What We Discovered

### 1. SessionStart Hook Bug (Critical)

The `~/.claude/hooks/longhouse-session-start.sh` hook uses `systemMessage` in its JSON output, which **only displays to the human in the terminal**. The AI model receives only `"SessionStart:startup hook success: Success"` — zero session context.

**The fix:** Claude Code hooks support `hookSpecificOutput.additionalContext` for injecting into the model's context. Both fields can coexist.

**Current output (broken):**
```json
{"systemMessage": "Longhouse: 288 sessions in zerg (7d):\n  ..."}
```

**Correct output:**
```json
{
  "systemMessage": "Longhouse: 3 recent sessions in zerg",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Recent work in zerg:\n..."
  }
}
```

**Files:**
- Hook script: `~/.claude/hooks/longhouse-session-start.sh` (line 57, the `jq` output)
- Hook config: `~/.claude/settings.json` (lines 16-26, SessionStart block)
- Claude Code docs: `hookSpecificOutput.additionalContext` is the correct field for SessionStart hooks

**Secondary bug:** The `\n` in the jq output renders as literal `\n` instead of newline on the first line. The hook uses `\\n` (escaped) in string interpolation.

### 2. Three-Hook System Architecture

The hooks form a closed loop installed by `longhouse connect --install`:

| Hook | Script | Trigger | Purpose |
|------|--------|---------|---------|
| **Stop** | `longhouse-ship.sh` | Every AI response | Ships transcript to Longhouse via `longhouse ship --file` |
| **SessionStart** | `longhouse-session-start.sh` | New session opens | Queries `GET /api/agents/sessions?project=X&limit=5&days_back=7` |
| **SessionEnd** | `session_end.sh` | Session closes | Runs `ralph learn --project X --hours 4` (insight extraction, throttled 1/hr) |

**Auth:** Device token at `~/.claude/longhouse-device-token` (format: `zdt_...`), instance URL at `~/.claude/longhouse-url`.

**Scope:** All three hooks are global (`~/.claude/settings.json`), not per-project. They fire in every directory. The SessionStart hook derives project from `basename $CWD`, which works for `~/git/zerg` → "zerg" but produces noise for `~` → "davidrose".

### 3. Existing Pipeline Inventory

Four separate pipelines process session data today:

#### Life-Hub Embeddings (`life-hub/src/life_hub/embeddings/`)
- **Input:** `agents.events` rows via asyncpg
- **Processing:** Extract user+assistant only (84% of events are tool calls — excluded). Check `raw_json` for `toolUseResult` to filter. Detect turns by timestamp interleaving. Build 4 chunk types: user, assistant, combined (per-turn), summary (`GOAL: {first_user} / RESULT: {last_assistant}`).
- **Embeddings:** `text-embedding-3-small` (1536 dims), batched up to 2048/request via async OpenAI client
- **Map-reduce:** Token-weighted mean of chunk embeddings → single reduced vector per type
- **Scoring:** kNN against labeled anchors (great/good/meh/bad/frustrated)
- **Token limit:** 7500 per chunk (below 8191 OpenAI limit)
- **Truncation:** Keep end for user/assistant trajectory, keep start for per-turn
- **Storage:** pgvector in `agents.session_embeddings`
- **Job queue:** `FOR UPDATE SKIP LOCKED`, 15-min stale detection

#### Zerg Daily Digest (`zerg/jobs/daily_digest.py`)
- **Input:** `AgentEvent` rows via SQLAlchemy for all production sessions in a 24h window
- **Processing:** Noise stripping (XML tags: `<system-reminder>`, `<function_results>`, `<env>`, `<claude_background_info>`), secret redaction, per-message 1000 token truncation, 8000 token budget per session
- **Summarization:** OpenAI chat completion, model from `get_model_for_use_case("summarization")` (TIER_2 = gpt-5-mini), 2-4 sentence summary per session
- **Concurrency:** `asyncio.Semaphore(3)` for map phase
- **Output:** Plain text email via user's Gmail OAuth
- **Schedule:** Cron `0 8 * * *`, registered via `job_registry`
- **Token encoding:** `o200k_base` (GPT-5 era)

#### Zerg Memory Summarizer (`zerg/services/memory_summarizer.py`)
- **Input:** Task + result text from completed Oikos runs
- **Model:** `gpt-5-mini` with `reasoning_effort: "minimal"` via OpenAI Responses API
- **Output:** Structured JSON (title, topic, outcome, summary_bullets, tags) → markdown memory file → embedding
- **Storage:** `memory_files` table + `memory_embeddings` (bytes blob, brute-force dot product search)

#### Ralph Learn (`myagents/scripts/ralph.sh`)
- **Input:** Recent sessions via `query_agents()` SQL
- **Processing:** LLM agent (z.ai/GLM-4.7 via hatch) reads session content, extracts patterns/failures/learnings
- **Output:** Insights DB via `log_insight()`
- **Trigger:** SessionEnd hook, throttled to 1/hr per project

### 4. Shared vs Unique Code

**Nearly identical (extract immediately):**
- Token counting: both use tiktoken, differ only in encoding (`cl100k_base` vs `o200k_base`)
- Content hash: SHA-256
- Embedding model constant: `text-embedding-3-small`

**Same purpose, different implementation (parameterize):**
- Truncation: Life-hub does head/tail cut; Zerg does sandwich (head+tail with marker)
- Content filtering: Life-hub checks `raw_json.toolUseResult`; Zerg strips XML noise + redacts secrets
- Session data model: dicts vs dataclasses

**Unique to one pipeline:**
- Turn detection, chunked embeddings, map-reduce, kNN scoring → Life-hub only
- Noise stripping, secret redaction, Gmail sending → Zerg only

### 5. Key Design Decision: Module Not Package

Original proposal was a standalone `agentlog` PyPI package. Rejected because:
- The shipper already handles format-specific JSONL parsing (`parser.py` with `ParsedEvent`, `ParsedSession`)
- Both current consumers are in the same Zerg codebase
- Life-hub's pipeline will migrate TO Longhouse per VISION.md
- No benefit to cross-repo dependency management

Final design: `zerg/services/session_processing/` module within Longhouse backend.

### 6. Codex Review Key Findings

- **Ship deterministic core first** (content, tokens, transcript), LLM extras later
- **Schema inconsistency**: `structured_summary` promised bullets but `SessionSummary` lacked the field (fixed)
- **Prompt injection risk**: injected session summaries could contain hostile instructions. Label as untrusted.
- **Token encoding drift**: `cl100k_base` vs `o200k_base` will silently change behavior. Callers must specify explicitly. Golden tests required.
- **Provider abstraction**: don't plumb `model/base_url/api_key` per function. Pass client object once.
- **`basename $CWD` for project detection** is weak for monorepos. Consider explicit project mapping.

### 7. Briefing Feature Design

**The killer feature:** Every Claude Code session starts with awareness of recent work. No other tool does this.

**Architecture:**
```
Stop hook → longhouse ship → /api/agents/ingest → store events
  → background: generate summary via session_processing.summarize
  → cache on AgentSession.summary + .summary_title

SessionStart hook → GET /api/agents/briefing?project=X
  → read cached summaries → format as additionalContext (~600-1000 tokens)
  → AI starts warm
```

**Adaptive depth by time gap:**
- <15 min since last session: 3 lines (~100 tokens)
- 15min-4h: 8 lines (~300 tokens)
- 4h-24h: 15 lines (~600 tokens)
- 1-3 days: 25 lines (~1000 tokens)
- >3 days: extended with decisions + unfinished work (~1500 tokens)

**Summary generation:** z.ai (GLM-4.7, flat rate) for David's instance. OSS users use their configured model.

## API & Endpoint Details

### Existing Endpoints (working)

- `POST /api/agents/ingest` — session ingestion (gzip, dedup via SHA-256 event_hash, rate limit 1000 events/min)
- `GET /api/agents/sessions?project=X&limit=N&days_back=N` — session list (used by current hook)
- `GET /api/agents/sessions/{id}/events` — event list with role/tool filtering
- `GET /api/agents/sessions/{id}/preview` — last N messages
- `GET /api/agents/filters` — distinct projects/providers for dropdowns

### New Endpoint (to build)

- `GET /api/agents/briefing?project=X&limit=5` — pre-computed summaries formatted for AI context injection

### Auth

- `X-Agents-Token: zdt_...` header (device token)
- Dev mode: `AUTH_DISABLED=1` allows all
- Rate limiting: per-device key, 1000 events/min, HTTP 429 with Retry-After

## Files to Create/Modify

### Create
- `zerg/services/session_processing/__init__.py`
- `zerg/services/session_processing/content.py` — extract from `daily_digest.py` lines 125-141 + `shared/redaction.py`
- `zerg/services/session_processing/tokens.py` — merge `shared/tokens.py` + life-hub `worker.py` token functions
- `zerg/services/session_processing/transcript.py` — `build_transcript()`, `detect_turns()`
- `zerg/services/session_processing/summarize.py` — `quick_summary()`, `structured_summary()`, `batch_summarize()`
- `zerg/routers/agents.py` — add `GET /api/agents/briefing` endpoint

### Modify
- `zerg/models/agents.py` — add `summary` (Text) + `summary_title` (String) columns to `AgentSession`
- `zerg/routers/agents.py` — trigger summary generation after ingest (async background task)
- `~/.claude/hooks/longhouse-session-start.sh` — fix JSON output to include `hookSpecificOutput.additionalContext`, call briefing endpoint instead of sessions endpoint
- `zerg/jobs/daily_digest.py` — refactor to use `session_processing.transcript` + `session_processing.summarize` (Phase 2)
- `zerg/services/memory_summarizer.py` — refactor to use `session_processing.summarize` (Phase 2)

### Don't Touch (stays as-is)
- `zerg/services/shipper/parser.py` — already handles JSONL extraction correctly
- `zerg/services/shipper/shipper.py` — transport layer, no processing changes needed
- `~/.claude/hooks/longhouse-ship.sh` — working correctly
- `~/.claude/hooks/session_end.sh` — working correctly (ralph learn)

## Implementation Order

1. **Fix the hook** (5 min) — swap `systemMessage` → both fields in `longhouse-session-start.sh`
2. **Create session_processing module** — `content.py`, `tokens.py`, `transcript.py` with golden tests
3. **Add summarize.py** — `quick_summary()` using z.ai
4. **Add summary columns** to `AgentSession` model
5. **Wire summary generation** into ingest path (async after `POST /api/agents/ingest`)
6. **Add briefing endpoint** — `GET /api/agents/briefing`
7. **Update hook** to call briefing endpoint instead of raw sessions list
8. **Refactor daily digest** to use session_processing (Phase 2)
9. **Refactor memory summarizer** to use session_processing (Phase 2)
10. **Add embeddings.py** when life-hub pipeline migrates (Phase 3)

## Risks & Gotchas

- **Token encoding mismatch:** `cl100k_base` (embedding model) vs `o200k_base` (GPT-5 era tokenizer). Will change truncation behavior. Must golden-test.
- **Prompt injection:** Session text injected as `additionalContext` could contain hostile instructions. Use labeled boundaries.
- **Hook timeout:** SessionStart hook has 5-second timeout. Briefing endpoint must respond in <50ms (pre-computed summaries, simple DB read).
- **Summary generation cost:** z.ai is flat-rate (free). OSS users with metered APIs may not want auto-summarization. Make it opt-in via config.
- **`basename $CWD` project detection:** Produces "davidrose" for `~`, "zeta" for work repos. Works but noisy. Consider explicit project mapping or allowlist.
