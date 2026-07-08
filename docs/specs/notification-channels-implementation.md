# Notification Channels — Implementation Task List

Living tracker for `docs/specs/notification-channels.md`.

## PM decisions (locked)

- [x] Kill Telegram for user notifications; runner alerts **email-only**
- [x] Tier 1 pushes with web tab visible **by default**; `notify_only_when_away` opt-in suppresses Tier 1
- [x] Quiet hours **queue** Tier 1/2 unless Time Sensitive enabled for blocked events

## Phase A — Kill and stabilize

- [x] Remove Telegram from `runner_health_reconciler.py` (email-only external alerts)
- [x] Unregister `send_telegram` from `BUILTIN_TOOLS`
- [x] Update `test_runner_health_reconcile.py` for email path
- [x] Commit: `fix(notifications): remove telegram runner alerts and default tool`

## Phase B — Preferences and suppression audit

- [x] Add `notification_policy.py` (prefs, quiet hours, suppression reasons)
- [x] Add `AgentSession.notification_muted` column
- [x] Extend `GET/PATCH /users/me/notifications` with new prefs
- [x] Add `PATCH /timeline/sessions/{id}/notification-watch`
- [x] Wire policy into `apns_sender.py` prepare paths + suppression event rows
- [x] Time Sensitive APNs header when user opts in
- [x] Queue deferred notifications; maintenance loop delivery tick
- [x] Tests: `test_notification_policy.py`, update `test_apns_notifications.py`
- [x] Commit: `feat(notifications): prefs, suppression audit, quiet-hours queue`

## Phase C — Web ambient cues

- [x] Favicon attention marker in `useAmbientSessionAttentionCue`
- [x] Frontend test for favicon cue
- [x] Commit: `feat(web): favicon attention cue for hidden tabs`

## Review gates

- [x] `make test` (backend)
- [x] `make test-frontend` (web)
- [ ] Merge to `main` and push
