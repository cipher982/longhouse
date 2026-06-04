# Session Alerting Research Spike

Status: Research spike
Date: 2026-06-04

## Goal

Design a notification system that pages the user when a CLI agent session needs
attention, without turning normal terminal-style back-and-forth into phone buzz.

The target use case is: start a long Claude/Codex task, leave the laptop, and
get told when the agent is blocked or when a substantial autonomous turn has
finished and is waiting for the user.

## Current State

Longhouse already has most of the native iOS transport plumbing:

- `server/zerg/services/apns_sender.py` prepares and sends APNs alert,
  background, widget, and Live Activity pushes.
- `server/zerg/models/apns_device_registration.py` stores app APNs tokens.
- `server/zerg/models/apns_live_activity_registration.py` stores per-session
  ActivityKit push tokens.
- `server/zerg/models/apns_widget_push_state.py` stores widget push debounce
  state.
- `server/zerg/routers/device_tokens.py` exposes APNs registration endpoints.
- `server/zerg/routers/users.py` exposes the global `apns_enabled` preference.
- `ios/Sources/LonghouseApp/PushNotifications.swift` requests notification
  permission, registers for remote notifications, handles tap-to-session, and
  removes delivered attention notifications after background resolution pushes.
- `ios/Sources/LonghouseApp/SessionLiveActivityManager.swift` starts one
  watched Live Activity and registers its ActivityKit push token with the
  server.

The current interruptive alert policy is intentionally narrow:

- `ATTENTION_PUSH_STATES = {"blocked"}`.
- `needs_user` is resolvable, so old or delivered attention notifications can
  be cleaned up, but it is not itself an alerting state.
- `runtime_display.needs_attention` is also effectively `blocked` only.
- `needs_user` is rendered as idle or waiting for the next prompt.
- The APNs attention title is currently hardcoded to `Needs permission`, which
  means there is no copy path yet for a second alert type like long-run done.

The current ambient channels are better developed than the alerting policy:

- Live Activity pushes update per-session state with a 15 second debounce.
- Widget timeline pushes update the active session set with a 30 second
  debounce.
- The iOS app refreshes widget timelines from push handling and foreground
  flows.

The web app does not currently have browser push:

- It has SSE streams and a `useDocumentVisible()` hook.
- It has no service worker, web app manifest, `PushManager` subscription flow,
  web-push/VAPID sender, or browser-notification UI.
- `LOOP_PUSH_VAPID_*` exists in config, but no active launch-surface web push
  path uses it.

## External Constraints

Apple notification design guidance points toward trust and restraint:

- Use notification urgency honestly.
- Time Sensitive notifications can break through Focus and scheduled delivery,
  so they should only be used for events that matter now or within about an
  hour.
- Critical alerts are not appropriate here.

For Longhouse, `blocked` is plausibly Time Sensitive: the agent is waiting on
the user and may otherwise waste a long unattended run. Ordinary `needs_user`
after a quick conversational turn is not.

Web platform constraints:

- Page Visibility is reliable for "is this tab visible?" and already has a
  local hook in the web app, but it says nothing about whether the user is at a
  terminal.
- Chrome's Idle Detection API can report coarse user idle and screen lock state,
  but it needs explicit permission/user gesture and is Chromium-specific.
- The Badging API is useful and non-interruptive for installed web apps, but
  Longhouse is not currently installable as a PWA.
- Web Push needs a service worker, user permission, server-side subscription
  storage, and a sender path. It is a separate product surface, not a small
  tweak to the existing SSE loop.

Comparable products mostly separate durable inbox state from interruptive
delivery:

- Slack routes mobile notifications based on desktop activity/inactivity and
  lets users delay mobile notifications.
- Linear keeps all notifications in an inbox, with desktop/mobile/email/Slack
  as delivery channels.
- GitHub Mobile supports notification schedules and working-hours style control.
- Teams distinguishes status/presence from notification delivery and lets users
  suppress mobile notifications while active elsewhere.

The shared lesson: first build a durable attention model and conservative
delivery policy, then add channel preferences.

## Product Model

Use three tiers:

1. Page now
   - `blocked`: permission or approval is required.
   - Default delivery: native iOS APNs alert.
   - Candidate interruption level: Active by default, with an optional
     Time Sensitive setting for blocks.

2. Nudge later
   - `needs_user` or idle after a substantial autonomous turn.
   - Only alert when the session ran long enough to imply unattended work and
     the user is not actively viewing Longhouse web.
   - This is the "your hour-long task finished" path.

3. Ambient
   - Ordinary `thinking`, `running`, `idle`, `needs_user`, and state churn.
   - Use timeline updates, Live Activity, widgets, in-tab badges/title changes,
     and in-app inbox state.
   - No phone buzz by default.

## Attention Policy Shape

Add a server-owned notification projection. Do not let web and iOS each infer
their own rules from raw runtime state.

Suggested projection inputs:

- Runtime transition: previous state, current state, occurred_at.
- Runtime history since last user prompt: active duration, tool-call count,
  state sequence, and last assistant turn completion.
- Session capability/control path: managed vs unmanaged, steerable vs observe
  only.
- Client presence: foreground web tab heartbeat, not iOS background presence.
- Preferences: global enable, quiet hours, per-session watch/mute, event class.
- Recent notification history: sent, collapsed, dismissed, resolved, renudged.

Suggested event classes:

- `session_blocked`: immediate attention.
- `session_blocked_reminder`: unresolved block after a configurable delay.
- `long_run_waiting`: substantial autonomous turn is waiting for the user.
- `attention_resolved`: silent cleanup/background event.
- `ambient_state_changed`: non-interruptive badge/widget/live activity update.

## Presence And Away

Do not try to infer terminal focus in v1.

Longhouse currently cannot know whether the user is sitting at the terminal
because the provider CLI is a separate process and a bare terminal has no
relationship to Longhouse clients. The reliable v1 signal is narrower:

- A visible web tab means the user is likely already watching Longhouse.
- No visible web tab means it is acceptable to push the phone for eligible
  events.
- iOS foreground can suppress foreground banners locally, but iOS background
  absence is not a useful "away" signal. The phone is primarily the push target.

Optional later local signals:

- Desktop App or Machine Agent reports screen locked / system idle.
- Desktop App reports whether Longhouse menu/window is foreground.
- Managed provider wrapper reports recent terminal input or foreground TTY
  activity if it can do that without privacy surprises.
- Browser IdleDetector can refine web presence for Chrome users who opt in.

These should be explicit, visible privacy-scoped signals. They should not block
v1.

## Data Model

Add a durable notification kernel before adding more channel logic.

Minimum tables:

- `notification_events`
  - id
  - owner_id
  - session_id
  - event_type
  - state_key / collapse_key
  - event_started_at
  - eligible_at
  - delivered_at
  - resolved_at
  - dismissed_at
  - channel_results JSON

- `notification_client_presence`
  - owner_id
  - client_id
  - client_type (`web`)
  - visible
  - route/session_id
  - last_seen_at

Preferences can start thin:

- Keep the existing global `apns_enabled`.
- Add quiet hours.
- Add per-session watch/mute.
- Add event toggles only for `blocked` and `long_run_waiting`.

Avoid a full channel-by-event matrix until web push exists.

## Recommended Phases

### Phase 0 - Stabilize Existing Block Alerts

Goal: improve the current `blocked` path before adding a new event type.

- Introduce `notification_events` on the existing APNs attention path for audit,
  dedupe, and future tuning.
- Split alert copy so `blocked` is not hardcoded through one title helper.
- Decide and implement a blocked reminder policy:
  - fire once only, or
  - re-nudge once after N minutes if still blocked and unresolved.
- Decide whether `blocked` should be Time Sensitive by default or only when the
  user opts in.
- Keep the existing APNs registration and resolution cleanup path.

### Phase 1 - Web Presence And Ambient Web Cues

Goal: get enough presence to avoid pushing while the user is already watching.

- Add `POST /api/clients/presence` for browser-authenticated web clients.
- Send heartbeats on visibility changes and on a 30 to 60 second foreground
  cadence.
- Treat stale or absent web presence as "eligible to push," not proof of being
  away.
- Add in-tab ambient cues from existing SSE:
  - document title marker or count,
  - favicon dot,
  - optional short local sound only after explicit user setting,
  - `navigator.setAppBadge()` only if available.

### Phase 2 - Long-Run Finished Nudge

Goal: implement the actual "the hour-long task finished" alert.

Initial eligibility:

- transition from `thinking` or `running` to `needs_user` or `idle`;
- active duration since last user prompt >= threshold, probably 10 to 15 minutes;
- no foreground web-tab presence within the last 2 to 5 minutes;
- session is active/open, not closed;
- no recent equivalent notification for this session/collapse key;
- global and session preferences allow it.

Better eligibility after instrumentation:

- active execution duration, not just wall-clock state dwell;
- count of tool calls or meaningful transcript deltas since last user prompt;
- whether the final assistant turn contains an explicit ask vs normal idle.

Copy should avoid overstating:

- "Longhouse task is waiting"
- "Codex finished a long turn"
- "Claude needs your next prompt"

Do not label ordinary `needs_user` as attention in `runtime_display` unless the
server also has enough context to explain why it is interruptive.

### Phase 3 - Optional Web Push / Idle Detection

Defer unless there is real demand.

- Add manifest + service worker + PushManager subscription flow.
- Add web-push sender with VAPID keys and subscription storage.
- Use Badging API for installed web apps.
- Consider IdleDetector only as an optional Chrome-only enhancement.

Native iOS already covers the primary "away with phone" case, so web push is not
required for the launch slice.

## Policy Decisions To Make

1. Is `blocked` Time Sensitive by default?
   - Recommendation: make it opt-in globally or per-session first, but design
     the APNs payload/header path now.

2. Should unresolved `blocked` re-nudge?
   - Recommendation: one reminder after 10 to 15 minutes, then stop until state
     changes or the user re-watches the session.

3. What is a long run?
   - Recommendation for v1: an autonomous turn with at least 10 to 15 minutes
     of fresh `thinking`/`running` since the last user prompt.
   - Better later: duration plus tool-call count or transcript delta count.

4. Is long-run notification opt-in by default?
   - Recommendation: default on only for managed sessions, with per-session mute
     and a global toggle. For unmanaged/search-only sessions, keep it ambient
     until runtime truth is strong enough.

5. What suppresses mobile push?
   - Recommendation: visible Longhouse web tab suppresses non-urgent long-run
     nudges. It should not suppress `blocked` unless the user has explicitly
     chosen "notify only when away."

6. How should quiet hours interact with blocks?
   - Recommendation: queue long-run nudges, allow Time Sensitive blocks only if
     the user opted into that urgency.

## Risks

- `needs_user` is currently normal idle in both backend display policy and iOS
  semantics. Promoting it to an interruptive state without extra context would
  recreate notification spam.
- iOS background heartbeats are not dependable enough for server presence.
- Web push would add permission and service worker complexity before the core
  attention policy is proven.
- New alert types should not reuse the same debounce stamp fields in a way that
  cross-suppresses `blocked` and `long_run_waiting`.
- If the user misses the first `blocked` notification, current behavior can sit
  silently for a long time unless we add a reminder policy.

## Recommendation

Build the launch slice in this order:

1. Instrument and clean up the existing `blocked` APNs path.
2. Add a one-time unresolved-block reminder.
3. Add web-tab presence and ambient web cues.
4. Add long-run-finished nudges using conservative thresholds.
5. Defer web push and browser idle detection until the native iOS path proves
   the policy.

This keeps Longhouse aligned with the core product loop: notify only when the
user can usefully steer a real session, and keep everything else ambient.
