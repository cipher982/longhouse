# Timeline Card Glanceability (iOS + Web)

**Status:** Research spike → spec. Not yet built.
**Surfaces:** iOS Timeline, web Timeline, iOS home-screen widget, shared model logic.
**Owner:** owner@example.com

---

## 1. Problem

The iOS Timeline is a vertical list of session cards (newest activity first). Today
each row is mostly grey/white and carries little glanceable signal:

- The right ~50% of every row is dead weight — a `Managed` badge that is identical on
  ~95% of rows, plus a `47 turns · 776 tools` footer that has no scan-time meaning.
- Liveness is a near-invisible **white pulsing dot vs grey static dot** — the user
  cannot tell active from idle at a glance.
- The preview line renders raw text and degrades to garbage: sessions started from a
  paste show `""" …` or `[Image #1] …` instead of a topic.
- There is no fast read of the three things the user actually wants per row:
  **which project · what it's about · is it active / waiting on me / done.**

What the user *does* like and we keep: the small tinted provider glyphs (Claude / Codex /
Gemini SVGs) that anchor the left edge.

### The muscle-memory tension

The user wants the per-row topic to behave like the ChatGPT sidebar: a short phrase set
**when the conversation starts** that **never changes**, so row N means the same thing
every time the app is opened (recognition, not re-reading). Today the opposite happens —
the enrichment reconciler re-summarizes on transcript-revision lag, so any title bound to
`summary_title` **drifts** over the session's life. Stability is the feature.

---

## 2. Key finding: the signal already exists

Most of what the redesign needs is already in the data and is simply not wired to the card.

| Want | Reality today |
|------|---------------|
| A short topic title | `summary_title` is LLM-written ("3-8 words, Title Case"), shipped to iOS (`SessionModels.swift:537`) — **the card never displays it** (`InboxView.swift:257`). |
| "Waiting on you" signal | `needs_attention` is a **curated first-class bool** (`SessionModels.swift:104`), distinct from the noisy raw `needs_user` state. |
| Active / done states | Runtime state machine already exists: `thinking / running / idle / needs_user / blocked / stalled / closed`. |
| Color hooks | `tone` + `border_tone` tokens already drive the accent. |
| Provider identity | `provider` already drives the glyph. |

**The garbage preview** is literally `InboxView.swift:257` rendering raw `summary`, then the
unsanitized `firstUserMessage` fallback ladder (`SessionModels.swift:198-204`).

**The only genuinely new backend field is `anchor_title`** — a frozen, write-once title.
Everything else is rewiring + a pure sanitizer.

---

## 3. Spec

### 3.1 Shared decisions (apply to every option / surface)

1. **Promote a topic title to the row's primary line.** It is the missing headline and the
   direct answer to "what is this session about."
2. **Freeze the displayed title — write-once `anchor_title`, never re-rendered mid-session.**
   The reconciler may keep improving `summary_title` for search/detail, but the card binds to
   a snapshot. Row N means the same thing every open.
3. **Never render raw `first_user_message` / `summary` on the card.** Use a sanitizer + a
   fallback ladder (see 3.3).
4. **Color is a signal-bearing axis only — cap status hue at ~3 semantic stops.** Provider tint
   stays on the glyph as a *separate* categorical (identity) axis; it must never bleed onto the
   status dot.
5. **Reserve motion for genuine liveness.** `needs_attention` is **steady** (waiting ≠ working);
   only `thinking/running` with recent activity animates (typically 1-3 rows). All meaning must
   survive Reduce Motion via fill + color.
6. **Remove the Managed/Unmanaged text badge and the `N turns · M tools` footer** from the
   resting row. Managed becomes an *affordance* (swipe-to-reply present/absent, chevron), not a
   label. Cumulative counts move to detail view.
7. **Derive title/anchor/sanitizer in shared/backend logic, not per-client**, so iOS
   (`TimelineBuilder.swift`), web (`timelineModel.ts`), and the widget render the identical
   frozen string. (CLAUDE.md pairing-parity rule.)

### 3.2 Color system

Three orthogonal axes, none encoded twice:

- **Provider = glyph hue** (categorical brand identity; unmanaged = desaturated/outline glyph).
- **Attention = status dot + accent, ordered ramp:**
  - `needs_attention` → **amber** (`#E8A23D`), steady. The only saturated thing in the common case.
  - live `thinking/running` (recent activity) → **teal/green** (`#3DB5C8`), breathing.
  - `idle / stalled / closed` → **grey**, static.
- **Freshness (optional, Option C)** = opacity/desaturation of the whole row.

Dark-mode: mid-saturation accents (~0.9 dot / ~0.3 border). Colorblind-safe: amber / teal / grey
separate on **luminance** and blue-yellow (not red-green); the **text state label is the redundant
code** and is load-bearing — do not let a later "cleaner" pass drop the word and leave color alone.

Reserve **red** strictly for a true fault tier (`blocked/stalled`, Option B) or connectivity
failure — never for routine idle.

### 3.3 Title resolution ladder (the garbage fix)

Pure sanitizer + fallback, run **server-side before any freeze**:

1. ready `anchor_title` (frozen sanitized `summary_title`)
2. `"Summarizing…"` placeholder while `summaryStatus == pending`
3. sanitized `first_user_message` — strip ``` fences, `[Image #N]`, URLs; collapse whitespace;
   take first ~8 words
4. `"{project} session"`

The sanitizer must run **before** the freeze so garbage is never persisted into `anchor_title`,
and the freeze must prefer a ready `summary_title` over any first-message fallback.

### 3.4 `anchor_title` lifecycle

- New column on **`AgentsBase`** (not `Base`), nullable, so `_auto_add_missing_columns()`
  auto-ALTERs it at startup.
- Snapshot the sanitized `summary_title` → `anchor_title` at **first `ready`**.
- The reconciler **never overwrites** `anchor_title` (it keeps mutating `summary_title` for
  search/detail).
- **On session close** (`process_gone` lifecycle), promote the latest sanitized `summary_title`
  → `anchor_title` once, for closed sessions only. This avoids freezing an early title generated
  from a 3-event opening transcript while still giving revisited/closed sessions their best stable
  title.

---

## 4. Options

### Option A — Signal Refit (conservative) — **recommended spine**

Same row skeleton; spend the change budget only on signal-to-noise.

```
│ ◆C  ZERG · feat/refresh        2m │
│ ● Fix Refresh Token Rotation    › │   amber dot, steady = waiting on you
├───────────────────────────────────┤
│ ◆O  G55                       1m │
│ ◉ Debug Bedrock Channel Race      │   teal dot, breathing = live
│   Using Codex                     │
├───────────────────────────────────┤
│ ◆G  ZERG                      3h │
│   Archive Export Cleanup          │   grey, static = closed
│   Closed                          │
```

- **Pro:** smallest diff, fastest ship, preserves muscle memory, fixes all four complaints.
- **Con:** plain; reclaimed right half just gets quieter; topic-drift signal hidden until you tap in.

### Option B — Instrument Panel (editorial / dark-cockpit) — **end state**

Headline-led like a news item. Strict value ramp; most rows go visually dark, the row that wants
you pops by **contrast**, not intensity. Adds a demoted italic `now: …` third line = the live
`summary_title` parked where movement is legitimate and the eye does not index — the honest home
for the drift signal.

```
│ C │ ZERG · feat/refresh        ⌁ │
│ C │ Fix Refresh Token Rotation    │
│ C │ ▸ now: rotating refresh    2m │
╞═══ amber bar ═════════════════════╡
│ O │ G55                           │
│ O │ Debug Bedrock Channel Race    │
│ O │ Awaiting you              1m │
```

- **Pro:** highest clarity; needs-you row unmistakable; keeps drift without breaking muscle memory.
- **Con:** bigger restructure (3 typographic tiers, accent-bar logic, red fault tier that depends on
  `blocked/stalled` being trustworthy at source); more Dynamic Type / colorblind QA.

### Option C — Cadence Card (data-forward + micro-viz) — **PARKED**

Turns the dead right half into an activity sparkline (`▁▃▅█▆ live`) — "grinding vs stuck" at a glance.

- **Pro:** densest live signal; most differentiated; Reduce-Motion friendly (rhythm is spatial).
- **Con:** **requires a per-bucket activity time-series that does not exist today**; sparkline at
  ~330pt risks looking non-native/decorative. The cheap decay-ring fallback is **not** a shortcut —
  it re-adds motion to the edge we just quieted for ~10% of the value.

---

## 5. Plan of Attack

Sequenced so each slice ships independently. A is the spine, B-lite layers on, C is gated.

| # | Workstream | Surface | Depends on | Effort |
|---|------------|---------|-----------|--------|
| 1 | **Sanitizer + `anchor_title` field** (shared truth; freeze at first-ready, reconciler never overwrites; expose `anchor_title` + `summaryStatus` on payload) | backend | — | M |
| 2 | Promote final title → `anchor_title` on session close (once, closed only) | backend | 1 | S |
| 3 | **iOS Slice 1:** kill garbage preview; bind primary line to `anchor_title` via the ladder; stop rendering raw `summary`/`firstUserMessage` | iOS | 1 | M |
| 4 | **iOS Slice 2:** 3-stop dot/color (amber off `needsAttention`, breathing teal for live, static grey); delete Managed badge + turns/tools footer; retint accent by attention tier; Reduce-Motion static fallback | iOS | 3 | M |
| 5 | **Web parity:** same anchor binding, ladder, 3-stop dot/color, trimmed row | web | 3 | M |
| 6 | **iOS widget parity:** point `SessionsWidgetView` at `anchor_title` + shared sanitizer (truncation only) — Xcode build-and-run, no push deploy | iOS | 3 | S |
| 7 | **B-lite:** subordinate live `now: …` drift line (italic / low-contrast); ship behind a quiet toggle if dogfood shows churn | iOS + web | 4 | M |
| 8 | **PARKED — Cadence micro-viz:** decision gate. Requires new per-bucket activity time-series + per-provider cadence baseline. Re-evaluate after A + B-lite are in dogfood. | backend + iOS + web | 7 | L |

**First shippable value** = slices 1 + 3: the `"""`-garbage dies and a real frozen headline
appears, with no new pipeline. Slice 4 makes "waiting on you" pop. Those are the two loudest
complaints and land first.

---

## 6. Risks / Traps

- **Freeze location.** The freeze must be a **new `anchor_title` column**, not a reuse of
  `summary_title`. The reconciler still overwrites `summary_title` as the transcript grows — bind
  the card to it and you still drift.
- **Snapshot timing.** Freezing at first `ready` can capture a title from a tiny opening transcript
  (`"Initial Setup"`). Mitigated by the on-close promotion (slice 2).
- **Garbage can be frozen.** A weak sanitizer freezes `""" [Image #1]` permanently. Sanitize
  **before** freeze; prefer a ready `summary_title` over any first-message fallback; never persist an
  unsanitized fallback.
- **Colorblind / dark-mode.** The redundant text state label is load-bearing — do not let a future
  pass drop it and leave color as the sole code. Keep provider hue on the glyph only.
- **Motion / alarm fatigue.** The current white pulse is invisible *because everything pulses*. Keep
  `needs_attention` **steady**; animate only live `thinking/running`; stale-running drops to static.
  Verify Reduce Motion via the `#Preview` render script, not by eye.
- **Shared-logic divergence.** Anchor freeze + sanitizer land server-side / shared so iOS, web, and
  the widget render the identical string. A Swift-only sanitizer drifts from web and breaks the widget.
- **Option C scope creep.** Do not smuggle the sparkline in early via the decay ring.

---

## 7. Final Goals (definition of done for the epic)

A user opening the Timeline can, **in under one glance per row and without reading carefully:**

1. **Know which project + what each session is about** — a frozen topic headline, stable across
   opens (muscle memory holds).
2. **See which session is waiting on them** — a steady amber annunciator that pops out of a list of
   mostly-quiet rows.
3. **See which sessions are actively working vs done** — breathing teal vs static grey, honest under
   Reduce Motion.
4. **Never see `"""` / `[Image #1]` garbage** — the sanitizer + ladder guarantee a meaningful line.
5. **Spend no attention on dead weight** — the repeated Managed badge and raw turns/tools are gone
   from the resting row; managed-ness is an affordance, counts live in detail.

Color earns its place on every axis (provider = identity, amber/teal/grey = attention, optional
opacity = freshness); nothing is decorative. iOS, web, and the widget stay in lockstep.
