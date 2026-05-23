# iOS Timeline Attention Colors

## Problem

The iOS timeline currently mixes two unrelated color systems on the same card:

- provider identity colors, such as Claude orange and Codex green
- runtime phase colors, such as thinking orange, running green, and closed gray

That makes color ambiguous. A user cannot tell whether a green or orange card means "this provider", "the agent is doing work", or "this session needs attention".

The timeline should use color for the user's attention contract, not for low-level runtime mechanics.

## Product Model

Timeline color answers one question:

> What should I understand or do right now?

The first iOS pass uses this model:

| User meaning | Runtime tones | Timeline treatment |
| --- | --- | --- |
| Agent is working | `thinking`, `running` | monochrome pulse, neutral/primary dot, quiet card edge |
| Work needs attention | `blocked` | amber attention treatment |
| Work appears stalled | `stalled` | amber attention treatment, no working pulse |
| Session is parked or ready but not explicitly watched | `idle`, `inactive`, `active` | gray quiet treatment |
| Session is finished | `closed` | dim gray treatment |
| Unknown or disconnected signal | anything else, stale connection | gray quiet treatment plus existing stale/offline copy |

Labels may still show runtime detail, for example `Thinking`, `Using Shell`, or `Blocked Shell`. The color does not distinguish thinking from tool use because both mean the user is waiting on the agent.

Provider identity should remain readable through the provider name and icon, but it should not compete with attention color. Provider badges should be neutral on timeline cards.

Red remains reserved for transport failure, degraded/error states, and the existing offline connection strip. A normal blocked agent needs user attention, but it is not the same severity as a broken connection.

`active` is not a working phase on this surface. It is treated as quiet unless the backend also reports a fresh `thinking` or `running` phase.

## Out of Scope

This pass does not implement watched sessions, push notifications, or a new "tell me when this agent finishes" pipeline.

The future watched-session model should make `working -> idle/needs_user` loud only when the user opted into caring about that transition. Without that opt-in, normal idle remains quiet.

## iOS Scope

Change the timeline card presentation only:

- `TimelineSessionCardRow`
- `ProviderBadge`
- `RuntimeBadge`
- timeline color helpers in `InboxView.swift`
- debug previews in `InboxViewPreviews.swift` if needed for QA coverage

Avoid backend contract changes. The backend can keep emitting the existing tone tokens because other clients and tests already use them as semantic state.

Avoid broad session-detail changes unless the timeline card and detail strip become visibly inconsistent during QA.

Do not change widget, Live Activity, session-detail header, `turnColor`, or `managementColor` in this pass.

## Success Criteria

- Claude, Codex, Gemini, Antigravity, and unknown provider badges render neutral text/icon treatment on timeline cards.
- `thinking` and `running` timeline tones share the same working visual treatment.
- Working cards pulse while the server phase deadline is fresh, preserving the existing freshness guard.
- A working session whose phase deadline has expired renders quiet/stale, not as static working.
- `blocked` and `stalled` use amber attention treatment, with no working pulse for stale/stalled states.
- `active`, `idle`, `inactive`, `closed`, stale, disconnected, and unknown states are gray/quiet.
- Non-healthy global connection states suppress per-card attention colors to neutral, preserving the current connection-state guard.
- Existing status labels and duration/stale copy remain unchanged.
- Reduce Motion disables the pulse animation.
- VoiceOver exposes the runtime status and duration without relying on color alone.
- The implementation is presentation-only: no API/schema changes.

## Local QA

- Run focused iOS tests that cover session model/timeline status behavior.
- Render the iOS timeline preview set with `ios/scripts/render-previews.sh` if the local toolchain can run it.
- If preview rendering is blocked, build an equivalent local HTML mock at iPhone dimensions that mirrors the card colors and verify via screenshot.
- Review screenshots for:
  - no provider identity color competing with runtime state
  - working states are visually unified
  - blocked/stalled stand out as attention states
  - idle/closed cards recede without looking disabled or broken
