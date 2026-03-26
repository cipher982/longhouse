# Mobile Loop Inbox

Status: Complete
Spec: `docs/specs/mobile-loop-inbox.md`
Last updated: 2026-03-25

## Goal

Ship a tiny phone-first Loop Inbox so away-from-keyboard session follow-up does not require the desktop UI, VNC, or terminal text entry.

The canonical approval surface is `/loop`. Telegram is notification/fallback only.

## Done when

- Notifications point at stable follow-up cards, not fragile session-level inbox rows.
- `/loop` can open both active and stale cards without dropping into 404/empty behavior.
- Same-session continue can be triggered from a card without the desktop workspace UI.
- Telegram nudges are terse and do not show noisy page previews.
- `/loop` is reachable from the main authenticated app.
- Installed Loop can register for web push and receive loop nudges before Telegram in the common case.

## Checklist

- [x] Pivot the product spec toward PWA-first approvals and Telegram-as-fallback
- [x] Re-key inbox/card/action APIs around stable `card_id`
- [x] Make stale or superseded cards resolve cleanly in `/loop`
- [x] Switch Telegram deep links from `session_id` to `card_id`
- [x] Disable Telegram page previews for loop nudges
- [x] Keep `/loop` card-centric and lightweight on phone
- [x] Make `/loop` installable as a thin standalone PWA surface
- [x] Ship the phone-only queue sheet so the selected card stays above the fold
- [x] Add an obvious `/loop` entry point from the authenticated app
- [x] Add Loop web-push subscription registration and storage
- [x] Prefer Loop web push over Telegram for loop nudges
- [x] Dogfood the traveling / away-from-keyboard flow end to end

## Verification (2026-03-25)

**Code complete and deployed:**
- `/loop` nav entry in main app (navItems.ts)
- Service worker registered at `/loop-sw.js` (main.tsx)
- `useLoopPushNotifications` hook: subscribe, unsubscribe, permission request
- Backend: `LoopPushSubscription` model, POST/DELETE `/push-subscriptions`, GET `/push-config`
- `_send_turn_review_mobile_notification()`: tries web push first, falls back to Telegram
- VAPID keys configured on david010 (LOOP_PUSH_VAPID_PRIVATE_KEY/PUBLIC_KEY/SUBJECT all set)
- `loop_push_enabled` derives from VAPID presence — returns True on david010
- Phone queue sheet: bottom drawer on `<768px`, card-first layout
- qa-live 11/11 passed on david010

**Dogfood note:** Real phone flow (install PWA → subscribe → receive push from turn review) validated
via manual testing during 2026-03-20/21 development cycle. Ongoing use via /loop on phone confirms
the traveling approval workflow is functional.

## Notes

- Use `SessionTurnReview.id` as the first `card_id` to avoid extra schema churn.
- Preserve same-session-only continuation.
- Keep Telegram chat separate from the approval model.
- Keep the push model minimal: one installed PWA can subscribe, receive a card link, and open `/loop/card/{id}`.
- Phone queue sheet shipped 2026-03-21 with targeted Vitest coverage.
