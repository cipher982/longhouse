---
name: landing-hero
description: Work on the landing page hero demo (real recorded provider terminals replayed as DOM), re-record its terminal takes, fix stale model names on screen, add a provider to the demo, or run the hero frame and layout QA. Also covers the Remotion export lane (make demo-render).
---

# Landing hero: recorded real-PTY demo

The public site is a placeholder during the employer standoff, but the hero code
still builds and is tested in this repo (`/landing` renders it locally). Do not
publish it or point QA at `longhouse.ai`.

## What it is

The hero is DOM, not a video. `HeroDemo` (a lazy chunk) runs one looping rAF
clock over an absolutely positioned stage. Layer rects come from the measured
width (`heroLayout.ts`), so layers move between layouts. The content is literal
PTY output from real provider binaries, recorded in a sandbox and replayed as
styled text. There is one story, identical on every loop:

1. **agents**: Claude Code, Codex and OpenCode tiles, each on a small task in the same demo repo.
2. **timeline**: the tiles shrink into rows in the web timeline's grammar (glyph, title = prompt, preview = final reply, mode chip).
3. **handoff**: the Claude row grows back into its terminal. An iOS-styled screen sends a follow-up, a relay path carries it, and the same recorded Claude session (`claude-handoff`, two turns) does the work while the phone mirrors the replies.

`video/src/demo/story.ts` is the narrative source. Every visible string derives
from a recording's meta (prompts) or its fixture (replies), and `HERO_TIMING`
holds the only hand-picked numbers. The web imports it as `@longhouse/video/demo`
(a path alias in `web/vite.config.ts` and `tsconfig.base.json`; no Remotion).
`video/src/demo/script.ts` feeds only the Remotion export lane (`ControlRoom`),
which still tells the older four-beat story and needs porting before the next
`make demo-render`.

```text
providers.yml (pinned source, auth mode, sentinels)
  -> fetch.py             stock binary into .sandbox/, sha256-locked
  -> record.py --sandbox  real binary under srt isolation, mock LLM on loopback,
                          asciicast v2 + meta sidecar; canary and fail-closed gates
  -> retime.ts            cap idle gaps (timestamps only, never drops events)
  -> compile.ts           offline @xterm/headless replay -> grid timeline JSON
  -> demo/story.ts        hero narrative
  -> TerminalGrid.tsx     pure React: time -> state lookup -> styled runs
  |-> web hero            useDemoClock + HeroDemo stage
  `-> ControlRoom.tsx     Remotion export (demo/script.ts)
```

## Files

| Path | What |
|---|---|
| `video/scripts/terminal/` | the rig: `providers.yml`, `providers.lock.json`, `fetch.py`, `record.py`, `mock_llm.py`, `retime.ts`, `compile.ts`, `record-all.sh`, `fixtures/<provider>.json` (canned model turns) |
| `video/src/assets/terminal/` | committed casts and compiled grids. `<prov>` = 100x16 take, `<prov>-tile` = 64x14 tile take, `claude-handoff` = 64x14 two-turn take. The web hero uses only 64-column takes: `claude-handoff`, `codex-tile`, `opencode-tile` |
| `video/src/demo/` | `story.ts` (hero narrative), `script.ts` (export lane), `recordings.ts` (typed grid exports) |
| `video/src/terminal/TerminalGrid.tsx` | grid renderer, shared by both lanes; no Remotion imports |
| `video/src/compositions/ControlRoom.tsx` | Remotion export composition |
| `web/src/features/marketing/hero-demo/` | `HeroDemo.tsx` (stage, relay, caption), `heroLayout.ts`, `HeroTimeline.tsx`, `HeroPhone.tsx`, `useDemoClock.ts` (offscreen pause, reduced-motion freeze, `?demoT=` freeze), `ResponsiveTerminal.tsx` |
| `web/src/features/marketing/landing/HeroSection.tsx` | hero copy and `HeroDemo` mount. The route is prerendered, so static HTML and hydration show `HeroDemoFallback` and `AfterHydration` mounts the lazy `HeroDemo` |
| `web/src/features/marketing/landing/SteerPlayground.tsx`, `hero-demo/LiveDemo.tsx` | separate below-the-fold live sandbox where a visitor sends one real instruction. The hero stays the recorded story |
| `scripts/ui-fixtures/landingShowcase.ts` | curated data for `make landing-screenshots` |

Related targets: `make landing-screenshots` regenerates the "Every session"
showcase images from the current web UI using fixtures where every API call is
mocked or sealed. Re-run it after timeline or session-detail UI changes, bump
the `?v=` stamps in `ProductShowcase.tsx`, and run
`bun scripts/generate/generate-og-image.mjs` for the social card.
`make demo-render` renders the export lane (mp4 with silent AAC stripped, poster, og).

## Layout contract

Check these by measurement, since agents tend to ship billboard terminals and
below-the-fold Send buttons that a human spots at a glance.

1. **Demo components scale to the page, not the reverse.** A 64-column terminal at full section width renders billboard type. Give media a column.
2. **Constrain media on both axes.** Tall media (the phone is about 2.26x its width) must cap width by viewport height too: `width: min(340px, 100%, calc((100vh - <chrome above>px) * <w/h ratio>))`.
3. **Fold check.** At 1440x900 and 1800x850, scroll the section to the top, screenshot one viewport, and assert its interactive elements (chips, Send) are inside it. Print `send bottom at y=N of H` from the script's boundingBox.
4. **Vertical budget.** At 900px the section has about 830px below the fixed header. Heading, controls and media must fit. Tighten heading scale before shrinking media below legibility.

## Re-record

One provider, both geometries, gated, assets published:

```bash
./video/scripts/terminal/record-all.sh claude   # or codex
```

Hero takes use one geometry and their own fixture. Repeat `--prompt` for
follow-up turns in one session (`compile.ts` stamps `meta.turns`):

```bash
cd video/scripts/terminal && uv run record.py --sandbox --provider claude \
  --cols 64 --rows 14 --fixture fixtures/claude-handoff.json \
  --prompt "Fix the off-by-one bug ..." --prompt "Add a test for an empty shelf, then rerun the tests" \
  --out .takes/claude-handoff/take.cast
# then retime.ts, compile.ts, the sanitization grep from record-all.sh, and copy to assets
```

`story.ts` derives most anchors from the recording and throws if a reply never
appears. To inspect timings, dump a grid's states (`states[].t` and `rows`
indexing `rowPool`) from `video/src/assets/terminal/<take>.grid.json`.

**Add a provider.** Add its `providers.yml` entry (source pin, auth mode,
sentinel profile, fixture), run `fetch.py --only <prov>`, record one calibration
take, then wire its grid JSON into the composition.

## Verify

1. `make hero-frames`, then look at both contact sheets and the fold captures. It freezes the clock (`/landing?demoT=<sec>`), steps one loop, and writes PNGs, a labelled sheet per viewport, and `frames.json` (visible layers with opacity, box and text, for non-vision agents) under `artifacts/hero-frames/<stamp>/`. For one transition: `bun scripts/qa/hero-frames.ts --step=0.15 --from=4.4 --to=7.7`. For motion, record real time with a Playwright `recordVideo` context and look at sampled frames. For timing assertions freeze the clock instead of sleeping.
2. `make test-frontend`.
3. `cd e2e && node scripts/qa-landing-live-demo.mjs` against a local dev server (default `http://localhost:5173/landing`). It launches its own headless chromium with a throwaway profile, closes every context in `finally`, and asserts the layout contract (fold at 1440x900, 1800x850 and 390x844, terminal fill, no horizontal overflow) by measurement. Extend it instead of hand-rolling CDP. `--run` sends a real instruction through the live sandbox and spends quota, so skip it unless the sandbox changed. `make qa-landing-live` wraps the script but the isolation dispatcher refuses `qa-*` goals on a developer machine without `--live --image`, so run the script directly.
4. Export lane, only when the composition or recordings changed: `cd video && bun install && bunx tsc --noEmit && bun run render:control` (`video/` is its own Bun project, outside the root workspaces). Extract frames per beat with ffmpeg and look at them, then `make demo-render`.

When mocking `/api` in a browser script, abort by URL predicate
(`url.pathname.startsWith("/api/")`). A `**/api/**` glob also matches Vite's
`src/shared/api/*` modules and blanks the page, and the dev proxy otherwise
forwards `/api` to a real instance.

Use a disposable headless browser for QA, never a shared `agent-browser-profile`
one: it opens a window on the owner's screen, leaves tabs in a shared profile
and collides with other agents holding it.

When asserting on terminal text, strip ANSI (the PTY interleaves escapes
mid-word) and all whitespace (TUIs wrap and emit cursor moves mid-phrase, so
"all tests passed" arrives as "all testspassed").

## Traps

Recording:

- **Answer terminal queries.** ink and ratatui TUIs ask the terminal for DA1, DA2, XTVERSION, CPR and OSC 10/11 at startup. Unanswered, they stall and swallow keystrokes. `record.py` answers like a plain xterm.
- **Never symlink the scratch cwd.** Claude binds folder trust to the resolved path, and a symlinked cwd brings back per-edit permission dialogs. Scratch is the real `/tmp/demo-repo`, guarded by a seed marker in `.git/`. The on-screen cwd is public copy, so keep it neutral.
- **Model ids on screen are public copy.** Codex prints its model id in the status bar and warns on unknown ids. The id in the codex sandbox config must be in its catalog and current.
- **Geometry is fixed at record time.** Replay cols x rows must match the recording or alt-screen TUIs corrupt. 100x16 for the big steer panel, 64x14 for tiles. Claude Code and Codex both render intact at 64x14.
- **Follow-up turns: never hold on the idle anchor.** After a turn, Claude Code fills its empty composer with a prompt suggestion, and the mock answers that request with the next scripted reply. The idle frame then shows the future as ghost text. Hold on the previous turn's final-reply frame instead (`HANDOFF.holdSec` in `story.ts`). The pre-send hold frame must show an empty composer.
- **The sandbox refuses symlinked binaries (exit 126).** In a worktree, copy `.sandbox/<provider>` and `.sandbox/srt` from the primary checkout.
- **Sentinels drift with provider releases.** Claude 2.1.219 removed "esc to interrupt"; the working signal is the token-counter line. Profiles live in `providers.yml`. Expect one calibration take after a provider update.
- **The gates are the product.** The canary must prove isolation before any take (egress blocked, `~/.claude` unreadable, mock reachable). After the take, browser attempts, unserved fixture turns or missing sentinels exit nonzero. Claude makes more main-lane requests than fixture turns (warmup, summarizer), so the gate counts distinct turns served.
- **srt:** allowPty on; AppleEvents off is the browser and OAuth blocker; keychain unreachable by default; pass `--` before the wrapped command. Claude needs egress to `api.anthropic.com` and `platform.claude.com` (an unauthenticated hello probe that exits 1 on failure), the only non-loopback exception.
- **Auth modes are declared, never improvised:** `mock` (default, no credentials, fixture-driven), `api_key` (only where proven non-interactive), `blocked`. OpenCode records in the mock lane at 64x14. Cursor is blocked (closed backend, no base-URL override). Never let a provider reach a browser; shims and `BROWSER=false` remain as telemetry and srt enforces.
- Casts are committable only from the blank sandbox. The sanitization gate greps cast, grid and meta for operator identity.

Compile and render:

- **No emulator in any frame path.** Async writes and out-of-order frames cause nondeterminism. Compile offline to a grid timeline and render as a pure lookup. `compile.ts` is deterministic: same cast, same bytes.
- **`agg` is a visual oracle only.** Its GIF clock drifts about a second.
- **Derive the steer message and window from the recording.** `compile.ts` stamps the exact prompt from the recorder's meta sidecar plus `promptIdleSec` and `promptTypedSec` into each grid. Consumers call `recordingPrompt(grid)` and `steerWindow(grid)`. To change the message, change the prompt in `providers.yml` and re-record. `HeroDemo.test.tsx` asserts titles are the recorded prompts and the follow-up differs from the finished task.
- **All web animation derives from the clock's `tSec`**, as pure functions of time. Do not mix in stateful CSS keyframe sequences for anything that must stay in sync with a recording.
- **The web hero never imports Remotion.** `TerminalGrid` and `demo/*` stay Remotion-free. Remotion is not in the root install, so a stray import fails the web build.
- **The marketing page never blocks on the API.** `LandingPage` shows its loading gate only when an auth redirect is possible, so `/landing` renders even with `/api` dead.
- **The palette is art-directed**, a warm theme in `TerminalGrid`, not literal ANSI. Do not claim pixel-literal colors.
- **Cell metrics:** font size locks to both axes (`min(cellH*0.72, cellW/0.62)`). JetBrains Mono is bundled so browsers match.

Process:

- The pre-commit end-of-file fixer rewrites grid JSONs mid-commit. When a commit reports "files were modified by this hook", `git add` them and commit again. The provider-census hook regenerates `docs/generated/provider_census.json` when provider literals change; include it.
- `video/src/**` is a web-runtime input. Deploy, runtime-image and web quality workflows trigger on it. A new workflow that gates on `web/**` needs the video paths too.
- **Mobile fold:** on phones the subhead's later sentences hide and the curl block renders below the demo (`landing-hero-secondary` grid area) so the demo starts above the fold. Keep that when editing hero copy.
- **Honesty line:** the page says real provider CLIs are replayed from recordings with scripted model responses (`HeroSection.tsx`). Copy must not imply uniform control depth across providers; the capability table is the truth.

## Rejected approaches

Hand-drawn terminal beats ("doesn't look like the real CLIs"), an mp4 hero
(compression blur), procedural later loops (a synthetic terminal with no
splash, composer or status line read as fake), `@remotion/player` in a fixed
16:9 frame (on a 390px phone the stage scales to about 20% and the terminals
become decoration), and AI-image device compositions (a harness for that is in
`workshop/`). The handoff phone is a drawn replica of the iOS app and the
send-to-react link is composited. The known upgrade is one continuous capture
through real Longhouse: launch, remote send, PTY reaction, timeline entry.
