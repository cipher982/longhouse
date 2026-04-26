# Session Loop Policy And OpenRouter Provider

Status: Active implementation
Last updated: 2026-04-26

## Goal

Make the session loop policy contract explicit from stored session rows through
API schemas, web controls, and hosted model routing.

The launch product has two normal user-facing policies:

- `assist`: default. Longhouse may draft the next human reply for review.
- `autopilot`: policy preview only until a durable autopilot runner exists.

Legacy `manual` may still exist in old rows or old clients, but it is no
longer a normal product mode. Read paths should normalize it to `assist`.
Write paths should reject or avoid creating it except where old database rows
are backfilled during startup migration.

## Provider Policy

OpenRouter is the preferred hosted text provider because one API key can route
to multiple model providers behind OpenAI-compatible chat completions.

Runtime rules:

- `OPENROUTER_API_KEY` is first-class for text LLM capability.
- OpenRouter uses `https://openrouter.ai/api/v1`.
- OpenRouter requests should identify the app with `HTTP-Referer` and
  `X-OpenRouter-Title` when going through our OpenAI-compatible client path.
- Embeddings remain on the configured embedding provider. Do not silently move
  vector spaces to OpenRouter.

## Today Task List

1. Backfill and default session `loop_mode` to `assist`.
2. Normalize legacy `manual` rows to `assist` in session API responses.
3. Remove web affordances that intentionally create a no-assistance state.
4. Default `longhouse claude`, `longhouse codex`, and managed-local API launch
   requests to `assist`.
5. Make OpenRouter the hosted/david text routing default and keep provider
   config/test endpoints aligned with it.
6. Configure the david010 hosted runtime environment with `OPENROUTER_API_KEY`
   from Infisical.
7. Run targeted backend/frontend tests and ask Hatch Opus to review the
   worktree before merging or shipping.

## Non-Goals

- Do not activate automatic Autopilot sends in this change.
- Do not add a hidden server loop that spends LLM calls on every turn without a
  persisted draft/audit contract.
- Do not change embedding provider compatibility.
