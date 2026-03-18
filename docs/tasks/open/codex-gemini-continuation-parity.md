# Codex/Gemini Continuation Parity

Status: Not started
Last updated: 2026-03-17

## Goal

Make “continue any synced session from the cloud” true for Codex and Gemini, not just Claude.

## Done when

- The headless executor/Hatch path can invoke provider-specific resume commands for Codex and Gemini.
- Longhouse can reconstruct provider-local session state for those providers the way it already does for Claude.
- `POST /sessions/{id}/chat` works beyond Claude-backed sessions.
- Focused regression coverage exists for non-Claude web continuation.

## Checklist

- [ ] Extend the headless executor/Hatch path for Codex and Gemini resume commands
- [ ] Reconstruct provider-local session state for Codex/Gemini in continuity prep
- [ ] Generalize `POST /sessions/{id}/chat` beyond Claude-only assumptions
- [ ] Add regression coverage for Codex/Gemini continuation
- [ ] Revisit any remaining UI copy once the backend path is real

## Notes

- Local Codex CLI already exposes `codex exec resume ... --json`; the missing layer is Longhouse/provider-state reconstruction.
