# ios

The SwiftUI iOS app, its widget and Live Activity. Folder-by-folder layout: the
Code map in [`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

- `Sources/LonghouseApp/` is the app target, grouped by feature; `Sources/Shared/`
  is shared with the widget; tests in `Tests/` mirror the source folders.
- The Xcode project is generated from `XcodeHarness/project.yml` by
  `make ios-project`; the build fails early when it is stale.
- The session transcript renders in a web view. Its document,
  `Resources/Transcript/transcript.html`, is built from
  `web/src/embeds/ios-transcript/` by `make generate-ios-transcript`.
- `Sources/Shared/Generated/` is written by generators; never edit it.

Test: `make test-ios`. Previews: `make ios-previews`. Renderer benchmark:
`make benchmark-ios-transcript`.
