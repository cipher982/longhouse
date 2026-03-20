# Mobile Loop Inbox

Status: In progress
Spec: `docs/specs/mobile-loop-inbox.md`
Last updated: 2026-03-19

## Goal

Ship a tiny phone-first Loop Inbox so away-from-keyboard session follow-up does not require the desktop UI, VNC, or terminal text entry.

The canonical approval surface is `/loop`. Telegram is notification/fallback only.

## Done when

- Notifications point at stable follow-up cards, not fragile session-level inbox rows.
- `/loop` can open both active and stale cards without dropping into 404/empty behavior.
- Same-session continue can be triggered from a card without the desktop workspace UI.
- Telegram nudges are terse and do not show noisy page previews.

## Checklist

- [x] Pivot the product spec toward PWA-first approvals and Telegram-as-fallback
- [ ] Re-key inbox/card/action APIs around stable `card_id`
- [ ] Make stale or superseded cards resolve cleanly in `/loop`
- [ ] Switch Telegram deep links from `session_id` to `card_id`
- [ ] Disable Telegram page previews for loop nudges
- [ ] Keep `/loop` card-centric and lightweight on phone
- [ ] Dogfood the traveling / away-from-keyboard flow end to end

## Notes

- Use `SessionTurnReview.id` as the first `card_id` to avoid extra schema churn.
- Preserve same-session-only continuation.
- Keep Telegram chat separate from the approval model.
