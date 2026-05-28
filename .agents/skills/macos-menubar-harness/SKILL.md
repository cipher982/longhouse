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
2. fixture or live `longhouse local-health --json`
3. PNG snapshot render
4. full-frame visual inspection of the rendered PNGs
5. window-host app
6. menu-bar-host app

For menu bar/dashboard information architecture work, treat the harness as a **mini product surface**, not a screenshot tool. Start by deciding which user-facing states must be obvious at a glance, then encode those as fixtures before touching live data.

## Commands

```bash
make menubar-harness MODE=full            # one-shot loop: test, render, smoke, manifest
make menubar-harness MODE=test            # build + Swift tests
make menubar-harness MODE=render-fixtures # render healthy/degraded/broken PNGs
make menubar-harness MODE=snapshot-live   # render live local-health PNG
make menubar-harness MODE=smoke           # boot both app shells and dry-run all controls
make menubar-harness MODE=xcuitest        # generate the Xcode wrapper and run macOS XCUITests
make menubar-harness MODE=window-live     # launch as a normal live window
make menubar-harness MODE=menubar-live    # launch as a real live menu bar extra
make test-install-macos-ambient           # full disposable installer smoke for engine + menu bar on local macOS
```

## Artifacts

Rendered PNGs and action logs land in:

```bash
artifacts/menubar-harness/
```

Typical files:
- `healthy.png`
- `degraded.png`
- `broken.png`
- `managed-attached.png`
- `managed-detached.png`
- `managed-degraded.png`
- `orphan-bridges.png`
- `machine-broken.png`
- `live.png`
- `window-smoke-actions.jsonl`
- `menubar-smoke-actions.jsonl`
- `xcuitest.log`
- `LonghouseMenuBarWindowHost.xcresult`
- `manifest.json`
- installer temp-home artifacts under `/var/folders/.../longhouse-install-smoke-*` during `make test-install-macos-ambient`

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
- Treat `artifacts/menubar-harness/*.png` as required QA, not a side effect. Inspect the literal full-frame images before touching the installed app.
- Do not accept “rendered successfully” or image dimensions as proof. Catch spacing, clipping, edge contact, and optical balance in the PNG stage.
- Reinstall `Longhouse.app` only after the fixture/live PNGs look correct.
- Prefer `make menubar-harness-full` when you need the whole unattended loop.
- Treat the Xcode wrapper as generated harness infrastructure; regenerate it via the script instead of hand-editing `.xcodeproj` files.
- Use live PNG/window/menubar runs only after the fixture loop is stable.
- Reuse the existing `longhouse local-health --json` contract. Do not teach the Swift code to parse launchd directly.
- Use `make test-install-macos-ambient` when changing the unified install path, launchd wiring, or menu bar runtime packaging.

## Recommended Iteration Loop For New States

1. Write down the user-facing states first.
   - Example: `managed-attached`, `managed-detached`, `managed-degraded`, `orphan-bridges`, `machine-broken`
2. Add or update fixture JSON in `desktop/LonghouseMenuBarHarness/Fixtures/`.
3. Extend the shared snapshot contract in `Sources/LonghouseMenuBarCore/HealthSnapshot.swift`.
4. Render all fixtures:
   ```bash
   make menubar-harness-fixtures
   ```
5. Inspect the actual PNGs in `artifacts/menubar-harness/`.
6. Only after the fixtures read well, check `make menubar-harness-window` or `make menubar-harness-menubar`.

## Product Guidance

- The menu bar should answer: **what Longhouse-owned things are alive on this Mac right now, and do I need to do anything?**
- Prefer explicit session/process truth over passive telemetry summaries.
- Use the menu bar for small, high-confidence actions (`reattach`, `stop`, `open`) and escalate to the full app for heavier workflows.
