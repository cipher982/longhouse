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

Background task details distinguish active membership from reported terminal
history. Expired active evidence becomes unknown without erasing a provider's
recorded completion, failure, cancellation, or abort. Native progress counters
remain separate from counters derived from the archived child transcript.

Settings → Machines shows enrolled machines, activity and independent sync facts.
If activity/sync is unavailable, the directory and supported machine actions remain
usable; failed refreshes label retained facts as last known. Open sessions keeps the
selected machine across listing, realtime updates, both search lanes and launch.
Machine-scoped views never replace the all-machine inbox/widget cache.

An ended Helm's **Show resume command** prepares a terminal handoff on the recorded
machine; opening it does not start a run. **Continue here**, when supported, creates
and opens a separate child session. Retrying unchanged branch text reuses its request ID.

Test: `make test-ios`. Previews: `make ios-previews`. Renderer benchmark:
`make benchmark-ios-transcript`. Ship a build to TestFlight: `make testflight`
(`make testflight-status` shows builds and the public link; see
[`RELEASE.md`](../RELEASE.md)).
For a focused bench run, `make ios-unit TEST=LonghouseIOSTests/SessionModelsTests`
selects a suite; append `/testName` to select one case.
