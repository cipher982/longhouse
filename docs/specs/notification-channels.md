# Notification Channels — First-Principles Spec

Status: Decision spec (supersedes policy sections of `session-alerting-research-spike.md`)
Date: 2026-07-07
Owner: Architecture; PM sign-off: David
Reads: `VISION.md`, `docs/specs/session-alerting-research-spike.md`, `AGENTS.md` (Session Modes, Product Focus)

This spec decides what Longhouse ships for user notifications at launch, what it kills, and how delivery works across channels. It is a build/kill document, not a survey. Where the June 2026 research spike proposed things that have since shipped, this spec records them as current state and moves the decision frontier forward.

---

## 1. Problem Statement

Longhouse's core promise is steering live agent sessions on user-owned machines. That promise fails silently if the user walks away and never learns the session is waiting on them. Today:

- The **iOS APNs path is real and reasonably mature**: `apns_sender.py` already implements `session_blocked`, `session_blocked_reminder`, `session_needs_answer`, and `long_run_waiting` events, a `notification_events` audit table, per-device registration, Live Activity and widget ambient pushes, and suppression using both machine presence and web client presence.
- The **web path is ambient-only**: SSE plus `useDocumentVisible()`, a working presence heartbeat (`POST /users/me/client-presence`), but no in-tab attention cues (title/favicon/badge) and no web push.
- **Telegram is half-dead legacy**: the Oikos operator bridge was torn down in April 2026; nothing writes `telegram_chat_id` anymore, there is no settings UI, and its one remaining consumer — runner-offline alerts from `runner_health_reconciler.py` — has a formatting bug (raw `<b>` tags rendered literally) and paged David unexpectedly when runners degraded. It is exactly the "half-supported surface" `VISION.md` says to delete.
- **Preferences are one bit** (`apns_enabled`). There is no quiet hours, no per-session watch/mute, no event-class toggle.

The risk is not "too few channels." It is shipping a channel matrix nobody trusts. Every product that gets notifications wrong loses the permission grant and never gets it back.

## 2. Principles

1. **Attention policy first, channels second.** The server owns a single notification projection (already embodied in `apns_sender.py` + `notification_events`). Clients render outcomes; they never re-derive eligibility from raw runtime state. This is `VISION.md` invariant 9 ("one source of truth per capability") applied to notifications.
2. **One buzz per human decision.** A notification is justified only when the user can act on it right now (approve, answer, prompt next). State churn is never a buzz.
3. **Presence suppresses, absence permits.** A visible Longhouse web tab means the user is watching; suppress non-urgent pushes. Absence of presence is *eligibility to push*, never proof of being away.
4. **Honest urgency.** Time Sensitive is reserved for `blocked`/`needs_answer` and is opt-in. Critical alerts are never appropriate.
5. **Session attention and ops/infra are different products.** Runner health is support tier (AGENTS.md); it must never share a paging channel with session attention by accident, which is precisely how the Telegram runner spam happened.
6. **Prefer deletion over half-supported surfaces.** No channel ships at launch unless it has a settings UI, an audit trail, and a resolution/cleanup path.

North star: *when a managed session needs you and you're not watching in web, your phone buzzes once with enough context to act — everything else is glanceable.*

## 3. Event Taxonomy

Four user-intent tiers; ops/infra is a fifth, non-launch tier. Event types below match the constants already in `apns_sender.py` where they exist.

### Tier 1 — Page now (interruptive)

| Event | Trigger | Collapse key | Modes |
|---|---|---|---|
| `session_blocked` | Runtime enters `blocked` (permission gate) | `session:{id}:blocked` | Helm, Console |
| `session_needs_answer` | Structured question pause request active | `session:{id}:blocked` (shared — same human decision class) | Helm, Console |
| `session_blocked_reminder` | Still blocked/unanswered after 15 min (`BLOCKED_REMINDER_DELAY`), fired once | same as parent | Helm, Console |

Delivery: APNs alert, interruption level Active by default, Time Sensitive per opt-in (§6). Visible web tab does **not** suppress Tier 1 unless the user opts into "notify only when away."

### Tier 2 — Nudge later

| Event | Trigger | Collapse key | Modes |
|---|---|---|---|
| `long_run_waiting` | `thinking`/`running` → `needs_user`/`idle` after a meaningful autonomous run (thresholds already tiered in code: 30 min default, 15 min when idle ≥10 min, 10 min when machine is locked, 5 min minimum meaningful run) | `session:{id}:long_run` | Helm, Console |

Delivery: APNs alert, standard interruption level. Suppressed by fresh visible web presence (90 s window, `WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW`) and by active machine presence (user typing at that machine — `MACHINE_ACTIVE_SUPPRESSION_GRACE_WINDOW`). Deferred by quiet hours (§6).

Critical guardrail carried over from AGENTS.md: `needs_user` alone is *not* an alerting state — it is usually the provider's normal idle prompt. Only the long-run projection may promote it, and Tier 1 and Tier 2 must never share debounce stamps (`sessions.last_attention_push_*` semantics — cross-suppression between `blocked` and `long_run_waiting` is a known failure class).

### Tier 3 — Ambient (never a buzz)

- `ambient_state_changed`: runtime churn → Live Activity pushes (15 s debounce), widget timeline pushes (30 s debounce), SSE-driven timeline updates, and (new, Phase B) in-tab title marker + favicon dot.
- Applies to **all modes including Shadow**. Shadow sessions are live but not steerable, so they never generate Tier 1/2 events — there is no control path, hence no action the user can take from the notification. Shadow gets ambient liveness only. If Shadow runtime truth later becomes strong enough and there is user pull, `long_run_waiting` for Shadow can be revisited — as opt-in, never default.

### Tier 4 — Resolution (silent)

- `attention_resolved`: state left the attention set → remove delivered notifications, clear stamps, end Live Activity urgency. Already implemented via resolution pushes; keep.

### Tier 5 — Ops/infra (support tier, not launch product)

- `runner_offline` / `runner_recovered` from `runner_health_reconciler.py`. See §8 for the channel decision (email only; Telegram killed).

### Helm vs Console differences

Both are managed and share the taxonomy. Differences are copy and deep-link, not eligibility:

- **Helm**: the user has a terminal somewhere. Machine presence suppression matters most here — if they're typing on the machine running the session, don't page the phone. Notification copy should name the machine ("Claude on `slim` needs permission").
- **Console**: no terminal exists; Longhouse UI is the only surface, so notifications are the *primary* return path. Console sessions default `watch=on`; long-run thresholds may eventually be tighter for Console, but v1 keeps one threshold set to avoid a policy matrix.

## 4. Channel × Situation Matrix

Channels at launch: **APNs alert**, **APNs ambient** (Live Activity + widget), **web in-tab ambient**, **email** (ops only). Explicitly absent: web push, Telegram, email-for-session-attention, SMS.

| Situation | Tier 1 (blocked/answer) | Tier 2 (long run done) | Tier 3 (ambient) |
|---|---|---|---|
| Web tab visible | APNs alert still fires (default); in-tab cue | Suppressed; in-tab cue only | SSE/in-tab |
| Web tab closed/hidden, iOS installed | APNs alert | APNs alert | Live Activity, widget |
| iOS foreground | Local banner suppressed by app for the visible session, delivered otherwise | Same | In-app |
| No iOS installed | **Nothing interrupts.** In-tab cue when tab reopens; `notification_events` row still recorded | Same | Web only |

The "no iOS installed" row is the launch gap we consciously accept: iOS is personal-Xcode-install only (no TestFlight). Web push is the eventual answer; we do not rush it (§9 Phase D). What makes the gap tolerable pre-launch: `notification_events` is durable, so an in-app "needs attention" affordance can be projected from the same rows the moment we build it — no policy rework.

## 5. Presence Model v1

**What exists and is the contract:**

- `POST /users/me/client-presence` with `{client_id, client_type: "web", visible, route, session_id}` → `NotificationClientPresence` upsert with `last_seen_at`. Web sends heartbeats on visibility change plus a foreground cadence (30–60 s).
- Fresh visible web presence within **90 s** suppresses Tier 2. Stale/absent presence = eligible to push.
- `MachinePresence` (freshness window 90 s, active-suppression grace 3 min) suppresses Tier 2 when the user is demonstrably active at the machine running the session; machine-locked state *tightens* the long-run threshold to 10 min.

**What we explicitly cannot know in v1, and refuse to fake:**

- Terminal focus. Helm runs the provider TUI invisibly; a bare terminal has no relationship to Longhouse clients. We do not infer it, and no eligibility rule may depend on it.
- iOS background presence. The phone is a push target, not a presence source.

**Later, opt-in only (not launch):** Desktop App screen-lock/foreground signals, browser IdleDetector (Chromium-only). These refine suppression; they must never gate delivery.

## 6. Preference Model

Launch preferences, in priority order (thin by design — no channel-by-event matrix until a second interrupt channel exists):

1. `apns_enabled` (exists) — global kill switch for all APNs alerts. Keep.
2. **Per-session watch/mute** — the single highest-leverage control. Muted session: no Tier 1/2 pushes, ambient continues. Defaults: Helm/Console watch=on; Shadow has nothing to watch (no Tier 1/2 exists for it).
3. **Time Sensitive for blocked** — global boolean, default **off**. Applies only to `session_blocked`/`session_needs_answer`. The APNs payload path should carry the interruption-level header now even while the toggle defaults off.
4. **Quiet hours** — one daily window, user-local. Tier 2 events are *queued*: if still valid (unresolved, session open, collapse key not superseded) at window end, deliver once; else drop silently. Tier 1 delivers through quiet hours only when Time Sensitive is enabled; otherwise it queues too, with the reminder clock paused.
5. **"Notify only when away"** — global boolean, default off. When on, visible web presence suppresses Tier 1 as well.

Storage: items 1, 3, 4, 5 in user prefs; item 2 as a per-session flag on the session row or a small `session_notification_prefs` table. All SQLite-core; nothing touches the control plane.

Explicitly rejected for launch: per-channel routing preferences, per-provider preferences, per-project rules, notification schedules beyond one quiet-hours window. Each is a matrix cell we would have to migrate later; none is needed to prove the policy.

## 7. Data Model

Mostly exists. Contract restated so clients and future channels build on it rather than around it:

- **`notification_events`** — audit source of truth (per AGENTS.md: `channel_results` JSON is the diagnostic record; `sessions.last_attention_push_*` is only a debounce stamp and may be rolled back after failed sends). Fields: `id, owner_id, session_id, event_type, collapse_key, event_started_at, eligible_at, delivered_at, resolved_at, dismissed_at, channel_results`. Every Tier 1/2 decision — including suppressions — should eventually land a row; suppressed events record *why* in `channel_results` (e.g. `{"suppressed": "web_presence"}`) so policy tuning has data.
- **`notification_client_presence`** — exists; §5 shape.
- **Debounce stamps** — per-event-class stamps on the session row. Rule: adding an event class requires its own stamp fields or keyed stamps; never overload an existing stamp.
- **New for prefs (Phase B):** quiet-hours fields on user prefs; per-session watch flag. No new tables beyond possibly `session_notification_prefs`.

Deliberately absent: a generic `channels` table for user notifications, a delivery queue service, a fan-out worker. One interrupt channel does not need channel abstraction; `channel_results` JSON keeps the audit shape channel-plural so adding web push later is additive.

## 8. Kill / Freeze / Keep Decisions

| Surface | Decision | Rationale |
|---|---|---|
| **Telegram as user notification channel** | **Kill** | No settings UI, no linking flow (nothing writes `telegram_chat_id` since the April Oikos teardown), a live formatting bug, and its only trigger spammed David during runner degradation. Fails every §2.6 criterion. Concretely: remove the Telegram branch from `runner_health_reconciler.py` (`_send_telegram_alert` and the telegram-preferred fallback order), stop reading `telegram_chat_id` in alerting paths. |
| **`send_telegram` agent tool + channel plugin** | **Freeze, internal-only** | The channel plugin SDK is generic infrastructure and the agent tool can be personally useful; but unregister `send_telegram` from default tool registries so no product path depends on it. It must not appear in any launch surface, docs, or settings. If it costs anything to keep frozen, delete it too. |
| **Runner-offline alerting** | **Keep, email-only** | Runners are support tier. Route `_maybe_send_external_alert` to SES email only, keep the 5-minute threshold and incident dedup. David's ops paging need is met by email (real inbox, no new infra) plus the existing incident rows; if that proves too slow post-launch, the correct upgrade is a *separate, clearly-labeled* "infrastructure" APNs category — never re-entangling ops with session attention, and never Telegram. |
| **Email for session attention** | **Defer** | Wrong latency class for "act now"; email surfaces are hidden from launch nav anyway. Revisit only as a digest, post-launch, if there is pull. |
| **Web push (VAPID/service worker)** | **Defer** | Permission prompt + service worker + subscription lifecycle is a full surface. iOS covers the away case for launch users. Config keys stay; no code path. |
| **APNs alert/ambient paths** | **Keep, tune** | The proven kernel. Remaining work is copy/deep-link polish and prefs, not architecture. |
| **macOS menu bar as notification surface** | **Keep ambient-only** | Status glances, no local notification fan-out at launch — it would duplicate policy client-side, violating §2.1. |

## 9. Competitive Patterns — one lesson each

- **Slack**: desktop activity suppresses mobile push. We generalize: *any* trusted presence signal (web tab, machine activity) suppresses non-urgent push. Already implemented server-side.
- **Linear**: the inbox is durable state; channels are just delivery. Our `notification_events` is that durable layer — keep writing it even when nothing is delivered, so a future in-app attention view is a projection, not a migration.
- **GitHub Mobile**: working-hours schedules earn permission retention. Our single quiet-hours window is the minimal version; resist finer scheduling until asked.
- **Teams**: presence and delivery are separate nouns users can inspect. We keep `notification_client_presence` inspectable and never let clients infer policy from it directly.

## 10. Launch Cut

**Ship:**
- Tier 1 + Tier 2 APNs alerts as implemented, with copy split per event type and machine-name context for Helm.
- Tier 3 ambient: Live Activity, widgets, SSE, plus new in-tab title/favicon cues.
- Presence: web heartbeat + machine presence suppression (exists).
- Prefs: `apns_enabled`, per-session watch/mute, Time Sensitive opt-in, quiet hours.
- `notification_events` audit for every Tier 1/2 decision, including suppressions.
- Runner alerts on email only.

**Defer:** web push, email session digests, Shadow long-run nudges, desktop-app presence signals, per-channel preference matrix, in-app notification inbox UI.

**Kill:** Telegram user notifications (all of §8 row 1), `send_telegram` from default registries.

## 11. Phased Rollout

**Phase A — Kill and stabilize (small, do first).**
Remove Telegram from `runner_health_reconciler.py` (fixes the HTML-escaping bug by deletion); unregister `send_telegram` from default tooling; verify email fallback fires for a simulated runner-offline incident. Audit that `blocked` vs `long_run_waiting` stamps cannot cross-suppress (regression test).

**Phase B — Preferences and suppression audit.**
Per-session watch/mute; quiet hours with Tier 2 queueing; Time Sensitive opt-in wired to APNs interruption-level headers; "notify only when away." Record suppression reasons into `notification_events.channel_results`.

**Phase C — Web ambient cues.**
Document-title marker and favicon dot driven by the same server projection over SSE (a `runtime_display.needs_attention`-derived flag, not client-side re-derivation). Optional local sound behind an explicit setting. `navigator.setAppBadge()` only if trivially available.

**Phase D — Post-launch, on pull only.**
Web push (manifest + service worker + VAPID sender + subscription storage), in-app attention inbox projected from `notification_events`, Shadow nudges, desktop presence signals.

Phases A and B are launch-blocking. C is launch-desired. D is explicitly not.

## 12. Open Questions

1. **Console default thresholds** — should Console long-run thresholds be tighter than Helm's (no terminal to notice completion)? v1 ships one threshold set; instrument `notification_events` first.
2. **Reminder cap** — one blocked reminder then silence: is a second escalation ever justified for Console sessions where the notification is the only return path? Recommendation: no for launch; measure unresolved-block dwell times.
3. **In-app attention affordance timing** — does launch need any visible "sessions needing you" list in web (even a timeline sort boost), or is per-session badging enough? Cheap either way given the event table; needs a product call.
4. **`needs_answer` copy** — structured questions can carry the question text into the alert body; how much transcript content is acceptable in a lock-screen notification (privacy)? Default to the question title only.

## 13. Decisions Requiring PM Sign-off

1. **Kill Telegram entirely for user notifications, runner alerts go email-only** (§8). This changes David's own ops paging.
2. **Tier 1 pushes even when the web tab is visible, by default** (§3, §6.5) — "notify only when away" is opt-in, not default.
3. **Quiet hours queue Tier 1 unless Time Sensitive is opted in** (§6.4) — a blocked session can wait until morning by default.
