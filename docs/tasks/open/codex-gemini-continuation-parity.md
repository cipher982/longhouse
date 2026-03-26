# Codex/Gemini Continuation Parity

Status: In progress (Codex done, Gemini TBD)
Last updated: 2026-03-26

## Goal

Make “continue any synced session from the cloud” true for Codex and Gemini, not just Claude.

## Done when

- The headless executor/Hatch path can invoke provider-specific resume commands for Codex and Gemini.
- Longhouse can reconstruct provider-local session state for those providers the way it already does for Claude.
- `POST /sessions/{id}/chat` works beyond Claude-backed sessions.
- Focused regression coverage exists for non-Claude web continuation.

## Checklist

- [x] Reconstruct provider-local session state for Codex in continuity prep
- [x] Generalize `POST /sessions/{id}/chat` beyond Claude-only assumptions
- [x] Add regression coverage for Codex continuation (6 tests in `test_codex_cloud_continuation.py`)
- [ ] Extend the headless executor/Hatch path for Gemini resume commands
- [ ] Reconstruct provider-local session state for Gemini in continuity prep
- [ ] Revisit any remaining UI copy once the backend path is real

## Notes

- Codex cloud continuation landed in `e05ad2ea` (2026-03-26). Key files:
  - `session_continuity.py`: `prepare_codex_session_for_resume()`, `get_codex_config_dir()`, `_find_latest_codex_session_file()`
  - `session_chat.py`: `_build_codex_resume_runtime()`, `_check_codex_binary()`, Codex JSONL parser in `stream_claude_output()`
- Codex sessions placed at `~/.codex/sessions/YYYY/MM/DD/rollout-{timestamp}-{session_id}.jsonl`
- Codex `exec resume --json --full-auto` is the non-interactive equivalent of `claude --resume`
- Gemini CLI does not have a resume command yet — blocked on upstream.
