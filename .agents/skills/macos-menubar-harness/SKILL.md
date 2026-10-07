---
name: macos-menubar-harness
description: Iterate on the Longhouse macOS local-health menu bar UI with a stable snapshot/window/menubar loop.
---

# Longhouse macOS Menu Bar Harness

Use this when working on the local-health menu bar utility or its shared SwiftUI surface.

## Principle

Do not start with fragile GUI scripting.

The inner loop is:
1. shared SwiftUI core
2. fixture JSON in the shape of `longhouse local-health --json`
3. PNG snapshot render
4. full-frame visual inspection of the rendered PNGs
5. window-host app
6. menu-bar-host app

For menu bar/dashboard information architecture work, treat the harness as a **mini product surface**, not a screenshot tool. Start by deciding which user-facing states must be obvious at a glance, then encode those as fixtures.

## Commands

`make menubar-harness` is a native target: from a developer machine it dispatches a
fresh GitHub-hosted macOS VM (a clean, pushed revision and an authenticated `gh`,
as in the `zerg-ui` skill) and downloads the VM's evidence. The dispatcher accepts
only the fixture modes; the live modes (`snapshot-live`, `window-live`,
`menubar-live`, `full`) use a live Runtime Host and are refused.

```bash
make menubar-harness MODE=test             # build + Swift tests (the default)
make menubar-harness MODE=render-fixtures  # render every fixture to a PNG
make menubar-harness MODE=render-trust-states
make menubar-harness MODE=smoke            # boot both app shells and dry-run all controls
make menubar-harness MODE=xcuitest         # generate the Xcode wrapper and run macOS XCUITests
make test-install                          # installer smoke through the hosted native worker
```

`scripts/qa/menubar-harness.sh` (its `snapshot-fixture <name>`, `window-fixture`,
`menubar-fixture` subcommands) refuses to run outside that VM.

## Artifacts

Each run writes `artifacts/menubar-harness/<run-id>/` inside the VM. The dispatcher
downloads the VM's `artifacts/` tree to
`artifacts/test-isolation/<id>/evidence/menubar-harness/project/` (CI keeps it one
day), so the files below are under `.../project/menubar-harness/<run-id>/`. Typical files:
- one `<fixture>.png` per fixture in `desktop/LonghouseMenuBarHarness/Fixtures/`
  (`healthy`, `degraded`, `broken`, `managed-attached`, `managed-detached`,
  `managed-degraded`, `orphan-bridges`, `machine-broken`, ...)
- `trust-never-loaded.png` and the other trust-state renders
- `window-smoke-actions.jsonl`
- `menubar-smoke-actions.jsonl`
- `xcuitest.log`
- `LonghouseMenuBarWindowHost.xcresult`
- `manifest.json`

## Source Layout

```text
desktop/LonghouseMenuBarHarness/
  Fixtures/                       fixture JSON states
  Sources/LonghouseMenuBarCore/   shared models, actions, SwiftUI surface
  Sources/LonghouseMenuBarHarnessSnapshot/
  Sources/LonghouseMenuBarHarnessApp/
  Sources/LonghouseMenuBarHarnessMenuBar/
  XcodeHarness/                    generated-on-demand Xcode wrapper for XCUITest
```

## Rules

- Keep the shared UI in `LonghouseMenuBarCore`.
- Prefer adding accessibility identifiers at the shared view layer.
- Use fixture PNGs first when changing layout or state presentation.
- For local process/session truth, model it explicitly in the snapshot contract instead of trying to infer it from `recent activity`.
- The key menu bar states for managed sessions are: `attached`, `detached`, `degraded`, and `orphan bridge`.
- Keep orphaned background bridges separate from managed sessions in the UI. They are an attention surface, not a normal session card.
- Do not hide managed session/process cards behind a generic blocker state. When the machine is broken, the specific managed sessions or orphan bridges causing it must stay visible and actionable.
- Treat the downloaded fixture PNGs as required QA, not a side effect. Inspect the literal full-frame images before touching the installed app.
- Do not accept “rendered successfully” or image dimensions as proof. Catch spacing, clipping, edge contact, and optical balance in the PNG stage.
- Reinstall `Longhouse.app` only after the fixture PNGs look correct.
- Treat the Xcode wrapper as generated harness infrastructure; regenerate it via the script instead of hand-editing `.xcodeproj` files.
- Reuse the existing `longhouse local-health --json` contract. Do not teach the Swift code to parse launchd directly.
- Use `make test-install` when changing the unified install path, launchd wiring, or menu bar runtime packaging.

## Recommended Iteration Loop For New States

1. Write down the user-facing states first.
   - Example: `managed-attached`, `managed-detached`, `managed-degraded`, `orphan-bridges`, `machine-broken`
2. Add or update fixture JSON in `desktop/LonghouseMenuBarHarness/Fixtures/`.
3. Extend the shared snapshot contract in `Sources/LonghouseMenuBarCore/HealthSnapshot.swift`.
4. Render all fixtures (commit and push first; it is a dispatched run):
   ```bash
   make menubar-harness MODE=render-fixtures
   ```
5. Inspect the actual PNGs in the downloaded evidence (path above).
6. Only after the fixtures read well, run `MODE=smoke` and `MODE=xcuitest`, then refresh the installed app (`make dogfood-refresh HERE=1` for your working tree before it lands; plain `make dogfood-refresh` installs `origin/main`) and look at the real menu bar.

## Product Guidance

- The menu bar should answer: **what Longhouse-owned things are alive on this Mac right now, and do I need to do anything?**
- Prefer explicit session/process truth over passive telemetry summaries.
- Use the menu bar for small, high-confidence actions (`reattach`, `stop`, `open`) and escalate to the full app for heavier workflows.
