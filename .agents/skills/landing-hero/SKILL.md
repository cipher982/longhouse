# Landing Hero: Live Real-PTY Demo

Use this skill when the task touches the landing hero demo, the terminal
recordings behind it, or the marketing video exports: "update the hero",
"refresh the demo", "re-record the terminals", "the model names are stale",
"add a provider to the demo".

## What the hero is (August 2026)

The hero is NOT a video and NOT a player frame. The landing page renders
the demo natively as DOM (`HeroDemo`, lazy chunk ~13KB gzip): a looping
rAF clock drives four beat components that are ordinary responsive
flow layout — terminals size their cells from measured container width,
mobile stacks instead of shrinking, captions are real selectable text,
and the beat dots seek. The content is **literal recorded PTY output**
from real provider binaries, captured in a sandboxed rig and replayed as
styled text. Remotion (`ControlRoom`) survives ONLY as the export lane
for mp4/poster/OG where a fixed 16:9 frame is genuinely required.

The narrative lives in ONE place: `video/src/demo/script.ts` (providers,
beat schedule, captions, steer choreography, take-coupled replay
windows). Both renderers consume it; re-records retune windows there and
nowhere else. The web imports it via the `@longhouse/video/demo` subpath,
which has zero Remotion imports — the web bundle carries no Remotion.

Pipeline, end to end:

```text
providers.yml (pinned source + auth mode + sentinels)
  -> fetch.py            stock binary into .sandbox/, sha256-locked
  -> record.py --sandbox real binary under srt isolation, mock LLM on loopback,
                         asciicast v2 + meta sidecar; canary + fail-closed gates
  -> retime.ts           cap idle gaps (timestamps only, never drop events)
  -> compile.ts          offline @xterm/headless replay -> grid timeline JSON
  -> demo/script.ts      THE shared narrative: beats, captions, replay windows
  -> TerminalGrid.tsx    pure React: time -> binary-search state -> styled runs
  ├-> web landing/demo/  native DOM hero: useDemoClock + beat components
  └-> ControlRoom.tsx    Remotion composition -> make demo-render (export only)
```

## Key files

- `video/scripts/terminal/` — the rig: `providers.yml`, `providers.lock.json`,
  `fetch.py`, `record.py`, `mock_llm.py`, `retime.ts`, `compile.ts`,
  `record-all.sh`, `fixtures/<provider>.json` (canned model turns)
- `video/src/assets/terminal/` — committed casts + compiled grid JSONs
  (`<prov>.…` = 100x16 detail take; `<prov>-tile.…` = 64x14 tile take;
  the web hero uses ONLY the 64-col tiles — readable at every width)
- `video/src/demo/script.ts` — the SINGLE narrative source: providers,
  beat schedule + captions, steer choreography, REPLAY WINDOWS
  (take-coupled, see traps), poster second
- `video/src/demo/recordings.ts` — typed grid-timeline exports
- `video/src/terminal/TerminalGrid.tsx` — the grid renderer (pure React,
  shared by both renderers; no Remotion imports, keep it that way)
- `web/src/components/landing/demo/` — the shipping hero: `HeroDemo.tsx`
  (beat crossfade, caption, seek dots), `useDemoClock.ts` (rAF loop,
  offscreen/hidden pause, reduced-motion poster freeze),
  `ResponsiveTerminal.tsx` (ResizeObserver -> cell metrics), one file per
  beat, `ease.ts`
- `web/src/components/landing/HeroSection.tsx` — copy + HeroDemo mount
- `video/src/compositions/ControlRoom.tsx` — Remotion export composition
- `Makefile demo-render` — export lane (mp4 has its silent AAC stripped)

## Layout contract (agents miss what humans see instantly)

Humans catch composition problems at a glance; agents verifying "does it
render / does it click" ship billboard terminals and below-the-fold Send
buttons. These rules are the formalized substitute — check them the way
you check tests:

1. **Demo components scale to the page; the page never scales to the
   demo.** A 64-col terminal at full section width renders billboard
   type. Give media a column, not the section.
2. **Media is constrained by BOTH axes.** Tall media (the phone is
   ~2.26x its width) must cap width by viewport height too, or its
   interactive bottom edge falls below the fold on short/wide windows:
   `width: min(340px, 100%, calc((100vh - <chrome above it>px) * <w/h ratio>))`.
3. **The fold check is part of the verify loop.** Scroll the section to
   the top of a 1440x900 AND a 1800x850 viewport, screenshot exactly one
   viewport, and assert the section's interactive elements (chips, Send)
   are inside it — measure boundingBox in the Playwright script and
   print `send bottom at y=N of H`; don't eyeball alone.
4. **Vertical budget:** at 900px the section gets roughly 830px of
   usable height below the fixed header. Heading + controls + media must
   fit it; tighten heading scale/rhythm before shrinking the media below
   legibility.

## Runbook

Re-record one provider (both geometries, gated, publishes assets):

```bash
./video/scripts/terminal/record-all.sh claude   # or codex
```

Then: inspect the new take's story-beat timings and retune the replay
windows in `video/src/demo/script.ts` (`REPLAY_WINDOWS`) by dumping
states:

```bash
python3 - <<'EOF'
import json
d=json.load(open('video/src/assets/terminal/claude.grid.json'))
pool=d['rowPool']; rt=lambda ri:''.join(r['text'] for r in pool[ri])
for s in d['states'][::8]:
    top=[rt(ri).strip() for ri in s['rows'] if rt(ri).strip()]
    print(round(s['t'],2), top[0][:70] if top else '')
EOF
```

### Browser QA hygiene (paid for 2026-08-12)

Drive a **disposable headless browser**, never a managed `agent-browser-profile`
one. `background`/`watchable` are for interactive, identity-bearing browsing;
borrowing one for repeated UI QA opens a window on David's screen, litters a
SHARED profile with tabs he has to close by hand, and collides with other agents
holding the same profile (`background` refuses to start while `watchable` runs,
and the tempting fallback — driving `watchable` — is the visible one).

The landing page is unauthenticated, so QA needs no identity at all:

```bash
make qa-landing-live                      # local dev server, no instruction run
make qa-landing-live URL=https://longhouse.ai RUN=1   # spends money, uses quota
```

`e2e/scripts/qa-landing-live-demo.mjs` launches its own headless chromium with a
throwaway profile, closes every context in `finally`, and asserts the layout
contract (fold at 1440x900 / 1800x850 / 390x844, terminal fill, no horizontal
overflow) by measurement rather than eyeball. Extend that script instead of
hand-rolling CDP.

Two matching traps when asserting on terminal text: strip ANSI before matching
(the PTY interleaves escapes mid-word) AND strip all whitespace (the TUI wraps
and emits cursor moves mid-phrase, so "all tests passed" arrives as
"all testspassed" and a flag can split across rows).

Verify loop (never skip; this is the vision-check rule):
1. `cd web && bun run build`, then `bunx vite preview --port 4188` and
   Playwright-screenshot `http://localhost:4188/landing` at 1440x900 AND
   390x844. Wait for `.hero-demo-dot`, click each dot to reach each beat
   (steer needs ~4.5s after its dot for send + reaction), LOOK at every
   shot with vision. If the page hangs on the logo spinner, the preview
   proxy's `/api` is hanging — `page.route("**/api/**", r => r.abort())`.
2. `make test-frontend`.
3. Export lane when it matters (composition or recordings changed):
   `cd video && bunx tsc --noEmit && bun run render:control`, extract
   frames per beat with ffmpeg, LOOK at them; `make demo-render` to
   publish mp4/poster/og.

Add a provider: add its providers.yml entry (source pin, auth mode,
sentinel profile, fixture), `fetch.py --only <prov>`, one calibration
take, then wire its grid JSON into the composition.

## Traps (paid for; do not relearn)

Recording:
- **Answer terminal queries.** ink/ratatui TUIs interrogate the terminal at
  startup (DA1/DA2, XTVERSION, CPR, OSC 10/11); unanswered, they stall and
  swallow keystrokes. record.py answers like a plain xterm.
- **Never symlink the scratch cwd.** Claude binds folder-trust to the
  RESOLVED path; a symlinked cwd resurfaces per-edit permission dialogs
  mid-task. Scratch is the real `/tmp/demo-repo`, reuse-guarded by a seed
  marker in `.git/`. The on-screen cwd is a marketing surface: keep it neutral.
- **Model ids on screen are marketing surfaces.** Codex prints its model id
  in the status bar and warns loudly about unknown ids; the id in the codex
  sandbox config must be in its catalog AND current (stale model names on
  the landing page look terrible).
- **Geometry is fixed at record time.** Replay cols x rows must exactly match
  the recording (alt-screen TUIs corrupt otherwise). 100x16 for the big
  steer panel; 64x14 for beat-1 tiles (real text large enough at tile size).
  Claude Code and Codex both render intact at 64x14.
- **Sentinels drift with provider releases.** Claude 2.1.219+ removed
  "esc to interrupt"; the working signal is the token-counter line. Profiles
  live in providers.yml; expect one calibration take after provider updates.
- **The gates are the product.** Canary must prove isolation before any
  take (egress blocked, ~/.claude unreadable, mock reachable); after the
  take, browser attempts / unserved fixture turns / missing sentinels exit
  nonzero. Claude legitimately makes MORE main-lane requests than fixture
  turns (startup warmup + summarizer); the gate checks distinct turns served.
- **srt notes:** allowPty on; AppleEvents OFF is the browser/OAuth blocker;
  keychain unreachable by default; pass `--` before the wrapped command
  (srt re-parses a later `-s` as its own flag). Claude REQUIRES egress to
  api.anthropic.com + platform.claude.com (unauthenticated hello probe,
  process.exit(1) on failure) — the only non-loopback exception.
- **Auth modes are declared, never improvised**: mock (default, zero creds,
  fixture-driven) | api_key (proven non-interactive only) | blocked.
  OpenCode mock routing is unresolved (models.dev catalog); Cursor is
  blocked (closed backend, no base-URL override). Never let a provider
  reach a browser: shims + BROWSER=false stay as telemetry, srt enforces.

Compile/render:
- **No emulator in any frame path** (async writes + out-of-order frames =
  nondeterminism). Compile offline to a grid timeline; render is a pure
  lookup. compile.ts output is deterministic: same cast -> same bytes.
- **agg is a visual oracle only** — its GIF clock drifts ~1s; never use it
  for timing decisions.
- **The steer message and steer window are DERIVED from the recording,
  never hand-written.** compile.ts stamps the exact prompt (from the
  recorder's meta sidecar) plus `promptIdleSec`/`promptTypedSec` anchors
  into each grid JSON; consumers call `recordingPrompt(grid)` /
  `steerWindow(grid)`. This exists because a hand-maintained display copy
  drifted from the footage once (card said one sentence, terminal
  received another). To change the message: change providers.yml's
  prompt and re-record. Only the beat-1 dense-work windows and
  `STEER_END_SEC` remain editorial hand-picks in demo/script.ts.
- **All web animation derives from the clock's tSec** — pure functions of
  time, like Remotion frames. Never mix in stateful CSS keyframe sequences
  for anything that must stay in sync with a recording (the decorative
  steer pulse is the one sanctioned exception).
- **The web hero must never import Remotion.** It consumes the
  `@longhouse/video/demo` subpath only; `TerminalGrid` and `demo/*` stay
  free of Remotion imports so the web bundle stays player-free (~13KB
  gzip lazy chunk, all four recordings included).
- **The marketing page never blocks on the API.** LandingPage only shows
  its loading gate when an auth redirect is actually possible; the
  `/landing` preview route renders immediately even with `/api` dead.
- **The palette is art-directed**, not literal ANSI (warm theme in
  TerminalGrid). Defensible; do not claim pixel-literal colors.
- **Cell metrics:** font size locks to BOTH axes (`min(cellH*0.72,
  cellW/0.62)`); JetBrains Mono is bundled in web so browsers match.

Process:
- The pre-commit end-of-file fixer rewrites grid JSONs mid-commit; when a
  commit reports "files were modified by this hook", `git add` those and
  commit again. The provider-census hook regenerates
  `docs/generated/provider_census.json` when provider literals change —
  include it in the commit.
- **video/src is a web-runtime input.** Deploy, runtime-image, and web
  quality workflows trigger on `video/src/**` (added 2026-08-09 after a
  video-only timing fix silently skipped deploy). If a new workflow
  gates on `web/**`, give it the video paths too.
- **Steer causality:** the pre-send hold frame must show an EMPTY
  composer (see REPLAY_WINDOWS comment). If the terminal ever shows the
  instruction before the card's Send fires, the window start drifted
  past the take's typing phase.
- Casts are committable ONLY from the blank sandbox. The sanitization gate
  greps cast + grid + meta for operator identity; keep it that way.
- Honesty line: the page carries "Demo shows real provider CLIs replayed
  from recordings, with scripted model responses." Copy must not imply
  uniform control depth across providers (capability table is truth).

## Explorations (reference)

The pre-2026-08 approaches live in git history: hand-drawn terminal beats
(rejected: "doesn't look like the real CLIs"), mp4 hero (rejected:
compression blur, 694KB vs crisp DOM), `@remotion/player` running the
composition live in a fixed 16:9 frame (rejected: on a 390px phone the
1920px stage uniform-scales to ~20% and two 100-col terminals become
decoration — the frame was the problem, not the codec), AI-image device
compositions (workshop harness under `workshop/` if layout exploration is
ever needed again). The steer beat's instruction card is a drawn mock and
the send->react link is composited; the known upgrade is one continuous
capture through real Longhouse (launch, remote send, PTY reaction,
timeline entry).
