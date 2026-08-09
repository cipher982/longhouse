# Landing Hero: Live Real-PTY Demo

Use this skill when the task touches the landing hero demo, the terminal
recordings behind it, or the marketing video exports: "update the hero",
"refresh the demo", "re-record the terminals", "the model names are stale",
"add a provider to the demo".

## What the hero is (August 2026)

The hero is NOT a video. The landing page runs the Remotion composition
live as DOM via `@remotion/player` (lazy chunk, ~21KB gzip), so terminal
text renders at native resolution on every screen. The content inside it is
**literal recorded PTY output** from real provider binaries, captured in a
sandboxed rig and replayed as styled text. An mp4/poster/OG export lane
exists only for embeds and social.

Pipeline, end to end:

```text
providers.yml (pinned source + auth mode + sentinels)
  -> fetch.py            stock binary into .sandbox/, sha256-locked
  -> record.py --sandbox real binary under srt isolation, mock LLM on loopback,
                         asciicast v2 + meta sidecar; canary + fail-closed gates
  -> retime.ts           cap idle gaps (timestamps only, never drop events)
  -> compile.ts          offline @xterm/headless replay -> grid timeline JSON
  -> TerminalGrid.tsx    pure React: frame -> binary-search state -> styled runs
  -> ControlRoom.tsx     4-beat composition (Remotion)
  -> web LiveDemo.tsx    @remotion/player runs it live on the landing page
  -> make demo-render    mp4 + poster + og-image (export lane only)
```

## Key files

- `video/scripts/terminal/` — the rig: `providers.yml`, `providers.lock.json`,
  `fetch.py`, `record.py`, `mock_llm.py`, `retime.ts`, `compile.ts`,
  `record-all.sh`, `fixtures/<provider>.json` (canned model turns)
- `video/src/assets/terminal/` — committed casts + compiled grid JSONs
  (`<prov>.…` = 100x16 detail take; `<prov>-tile.…` = 64x14 beat-1 take)
- `video/src/terminal/TerminalGrid.tsx` — the grid renderer (shared)
- `video/src/compositions/ControlRoom.tsx` — beats, captions, phone mock,
  and the REPLAY WINDOW CONSTANTS (take-coupled, see traps)
- `web/src/components/landing/LiveDemo.tsx` — Player wrapper (lazy,
  IntersectionObserver pause, reduced-motion freeze at poster frame)
- `web/src/components/landing/HeroSection.tsx` — copy + LiveDemo mount
- `Makefile demo-render` — export lane (mp4 has its silent AAC stripped)

## Runbook

Re-record one provider (both geometries, gated, publishes assets):

```bash
./video/scripts/terminal/record-all.sh claude   # or codex
```

Then: inspect the new take's story-beat timings and retune the replay
windows in `ControlRoom.tsx` (`REPLAY_START/END_SEC`, `TILES[].startSec`)
by dumping states:

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

Verify loop (never skip; this is the vision-check rule):
1. `cd video && bunx tsc --noEmit && bun run render:control`
2. Extract frames per beat with ffmpeg, LOOK at them with vision.
3. `make demo-render` (exports), page screenshots at 1440x900 + 390x844
   via Playwright against `http://localhost:47200/landing` (the always-on
   preview route; works even authenticated), LOOK at them.
4. `make test-frontend`.

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
- **No emulator in the Remotion frame path** (async writes + out-of-order
  frames = nondeterminism). Compile offline to a grid timeline; render is a
  pure lookup. compile.ts output is deterministic: same cast -> same bytes.
- **agg is a visual oracle only** — its GIF clock drifts ~1s; never use it
  for timing decisions.
- **Replay windows are take-coupled.** ControlRoom's window constants
  reference absolute seconds in specific takes. Every re-record: dump
  states, re-pick windows, re-verify frames.
- **The palette is art-directed**, not literal ANSI (warm theme in
  TerminalGrid). Defensible; do not claim pixel-literal colors.
- **Cell metrics:** font size locks to BOTH axes (`min(cellH*0.72,
  cellW/0.62)`); JetBrains Mono is bundled in web so browsers match.

Process:
- The pre-commit end-of-file fixer rewrites grid JSONs mid-commit; when a
  commit reports "files were modified by this hook", `git add` those and
  commit again.
- Casts are committable ONLY from the blank sandbox. The sanitization gate
  greps cast + grid + meta for operator identity; keep it that way.
- Honesty line: the page carries "Demo shows real provider CLIs replayed
  from recordings, with scripted model responses." Copy must not imply
  uniform control depth across providers (capability table is truth).

## Explorations (reference)

The pre-2026-08 approaches live in git history: hand-drawn terminal beats
(rejected: "doesn't look like the real CLIs"), mp4 hero (rejected:
compression blur, 694KB vs crisp DOM), AI-image device compositions
(workshop harness under `workshop/` if layout exploration is ever needed
again). The steer beat's phone is still a drawn mock and the send->react
link is composited; the known upgrade is one continuous capture through
real Longhouse (launch, remote send, PTY reaction, timeline entry).
