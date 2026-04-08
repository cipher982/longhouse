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
4. window-host app
5. menu-bar-host app

## Commands

```bash
make menubar-harness-full       # one-shot loop: test, render, smoke, manifest
make menubar-harness-test       # build + Swift tests
make menubar-harness-fixtures   # render healthy/degraded/broken PNGs
make menubar-harness-live       # render live local-health PNG
make menubar-harness-smoke      # boot both app shells and dry-run all controls
make menubar-harness-xcuitest   # generate the Xcode wrapper and run macOS XCUITests
make menubar-harness-window     # launch as a normal window
make menubar-harness-menubar    # launch as a real menu bar extra
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
- `live.png`
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
- Prefer `make menubar-harness-full` when you need the whole unattended loop.
- Treat the Xcode wrapper as generated harness infrastructure; regenerate it via the script instead of hand-editing `.xcodeproj` files.
- Use live PNG/window/menubar runs only after the fixture loop is stable.
- Reuse the existing `longhouse local-health --json` contract. Do not teach the Swift code to parse launchd directly.
