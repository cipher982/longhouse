# iOS Streaming Transcript Renderer Benchmark

**Status:** Spike
**Owner:** Longhouse iOS
**Created:** 2026-07-17

## Decision Question

Longhouse should keep its current `WKWebView` transcript unless a smaller
renderer proves a material physical-device advantage without giving up the
behaviors that make agent transcripts useful.

This benchmark answers three separate questions:

1. What does the current production snapshot-WebKit path cost when cold, warm,
   streaming, scrolling, prepending history, and opening the keyboard?
2. How much of that cost comes from WebKit itself versus Longhouse rebuilding
   and transferring the complete transcript for each accepted update?
3. Can a UIKit-backed renderer match transcript semantics and interaction
   correctness while materially reducing cold latency or memory?

The benchmark is intentionally renderer-focused. It does not measure network,
server, authentication, or provider latency.

## Why A Benchmark, Not Another Renderer

The current hot path performs more work than the information change requires:

```text
TimelineItem[]
  -> WebTranscriptPayload[]
  -> JSON Data
  -> Base64 String
  -> JavaScript source / WebKit IPC
  -> decoded bytes
  -> decoded JSON
  -> generated HTML for every row
  -> root.innerHTML replacement
  -> document layout
```

An append-sized change has an irreducible lower bound closer to:

```text
append delta -> parse unfinished block -> lay out changed visible region
             -> preserve anchor -> composite changed pixels
```

Before replacing WebKit, we need to quantify the gap between those two paths.
The useful community artifact is a reproducible operation trace and result
format that can compare renderer claims on real Apple hardware.

## Renderer Lanes

Every result declares both a renderer and a semantic tier.

| Renderer | Initial tier | Purpose |
| --- | --- | --- |
| `snapshot-webkit` | `production` | Current Longhouse renderer and correctness baseline. |
| `retained-webkit` | `mechanical-lower-bound` | Stable DOM nodes with incremental updates; isolates whole-DOM replacement cost. |
| `native-uikit` | `mechanical-lower-bound` | Reused native cells and native text; isolates WebContent-process and bridge cost. |

A mechanical lower bound must not be presented as a replacement. It may omit
rich Markdown details while measuring scrolling and append mechanics. A
candidate advances to `semantic-parity` only after it passes the parity matrix
below.

Pure SwiftUI is not an initial lane. Current evidence shows that variable-height
lazy stacks, streaming Markdown, prepends, and document-wide selection confound
the engine comparison. A future SwiftUI implementation can plug into the same
trace if it has a concrete reason to exist.

## Canonical Operation Trace: `agent-rich-v1`

The trace is deterministic and renderer-independent.

### Initial document

- 120 rows: 24 user messages, 36 assistant messages, tool rows, actions, and
  passive exploration groups.
- Assistant Markdown includes headings, nested lists, links, Unicode, one
  100-line fenced code block, and one 12-column table.
- At least one collapsed 15,000-character message.
- Stable item identifiers and deterministic timestamps.

### Operations

1. Append one assistant message from 0 to 12,000 characters at 20 updates/sec.
2. Transition three tool rows from running to completed with output.
3. While pinned, grow the final row and verify the bottom remains visible.
4. Scroll upward, continue streaming, and verify no automatic snap to bottom.
5. Prepend 50 older rows while positioned 120 points from the top.
6. Expand a tool and the collapsed long message while scrolled up.
7. Resolve four delayed media placeholders, changing row heights.
8. Focus and dismiss the native composer during streaming.

The first implementation may land a `core-v1` subset containing initial rows,
streaming, tool transition, prepend, scroll-away, and keyboard focus. Missing
operations must be declared in the result rather than silently skipped.

## Speed-of-Light Budgets

These are comparison gates, not claims that XCTest overhead equals product
latency.

| Metric | Target |
| --- | ---: |
| Discrete interaction app work p95 | < 50 ms |
| Tap-to-visible response p95 | < 100 ms |
| Main-thread work during continuous scroll | < 5 ms/frame |
| Renderer update-to-visible p95 at 20 Hz | < 16.7 ms |
| Main-run-loop stalls >= 250 ms | 0 |
| Anchor error after append/prepend/resize | <= 2 pt |
| Composer tap-to-keyboard p95 | < 250 ms app-attributable time |
| Repeated rendering of an identical revision | 0 |

Cold first paint and memory are comparative. The native migration gate is at
least 25% better cold first paint or 30% lower total resident memory, with no
regression in the interaction and correctness gates.

## Measurements

Each run emits one structured result with:

- benchmark schema and trace versions;
- git SHA, build identity, renderer, semantic tier, device model, OS, and run
  temperature (`cold` or `warm`);
- launch-to-fixture-ready and trigger-to-trace-complete wall time;
- renderer update count, duplicate count, repeated-revision count, payload
  bytes, p50/p95/max render duration, and final revision;
- scroll-away stickiness and prepend anchor error;
- composer focus wall time;
- cold-launch and trace-only main-thread stall counts and maximum stalls;
- process-resident memory for the app and WebContent process when available;
- semantic operations implemented/skipped;
- failure screenshots and an Instruments trace reference.

The app uses `OSSignposter` intervals for internal stages. XCUITest supplies
external interaction timing and transfers JSON as an `.xcresult` attachment.
Instruments Hangs, Time Profiler, SwiftUI, Core Animation, and Allocations remain
the authority for attribution. XCTest's own idle waits are reported separately
and never described as application work.

## Run Matrix

The publication matrix is:

- oldest supported physical iPhone;
- current Pro iPhone;
- current simulator only as a fast regression signal;
- 30 cold and 50 warm runs per renderer on physical devices;
- Low Power Mode off, thermal state recorded, device unplugged only when power
  measurements are intentionally collected;
- Debug-under-LLDB and optimized non-debugger runs reported separately.

Results from different Xcode, OS beta, build configuration, or debugger state
must not be pooled.

`uncontrolled` is the default run temperature and is the only honest label for
an isolated invocation. `warm` requires one discarded identical run immediately
before the recorded run. `cold` requires an explicitly documented app/WebContent
reset protocol. The app records the requested label, thermal state, Low Power
Mode, battery state, and battery level in the result; the extractor must not
rewrite those fields afterward.

## Semantic Parity Matrix

A replacement candidate must demonstrate:

- assistant and user prose;
- streaming append without reprocessing settled blocks;
- headings, emphasis, links, Unicode, lists, tables, and fenced code;
- text selection and copy across the useful document boundary;
- VoiceOver reading order and actionable links;
- tool running/completed/dropped/orphan states;
- passive groups and disclosures;
- optimistic submitted inputs and reconciliation;
- authenticated media, delayed image sizing, and placeholders;
- collapsed-message expansion;
- sticky-bottom disengagement and re-engagement;
- prepend without visible movement;
- native composer keyboard transitions;
- Dynamic Type, light/dark appearance, and memory-pressure recovery.

## Harness Shape

The harness reuses the existing Debug-only chat fixture and
`LonghouseChatStress` UI-test scheme:

```text
TranscriptBenchmarkUITests
  -> launches deterministic ChatUITestFixtureView
  -> selects renderer with an explicit environment value
  -> waits for fixture-ready revision
  -> triggers the canonical operation trace
  -> performs scroll and keyboard interactions
  -> reads renderer diagnostics through accessibility/probe output
  -> attaches versioned JSON + screenshot to xcresult
```

Production behavior never selects a benchmark renderer. The renderer switch is
compiled only in Debug and requires the benchmark fixture environment.

## Commands And Artifacts

The repo owns one command:

```bash
make benchmark-ios-transcript
```

Optional environment selects renderer, destination, and output directory. The
default simulator run is a smoke comparison, not publishable evidence.
Physical-device publication runs use the same XCTest and save `.xcresult`, JSON,
screenshots, and optional `.trace` files under a gitignored artifact directory.

Publication runs set `IOS_TRANSCRIPT_BENCHMARK_BUILD_MODE=optimized`. This keeps
the Debug-only deterministic fixture available while compiling the app with
Swift `-O`; results identify the configuration as `Debug-Optimized`. Plain
`Debug` remains the fast harness-development mode and must not be pooled with
optimized results.

## Decision Rules

1. Optimize `snapshot-webkit` first if it misses budgets because of avoidable
   Longhouse work rather than WebKit startup or rendering.
2. Prefer `retained-webkit` if it reaches the budgets and preserves semantics;
   it is the smallest product change.
3. Consider `native-uikit` only after it reaches semantic parity and clears the
   cold-paint or memory migration gate.
4. Do not ship a permanent dual renderer.
5. Publish negative results, skipped semantics, debugger state, and device
   variance. A benchmark that hides those facts is marketing, not evidence.

## Public Write-Up Shape

The first post should publish:

- the operation trace and why chat rendering is mechanically difficult;
- cold versus warm and debugger versus non-debugger results;
- what WebKit did well and what Longhouse did unnecessarily;
- where native UIKit helped and which semantics cost the advantage;
- raw JSON, hardware/OS/build metadata, and runnable commands;
- the decision Longhouse made and the measurement that would reverse it.

The post must not infer how ChatGPT, Claude, or Gemini render internally unless
their vendors publish verifiable implementation evidence.
