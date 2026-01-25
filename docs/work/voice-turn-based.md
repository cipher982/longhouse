# Jarvis Turn-Based Voice (Option 1)

Last updated: 2026-01-25
Owner: David Rose

Principle: turn-based only, no realtime browser calls, 80/20 simplicity.
Update this doc after every meaningful change.

## Summary
Replace realtime mic flow with a turn-based STT → Supervisor → TTS round-trip. The Jarvis chat UI becomes “press to talk, release to send,” then the assistant responds with text + audio playback. No voice mode toggles or env-driven switches.

## UX Spec (Manager View)
- Chat page shows a mic button next to the text input.
- Press and hold to record; release to send.
- Status changes: ready → listening → processing → speaking → ready.
- Transcript is shown as the user message; assistant response shows as text + plays audio.
- Errors: mic shows error state + assistant bubble shows a short error message.

## Data Flow
1) `MediaRecorder` captures mic audio (prefer `audio/webm;codecs=opus`, fallback types).
2) POST FormData to `/api/jarvis/voice/turn` with `return_audio=true`.
3) Response returns `{ transcript, response_text, tts.audio_base64, tts.content_type }`.
4) UI appends user + assistant messages and auto-plays audio.

## Non‑Goals
- Realtime streaming transcripts / VAD.
- Voice mode toggle or custom voice picker UI.
- Hands‑free mode.

## Open Risks / Notes
- Autoplay policies may require audio to be triggered from a user gesture.
- MediaRecorder MIME support varies by browser (fallback needed).

## Status Key
- [ ] pending
- [~] in progress
- [x] done
- [!] blocked

## Phase 0 — Plan Review
- [!] Gemini review of plan (tool timeout; retry pending).

## Phase 1 — Frontend Turn-Based Voice
- [ ] Add `useTurnBasedVoice` hook (record → upload → render → play audio).
- [ ] Add `voiceTurn` API helper in `services/api/`.
- [ ] Wire Jarvis `App.tsx` mic handlers to turn-based hook (remove realtime connect step).
- [ ] Guard/disable realtime voice listeners in `useJarvisApp` to avoid status conflicts.
- [ ] Make `MicButton` ignore presses while processing/speaking.

## Phase 2 — Error Handling + UX Polish
- [ ] Surface voice errors in chat (short assistant bubble + mic error state).
- [ ] Ensure audio playback cleanup (revoke object URLs, reset status).

## Phase 3 — Tests
- [ ] Unit: mock MediaRecorder + fetch for `useTurnBasedVoice`.
- [ ] E2E: stub mic + voice-turn response; assert transcript + response appear.

## Phase 4 — QA
- [ ] Manual smoke: mic record → transcript + response + audio playback.
- [ ] Verify no CSP errors in prod console.
