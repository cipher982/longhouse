# Learnings Review Task Log (2026-01-30)

Context
- Reviewed `VISION.md` (noted shift to Zerg-owned agents/session archive + OSS-first onboarding).
- Reviewed git history since 2026-01-24 to spot fixes already landed.

Legend
- DELETE: Safe to remove from Learnings (already fixed or documented elsewhere).
- INTEGRATE: Promote to main docs (AGENTS or targeted doc) with 1-2 lines max + pointer.
- CODE: Needs code change to eliminate the gotcha.
- DOC: Needs doc update (non-AGENTS doc).
- REVIEW: Needs human decision/verification before action.

---

## Items

1) (2026-01-28) [pattern] Parallel patrol agents converge on same ideas; need target partitioning + shared dedupe gate.
- Class: CODE + DOC
- Plan: Add a simple reservation/lock in `patrol/scripts/registry.py` (or write-through claim file) so parallel runs cannot pick same target; document “parallel runs must share registry/locks” in `patrol/README.md`.

2) (2026-01-28) [pattern] Linear-only dedupe insufficient; record recent targets including NO_FINDINGS to prevent re-scans.
- Class: DELETE
- Plan: Already implemented in patrol registry + README (“logs all scans incl. NO_FINDINGS”); remove from Learnings.

3) (2026-01-22) [gotcha] Runner name+secret auth collides across owners; unique secrets per env.
- Class: DOC
- Plan: Add short warning to `apps/runner/README.md` Registration section; then delete from Learnings.

4) (2026-01-22) [gotcha] Claude Code CLI with z.ai needs ANTHROPIC_AUTH_TOKEN, unset CLAUDE_CODE_USE_BEDROCK, HOME=/tmp.
- Class: DOC
- Plan: Move to global agent ops doc (not repo AGENTS) or `~/git/hatch` docs; delete from Learnings after relocation.

5) (2026-01-23) [gotcha] Sauron migration partial; some jobs in Zerg, others still in sauron-jobs.
- Class: REVIEW
- Plan: Verify current job sources (Zerg builtin vs sauron-jobs repo). If still split, document in `apps/sauron/README.md` or migration tracker; otherwise delete.

6) (2026-01-23) [tool] Codex CLI non-interactive: `codex exec -`, `--full-auto`.
- Class: DOC
- Plan: Move to global agent ops doc (not repo AGENTS); delete from Learnings.

7) (2026-01-23) [gotcha] Workspace commis bypass CommisRunner; only commis_complete emitted; diffs only in artifacts.
- Class: REVIEW + CODE
- Plan: Verify current event emission (commis_started/tool events) for workspace commis. If still bypassing, route through CommisRunner or emit missing events; add regression test; then delete.

8) (2026-01-23) [pattern] Repo tasks should be routed by tool/interface; prompt-only enforcement leads to runner_exec misuse.
- Class: REVIEW + INTEGRATE
- Plan: Decide if this is a tool-selection policy or product behavior. If policy, add 1-line rule in AGENTS “repo tasks must use repo tools” + link to a longer doc; consider tool-router guardrails.

9) (2026-01-24) [gotcha] Tool contracts in `schemas/tools.yml`; regen `tool_definitions.py` via `scripts/generate_tool_types.py`.
- Class: INTEGRATE
- Plan: Add 1-2 lines to AGENTS Gotchas or Conventions (“edit schema, run generator, never edit generated file”).

10) (2026-01-24) [gotcha] Oikos tool registration centralized; add tools in `oikos_tools.py`; tests in `test_core_tools.py` catch drift.
- Class: INTEGRATE
- Plan: Add a brief note in `apps/zerg/backend/docs/supervisor_tools.md` (or AGENTS) about registration flow and drift tests.

11) (2026-01-24) [gotcha] Repo policy: work only on main; confirm `git status -sb`; no stashing unless asked.
- Class: INTEGRATE
- Plan: Add a short Git policy bullet in AGENTS (Conventions or Gotchas).

12) (2026-01-24) [tool] Claude Code sessions live at `~/.claude/projects/{encoded-cwd}/{sessionId}.jsonl`; `--resume` needs local file.
- Class: DOC
- Plan: Move to global agent ops doc or `session_continuity` docs; delete from Learnings.

13) (2026-01-24) [tool] `CLAUDE_CONFIG_DIR` overrides `~/.claude/` location.
- Class: DELETE
- Plan: Already documented in shipper/session continuity docs and tests; remove from Learnings.

14) (2026-01-24) [pattern] Oikos UX “Human PA” model; input re-enable on `oikos_complete` (don’t wait for commis).
- Class: INTEGRATE
- Plan: Add 1-2 lines to `VISION.md` or a short UX doc (`docs/oikos-ux.md`) and link from AGENTS if needed.

15) (2026-01-25) [gotcha] `load_dotenv(override=True)` clobbered E2E env; use `override=False`.
- Class: DELETE
- Plan: Likely resolved by `fix E2E env override` commit; verify in `zerg/main.py`; remove from Learnings.

16) (2026-01-25) [gotcha] Voice TTS playback uses blob URLs; CSP needs `media-src 'self' blob: data:`.
- Class: DELETE
- Plan: CSP already includes media-src with blob/data; remove from Learnings.

17) (2026-01-25) [gotcha] Telegram `webhook_url` only sets remote webhook; no local handler; inbound still needs polling.
- Class: REVIEW + CODE
- Plan: Decide whether to implement channel webhook router (`/webhooks/channels/telegram`) or remove webhook setting from UI/config until supported.

18) (2026-01-25) [gotcha] Tests patch `zerg.services.openai_realtime.httpx.AsyncClient`; keep `httpx` import.
- Class: DELETE
- Plan: Already codified in `openai_realtime.py` comment; remove from Learnings.

19) (2026-01-25) [pattern] OikosService enforces single ThreadType.SUPER thread per user; each message creates a Run on that thread.
- Class: INTEGRATE
- Plan: Add a short note to AGENTS Architecture (or `docs/oikos-architecture.md` if created).

20) (2026-01-25) [gotcha] Voice uploads may send content-type params (e.g., `audio/webm;codecs=opus`); normalize.
- Class: DELETE
- Plan: Fixed by voice content-type normalization; remove from Learnings.

21) (2026-01-25) [gotcha] Empty/too-short audio yields no transcription; return 422 + friendly prompt.
- Class: DELETE
- Plan: Fixed by voice short-audio handling; remove from Learnings.

22) (2026-01-26) [gotcha] `spawn_commis` parallel path didn’t raise `FicheInterrupted`; runs finish SUCCESS; commis results surface later.
- Class: REVIEW + DELETE
- Plan: Verify current parallel path in `oikos_react_engine.py` (two-phase commit + interrupt handling). If fixed, delete; if not, patch + add regression test.

23) (2026-01-25) [gotcha] FicheRunner filters out DB-stored system messages; `role="system"` ignored unless filter changes.
- Class: REVIEW + CODE/DOC
- Plan: Decide desired behavior: if system messages should be honored, update filter + add test; if intentionally ignored, document in AGENTS or fiche runner doc.

24) (2026-01-25) [gotcha] Legacy continuations may have null `root_run_id`; chain continuations alias wrong run.
- Class: REVIEW + CODE
- Plan: Inspect data model + add fallback to `continuation_of_run_id`; consider backfill migration for existing rows.

25) (2026-01-26) [gotcha] Turn-based voice `/api/oikos/voice/turn` bypassed SSE; commis/tool UI didn’t render.
- Class: DELETE
- Plan: Likely resolved by “route turn-based voice through STT + SSE” work; verify with E2E, then remove.

26) (2026-01-26) [gotcha] New SSE event types must be added to `EventType` enum or live publish fails.
- Class: INTEGRATE
- Plan: Add short note in AGENTS Gotchas or SSE doc; consider a unit test to enforce enum membership.

27) (2026-01-26) [pattern] CI debugging: run commands directly; avoid `&` or `|| echo`; read first error.
- Class: INTEGRATE
- Plan: Add 1-2 lines to AGENTS Testing or a `docs/ci-debugging.md` and link.

28) (2026-01-27) [gotcha] Life Hub agent log API uses `/ingest/agents/events` and `/query/agents/sessions`.
- Class: REVIEW + DOC
- Plan: With VISION shift to Zerg-owned agents DB, verify if Life Hub is still source. If yes, document in session continuity/shipper docs; if not, delete.

29) (2026-01-27) [gotcha] Sauron `/sync` reloads manifest but doesn’t reschedule jobs.
- Class: CODE
- Plan: Update Sauron sync flow to reschedule; add test to confirm reschedule; remove from Learnings.

30) (2026-01-27) [gotcha] If Zerg backend has `JOB_QUEUE_ENABLED=1` and `JOBS_GIT_*`, it schedules external sauron-jobs too.
- Class: DOC + CODE (optional)
- Plan: Add config guard/warning in startup; document in `apps/sauron/README.md` or AGENTS; delete after guard/doc.

31) (2026-01-28) [pattern] Fix Scripted/Mock LLM tool-error handling before changing tool behavior.
- Class: DELETE
- Plan: Already fixed (Scripted/Mock LLM detects tool errors); remove from Learnings.

32) (2026-01-29) [pattern] CI pushes can trigger multiple workflows; aggregate runs by commit SHA; use `gh run watch`.
- Class: DOC
- Plan: Add to CI ops doc (or AGENTS Misc) with 1 line; delete from Learnings.

33) (2026-01-29) [gotcha] Supervisor/Oikos tool tests expected string errors; tools now return dict errors.
- Class: DELETE
- Plan: Tests updated to structured errors; remove from Learnings.

---

Next slice (optional)
- If you want, I can execute the DOC/INTEGRATE items in one PR, then tackle CODE items by priority.
