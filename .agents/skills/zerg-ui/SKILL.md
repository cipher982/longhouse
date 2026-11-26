---
name: zerg-ui
description: Look at a rendered frame of a Longhouse UI change (web fixture captures, iOS simulator shots, SwiftUI previews) and run simlab, the real-pipeline iOS recovery journeys, without a physical phone. Use for any UI change, simulator or network-loss experiments, transcript jump bugs, and visual regression.
---

# Look at it

A UI change is not done until you have looked at a rendered frame. Tests prove an
element exists, not that "fable 5 1" should read "fable 5.1", that a banner has no
bottom edge over scrolling text, or that a preview rendered blank. `Read` a PNG and
you see it. Say what you saw in the handoff, with the artifact path. When a change
adds a served field, add it to the fixtures so the captured frame shows it.

## Pick the render path

| You changed | Command | Runs | Shows |
|---|---|---|---|
| SwiftUI view and its states | `make ios-previews` | wisp (dispatched) | Every `#Preview`, dark and light, no server |
| iOS screen on real data | `make sim-deploy SESSION=<id>` then `make sim-shot LABEL=<name>` | bench: `scripts/ops/bench.sh run '...'` | Real pipeline, fonts and chrome |
| iOS screen in a fixed state | `make ios-ui-shot TEST=<Suite>/<test>` | wisp (dispatched) | Frame a UI test attaches, exported to PNG |
| iOS ingest and recovery | `make simlab-run` | wisp (dispatched); `simlab.py up/run` on the bench to iterate | Scratch host, Machine Agent and app; render convergence, network recovery |
| iOS transcript jumping while content loads or streams | `make ios-transcript-stability` (`ONLY=a,b`, `FILM=1`, `DARK=1`) | here, shipped `transcript.html` in Playwright WebKit | Anchor drift and pinned-bottom gap as numbers with budgets. Spec: `control-plane/docs/specs/ios-transcript-stability.md`. Add a scenario in `scripts/qa/ios-transcript-stability/scenarios.ts` for each jump found |
| iOS chat opening on real data | `scripts/qa/ios_open_film.py --udid <sim> --session <id>` after `sim.sh deploy` | bench | Video, time to first content and settle, big-jump count, contact sheet |
| One web page or scene | `make ui-capture PAGE=<page> SCENE=<scene>` | here, fixture scenes need nothing running | PNG plus accessibility snapshot |
| Every web page and state | `make ui-gallery` (`ONLY=` narrows) | here | One `index.html` sheet under `/tmp/agents/ui-gallery/<stamp>/` at 1440x900, 2000x1200 and phone |
| A sent message on the web (optimistic row to transcript row) | `make web-send-handoff` (`ONLY=`, `SWEEP=1` for 72 generated orderings); CI runs the recorded ones | here, scripted server, no provider | Per frame: blank, doubled, remounted, reshaped or moved. Add each new send glitch as a scenario in `scripts/qa/web-send-handoff.ts` with the host and session it came from |
| Every menu, popover and dialog | `make ui-sweep` (`ONLY=`, `JOBS=`); CI runs it | here | Fails when an overlay lands off-screen, covered or clipped. `ui-capture ... --sweep` checks one scene (`SWEEP_SHOTS=1` saves a PNG per overlay). A control that exists in only one session mode needs a fixture scene |

Look at more than one when the change spans surfaces: the simulator shot proves
the data path, the fixture shot proves layout at a known state.

**Dispatched targets** (`ios-previews`, `ios-ui-shot`, `simlab-run`, `test-ios`,
`menubar-harness`) run on wisp, refuse a dirty or unpushed worktree, and need the
laptop's `gh`. Their rules are in `zerg-testing`. The dispatcher prints the run URL
and `source=<sha>` and downloads the run's `artifacts/` tree to
`artifacts/test-isolation/<id>/evidence/<target>/project/`. Paths below for those
targets exist only after that download.

## Own the QA lifecycle

`zerg-testing` ("Proof-run cleanup") sets the rules. For UI work specifically:

- Before launching, record simulator UDIDs and boot state, owned processes and
  ports, browser tabs and scratch roots. Put teardown in `finally` or an exit trap
  and run it as soon as the visual check finishes.
- A simulator you created: `xcrun simctl shutdown <udid>`, then
  `xcrun simctl delete <udid>`, and confirm its window closed. Never delete a
  pre-existing simulator; restore its boot state and close only windows you opened.
  Do not quit Simulator or kill browsers globally.
- Every command that can outlive the shell (`make test-ios`, `xcodebuild`, `simlab`,
  `sim-deploy`, `sim-shot`, log followers, local servers) is an owned resource:
  record its PID or process group before starting and run it under a trap.
- After an interruption, clean the exact process group first. Do not start a
  replacement while an older `xcodebuild` or app is alive.
- Provider verification uses an isolated `HOME` and `--no-session` or a disposable
  session directory, and you remove any native session it created.
- Keep the screenshots and receipts, not the environment. Before handoff, check
  process names and PIDs and `xcrun simctl list devices`.

## iOS

### Autonomous recovery dogfood (simlab)

Use this before asking anyone to reproduce transcript-delivery or reconnect
problems on a phone.

```bash
make simlab-run                                    # dispatched, clean pushed revision
make simlab-run SCENARIOS="interrupted-client-recovery client-network-recovery"
make test-ios-helper                               # harness verdict, cleanup and TCP relay
scripts/ops/bench.sh run 'python3 scripts/qa/simlab.py up --build && python3 scripts/qa/simlab.py run --deploy interrupted-client-recovery; python3 scripts/qa/simlab.py down'
```

`scripts/qa/simlab.py` builds an isolated provider `HOME`, a scratch SQLite
Runtime Host, a real Machine Agent, synthetic Claude transcripts and the real
simulator app. `simlab_proxy.py` puts a loopback TCP relay in the client path only,
so the engine keeps shipping while the client is offline. Scenarios are hidden
**Shadow** imports, not Console launches; do not use the Console session-creation
API to register them. Do not run two simlab jobs at once (shared `current` state
and simulator).

- `simlab up` runs the newest binary across the `release` and `ci` cargo profiles,
  which may not match `HEAD`. A stale engine drops newer behavior silently.
  Compare `longhouse-engine --version` with the commit under test, or use
  `up --build`, before believing a negative result.
- A pass needs more than server progress: the exact final assistant reply must
  reach the server projection, then the newest session-open's WebKit render must
  carry its `prose:<event.id>`. Network recovery also needs an observed
  disconnect, a fresh stream connection after the offline boundary, and no new
  session-open.
- Read `artifacts/simlab/current/summary.json` (on the bench, under
  `/tmp/agents/bench/<job>/`; for a dispatched run, in the downloaded evidence) for
  screenshots, logs, source transcript, projection, timings and verdict. Open the
  PNGs; a render callback alone does not prove a usable layout. `current` is
  overwritten by the next run and scratch evidence under
  `/tmp/longhouse-simlab/<stamp>/artifacts` is temporary: record conclusions in
  session notes, not raw logs.
- Keep one stable log capture; do not replace a failed follower with a sliding
  OSLog window. Teardown checks process birth identity and group ownership; never
  signal a PID only because an old state file names it.
- Limits: termination is not iOS background suspension, TCP loss is not a
  cellular radio, and synthetic Shadow history does not qualify real provider
  execution, Helm or Console steering, APNs or Live Activities. No phone or
  provider credentials are needed.

### Simulator on real data

```bash
scripts/ops/bench.sh run 'make sim-deploy SESSION=<id> && make sim-shot LABEL=<name>'
```

Use the bench; the laptop only when the bench is unreachable, and say so. The shot
lands in `/tmp/agents/bench/<stamp>/` (on the laptop, `artifacts/sim/`). It signs
in with this machine's device token against its linked Runtime Host
(`SIM_SERVER_URL` and `SIM_AUTH_TOKEN` override). `SESSION` is the Longhouse id
(`GET /api/agents/sessions/<id>` returns it next to the provider's id). The app
opens at the tail, so a footer from an earlier reply is above the fold, and nothing
scrolls the simulator from the shell; use a fixture shot for a frame you control.

### Fixture frame with a screenshot

```bash
make ios-ui-shot TEST=SessionChatUITests/testTurnFooterRendersUnderTheProviderReply
```

Write the test like those in `ios/Tests/LonghouseIOSUITests/SessionChatUITests.swift`:
open a chat fixture (`launchChatFixture(eventCount:)` or a named fixture in
`ios/Sources/LonghouseApp/Fixtures/ChatUITestWorkspaceClient.swift`), wait for the
element, then `attachScreenshot(app, named:)`. Fixture tests share one launched app
and switch fixtures in process (`FixtureApp`), so open fixtures through it rather than
`XCUIApplication().launch()`. The merge gate drops these screenshots; `ios-ui-shot`
keeps them. Transcript text is exposed as `staticTexts` (query with
`NSPredicate(format: "label CONTAINS %@", ...)`). Add any new served field to the
fixture too. XCTest failure screenshots are exported as well. Only the
`LonghouseSmoke` scheme carries the UI test target; the target handles that.

### SwiftUI previews

`make ios-previews` renders every `#Preview`. Add one in `*Previews.swift` for each
new view, in dark and light when it uses materials or secondary text. Put the view
in the shell it lives in (a `NavigationStack` with a scrolling body, as
`SessionScreenPreview` and `ProviderChromePreview` do in
`Previews/SessionViewPreviews.swift`); a bare view over the transparent harness
canvas renders as a blank PNG with one divider.

### Transcript rows are HTML

The phone transcript is a `WKWebView` document. Its TypeScript and CSS live in
`web/src/embeds/ios-transcript/` (tested by `make test-frontend`).
`make generate-ios-transcript` builds the checked-in
`ios/Resources/Transcript/transcript.html`; `make validate` fails when it is stale,
so rebuild and commit both together. Each item is one root element and the
reconciler keys on `JSON.stringify(item)`: put a footer or badge inside the item's
root, and a new `WebTranscriptPayloadItem` field re-renders the row. The
diagnostics line `transcript-benchmark-status` reports `renders=`, `rows=` and
`latest=`; `renders=1 rows=1` after a send means the DOM never re-rendered, so look
there before blaming a style.

### Phone, bench and profiling

- A real phone is for timing and device-only behavior (APNs, Live Activity,
  cellular). `make phone-deploy` installs over the Wi-Fi tunnel; a locked phone
  takes the install and refuses the launch. Unlocked: `phone.sh console
  [--seconds N]` streams stdout and OSLog, `phone.sh profile` records Instruments.
  Testers use TestFlight (`zerg-ship`). Never tour David's own phone.
- `scripts/ops/bench.sh run <cmd>` runs on wisp (mirror, lock, run, copy
  `$BENCH_OUT` to `/tmp/agents/bench/<stamp>/`).
- `~/bench/corpus/home` on wisp holds 100 recent Claude transcripts; `simlab.py up
  --build --seed-corpus ~/bench/corpus/home` ships them through a real Machine
  Agent into the scratch host, never David's instance. Refresh by rsyncing newer
  `~/.claude/projects` files there.
- `scripts/ops/ios_tour.sh` (`TOUR_TARGET=device` for a test phone) runs the
  read-only `testLiveDogfoodTour` under Instruments and prints timings plus
  `trace_summary.py` output. Example: `bench.sh run 'python3 scripts/qa/simlab.py
  up --build --seed-corpus ~/bench/corpus/home && scripts/ops/ios_tour.sh; python3
  scripts/qa/simlab.py down'`.
- `sim.sh profile` or `phone.sh profile [--template "Animation Hitches"]
  [--seconds 20]` prints CPU by thread, top symbols, hangs and hitches. Simulator
  Debug numbers find hotspots; judge speed on the phone.
- Keep `sim.sh` and `phone.sh` builds at zero compiler warnings (a "nearly matches
  optional requirement" warning once meant WebKit never called a delegate method;
  check with `otool -oV <app>/Longhouse.debug.dylib | grep <selector>`). For
  console noise, filter to `subsystem == "ai.longhouse.ios"` (`sim.sh logs`); most
  yellow and red lines are Apple frameworks.

## Web

Render before reasoning from CSS or a pasted screenshot:

```bash
make ui-capture PAGE=session-detail SCENE=session-detail-stress
make ui-capture PAGE=session-detail SCENE=session-tones      # composer in every live state
make ui-capture PAGE=timeline SCENE=timeline-card-stress VIEWPORT=mobile
make ui-capture PAGE=session-detail SCENE=session-prose-idle VIEWPORT=2000x1200 CSS_VARIANT=terminal   # variants in scripts/ui/css-variants/
make qa-ui-workbench          # timeline and session fixtures, desktop and mobile; qa-* goal, output under artifacts/test-isolation/<run-id>/files/
make ui-capture ALL=1         # demo data; needs `make dev-demo` (backend on :47300)
```

- Output: `artifacts/ui-capture/<timestamp>/` with `<page>.png`, `<page>-a11y.json|yml`,
  `console.log`, `manifest.json`, and `trace.zip` unless `NO_TRACE=1`. A fixture
  scene starts Vite on :47200 if nothing listens and stops it afterwards (an
  existing Vite is left alone).
- To measure instead of eyeball, add `PROBE='.a,.b'`: `<page>-probe.json` gives each
  selector's bounding box and key computed styles, which shows an overridden rule.
- `WHEEL_MAP=1` (with `VIEWPORT=1700x900` for the three-column session layout)
  sends a wheel event at every 40px cell and writes `<page>-wheelmap.txt|json`, a
  letter per cell naming the scroller that moved, `.` where nothing did. Dead cells
  beside a scroller (gutters, rails) are the bug; over a fixed header or composer
  they are intended. A scroller narrower than its pane leaves dead gutters: make it
  span the pane and center the column with padding.
- Fixture scenes answer API calls from Playwright routes. Add new served fields to
  `scripts/ui-fixtures/*.ts`. The session context drawer is closed in captures.
- Fixture scenes (nothing running): `session-detail-stress`, `session-tones`,
  `session-resume`, `session-stale-observation`, `session-prose-idle`,
  `session-console`, `timeline-card-stress`. Demo-backend scenes: `demo`, `empty`,
  `onboarding-modal`, `missing-api-key`.
- Visual regression (CI): `make qa-ui-baseline`, `make qa-ui-baseline-mobile`,
  `make qa-ui-baseline-update` to accept, `SKIP_LLM=1 make qa-visual-compare`.
- Public pages need no identity: use a disposable headless browser (`browser`
  skill; `make hero-frames` and `e2e/scripts/qa-landing-live-demo.mjs` do).
  `make qa-remote-scene` writes a frame bundle for a separate vision agent to
  describe.

## How to look

- `Read` the PNG and compare with the reference (provider terminal, design note,
  web version of the row).
- Check the words: truncation, number formatting, dates on the wrong day, "done"
  with no time.
- Check edges: anything over scrolling content needs its own bottom edge;
  right-aligned items need the same inset as neighbors.
- Check both themes for anything with materials or secondary text.
- A blank or near-empty PNG is a harness problem, not a pass; fix the preview
  shell or fixture until the frame shows the thing.

## Gotchas

- The shell guard blocks `rm -rf` and `>` into repo paths: write outputs under
  `artifacts/` or `mktemp -d`, and use the Write tool for files.
- `xcresulttool export attachments` names files by uuid; `ios-ui-shot` renames them
  to the attachment name.
- Output directories are gitignored; quote the path, do not commit PNGs.
