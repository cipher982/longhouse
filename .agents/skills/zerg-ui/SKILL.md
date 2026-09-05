---
name: zerg-ui
description: Longhouse visual QA and autonomous iOS dogfooding. Use for UI changes, simulator recovery, network-loss experiments, and client-render verification without a physical phone. Covers simlab real-pipeline journeys, iOS screenshots/previews, and web fixture captures.
---

# Zerg UI: look at it

A UI change is not done until you have looked at a rendered frame of it.
Tests prove behavior (an element exists, a label contains a string); they do
not see "fable 5 1" where "fable 5.1" belongs, a banner with no bottom edge
over scrolling text, or a preview that rendered blank. Every one of those got
past a green suite in this repo. You have vision: `Read` a PNG and it is
shown to you. Use it.

## Pick the render path

| You changed | Render it with | Cost | Sees |
| --- | --- | --- | --- |
| A SwiftUI view or its states | `make ios-previews` | ~4 min, renders every `#Preview` | Component in dark/light, edge states, no server |
| iOS screen against real data | `make sim-deploy SESSION=<id>` then `make sim-shot LABEL=<name>` | ~3 min build, then seconds per shot | The real pipeline: hosted data, real fonts, real chrome |
| iOS screen in a deterministic state | `make ios-ui-shot TEST=<Suite>/<test>` | ~2 min | A fixture-driven frame the UI test attaches, exported to PNG |
| iOS ingest/recovery without a phone | `make simlab-run` | six real-client journeys | Scratch Runtime Host + Machine Agent + iOS app; client-render convergence and network recovery |
| Web page or row | `make ui-capture PAGE=<page> SCENE=<scene>` | seconds once Vite runs | Playwright screenshot plus accessibility snapshot |

Look at more than one when the change spans surfaces. The simulator shot
proves the data path; the fixture shot proves the layout at a known state.

## iOS

### Autonomous recovery dogfood (simlab)

Use this before asking David to reproduce transcript-delivery or reconnect
problems on his phone. Run from the product repo on a Mac with Xcode and a
simulator:

```bash
make simlab-run
make simlab-run SCENARIOS="interrupted-client-recovery client-network-recovery"
make test-ios-helper
```

`scripts/qa/simlab.py` owns the experiment: isolated provider HOME, scratch
SQLite Runtime Host, real Machine Agent, synthetic Claude transcripts, and
the actual simulator app. `simlab_proxy.py` places a loopback TCP relay only
in the client path; the engine keeps shipping while the client is offline.
Do not run concurrent simlab jobs against its shared `current` state/simulator.

Six scenarios: open imported history; live appended turns; abandon/resend;
malformed/split/delayed transcript input; terminate/reopen after new output;
network loss and reconnect without relaunch. They are hidden **Shadow**
imports via the existing provider-evidence namespace, not Console launches.
Do not use the Console-only session-creation API to register a Shadow proof.

**A pass is stronger than server progress:** the exact final assistant reply
written to the source must reach the server projection, then the newest
session-open's successful WebKit render must carry its `prose:<event.id>`.
Duplicate early renders or a partial head containing all user prompts cannot
pass. Network recovery additionally requires an observed disconnect, a fresh
stream connection after the offline boundary, and no new session-open.
`source_revision` is optional benchmark attribution (normally `-1` here), not
a production freshness requirement.

Read `artifacts/simlab/current/summary.json` for links to each unique scratch
run's screenshots, app/server/engine/relay logs, source transcript, projection,
recovery checkpoints, timings, and verdict. Inspect the PNGs: a rendered
callback alone does not prove a usable layout. `current` is overwritten by the
next run; scratch evidence lives under `/tmp/longhouse-simlab/<stamp>/artifacts`
and is temporary, not a durable archive. Persist conclusions/review IDs in
session notes rather than committing raw logs or screenshots.

Failures must retain verdicts and available evidence. Do not replace a failed
log follower with a sliding OSLog window: recovery boundaries depend on one
stable capture. Teardown checks process birth identity and group ownership;
never signal a PID merely because an old state file names it.

**Limits:** termination is not iOS background suspension; TCP loss is not a
cellular-radio simulation; synthetic Shadow history does not qualify real
provider execution, Helm/Console steering, APNs, or Live Activities. No physical
phone or provider credentials are required. Contributor-facing instructions
also live in the root `CONTRIBUTING.md` Tests section.

### Simulator on real data
```bash
make sim-deploy SESSION=<longhouse-session-id>   # Debug build, install, sign in headlessly, open the session
make sim-shot LABEL=turn-footer                   # artifacts/sim/<timestamp>-turn-footer.png
```
Signs in with this machine's device token against its linked Runtime Host
(`SIM_SERVER_URL` / `SIM_AUTH_TOKEN` override). The session id is the
Longhouse id (`GET /api/agents/sessions/<id>` returns it next to the
provider's own id). The app opens at the tail: a live session shows the turn
in progress there, so a footer or banner that belongs to an earlier reply is
above the fold. Deep links open a session, not an event, and nothing scrolls
the simulator from the shell; use a fixture shot for a frame you control.

### Fixture-driven frame with a screenshot
```bash
make ios-ui-shot TEST=SessionChatUITests/testTurnFooterRendersUnderTheProviderReply
# artifacts/ios-ui-shot/<timestamp>/turn-footer_0_<uuid>.png
```
Write the test like the ones in `ios/Tests/LonghouseIOSUITests/SessionChatUITests.swift`:
launch a chat fixture (`launchChatFixture(eventCount:)` or a named fixture from
`ChatUITestFixtureView.swift`), wait for the element, then
`add(XCTAttachment(screenshot: app.screenshot()))` with `lifetime = .keepAlways`.
WebKit exposes transcript text as `staticTexts`, so a DOM footer is
queryable with `staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "Worked for"))`.
The fixture stamps every assistant reply with a turn end and every send with
a Longhouse origin; put new served fields into the fixture too, so the frame
the test sees is the frame the app will show. XCTest's own failure
screenshots are exported as well, so a failing run still leaves a frame.

Only the `LonghouseSmoke` scheme carries the UI test target; the `Longhouse`
scheme refuses `-only-testing:LonghouseIOSUITests/...`. The target handles that.

### SwiftUI previews
```bash
make ios-previews        # artifacts/ios-previews/<timestamp>/<File>_0_<Preview name>.png
```
Add a `#Preview` in `*Previews.swift` for every new view, in dark and light
when it uses materials or secondary text. Put the view in the shell it
really lives in (a `NavigationStack` with a scrolling body, the way
`SessionScreenPreview` and `ProviderChromePreview` in
`SessionViewPreviews.swift` do): a bare view over the harness's transparent
canvas renders bar materials and secondary text as nothing, and you get a
blank PNG with one divider on it.

### Transcript rows are HTML
The phone transcript is a `WKWebView` rendered by the JS in
`WebTranscriptView.swift`. Each item renders as one root element and the
retained-node reconciler keys on `JSON.stringify(item)`, so a footer or badge
belongs inside the item's root, and any new field on `WebTranscriptPayloadItem`
re-renders the row when it arrives. The view model's diagnostics line
(`transcript-benchmark-status`) reports `renders=`, `rows=`, and `latest=`;
`renders=1 rows=1` after a send means the DOM never re-rendered, which is
where to look before blaming a style.

### Phone
Only for device-only behavior (APNS, Live Activity, cellular):
`make phone-deploy`, `make phone-shot`, `make phone-logs`. The App Store
build David uses is an Xcode build he runs; tell him when the phone needs one.

## Web

```bash
make dev                                              # Vite on :47200 against the linked Runtime Host
make ui-capture PAGE=session-detail SCENE=session-detail-stress   # fixture scene, needs only Vite
make ui-capture PAGE=timeline SCENE=timeline-card-stress VIEWPORT=mobile
make ui-capture                                       # demo data; needs the demo backend on :47300 (`make dev-demo`)
make ui-capture ALL=1
make qa-ui-workbench                                  # timeline + session fixtures, desktop and mobile, one index.html
```
Output: `artifacts/ui-capture/<timestamp>/<page>.png`, `<page>-a11y.json|yml`,
`console.log`, `manifest.json`, and `trace.zip` unless `NO_TRACE=1`.

Fixture scenes (`session-detail-stress`, `session-resume`,
`timeline-card-stress`) answer every API call from Playwright routes, so
they run with Vite alone; add new served fields to
`scripts/ui-fixtures/*.ts` so the capture exercises them. The session
context pane is a drawer and is closed in captures; the timeline pane is
what you see. Stop the stack with `make stop`.

Scenes: `demo` (seeded sessions, default), `empty`, `onboarding-modal`,
`missing-api-key`, `timeline-card-stress`, `session-detail-stress`,
`session-resume`.

## How to look
- Open the PNG with `Read`. Compare it with the reference you are matching
  (the provider terminal, a design note, the web version of the same row).
- Check the words: truncation, number formatting, dates on the wrong day,
  "done" with no time.
- Check the edges: anything that sits over scrolling content needs its own
  bottom edge; anything right-aligned needs the same inset as its neighbors.
- Check both themes for anything using materials or secondary text.
- A blank or near-empty PNG is a harness problem, not a pass. Fix the
  preview shell or the fixture until the frame shows the thing.
- Say what you saw in the handoff, with the artifact path. "Tests pass" is
  not a description of a screen.

## Visual regression (CI)
```bash
make qa-ui-baseline           # desktop visual baselines
make qa-ui-baseline-update    # accept new baselines
make qa-ui-baseline-mobile
SKIP_LLM=1 make qa-visual-compare
```

## Public pages
```python
mcp__browser-hub__browser(action="navigate", url="https://longhouse.ai")
mcp__browser-hub__browser(action="look")   # screenshot + accessibility tree
```
For the landing-page ASCII scene, `make qa-remote-scene` writes a review
bundle (frames, contact sheets, `review-prompt.md`); hand only the bundle to
a separate vision-capable agent and let it describe what the frames show.

## Gotchas
- The shell guard blocks `rm -rf` and `>` into repo paths from agents: write
  outputs under `artifacts/` or `mktemp -d`, and use the Write tool for files.
- `xcresulttool export attachments` names files by uuid; `ios-ui-shot`
  renames them to the attachment name.
- `make dev` does not start a local API; `:47300` is `make dev-demo`.
- Output directories are gitignored; quote the path in your handoff, don't
  commit the PNGs.
