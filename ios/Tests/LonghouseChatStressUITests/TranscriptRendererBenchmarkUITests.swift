import XCTest

@MainActor
final class TranscriptRendererBenchmarkUITests: XCTestCase {
    private enum BuildMetadata {
        static func value(_ key: String, fallback: String) -> String {
            let value = ProcessInfo.processInfo.environment[key]
            guard let value, !value.isEmpty else { return fallback }
            return value
        }

        static let renderer = value("LONGHOUSE_BENCHMARK_RENDERER", fallback: "snapshot-webkit")
        static let temperature = value("LONGHOUSE_BENCHMARK_TEMPERATURE", fallback: "uncontrolled")
        static let debugger = value("LONGHOUSE_BENCHMARK_DEBUGGER", fallback: "none")
        static let buildConfiguration = value("LONGHOUSE_BENCHMARK_BUILD_CONFIGURATION", fallback: "unknown")
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testAgentCoreV1() throws {
        let renderer = BuildMetadata.renderer
        let runID = UUID().uuidString
        XCTAssertTrue(
            ["snapshot-webkit", "retained-webkit"].contains(renderer),
            "The native-UIKit name is reserved but not implemented. Never publish baseline numbers under a candidate label."
        )

        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-transcript-benchmark-\(UUID().uuidString)", isDirectory: true)
        let probeURL = scratch.appendingPathComponent("probe.txt")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)

        let app = XCUIApplication()
        app.launchEnvironment["LONGHOUSE_UI_TEST_CHAT_FIXTURE"] = "benchmark-core"
        app.launchEnvironment["LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"] = "120"
        app.launchEnvironment["LONGHOUSE_WEBKIT_TRANSCRIPT_DIAGNOSTICS"] = "1"
        app.launchEnvironment["LONGHOUSE_MAIN_THREAD_STALL_DIAGNOSTICS"] = "1"
        app.launchEnvironment["LONGHOUSE_UI_TEST_CHAT_PROBE_PATH"] = probeURL.path
        app.launchEnvironment["LONGHOUSE_TRANSCRIPT_BENCHMARK_RENDERER"] = renderer
        app.launchEnvironment["LONGHOUSE_TRANSCRIPT_BENCHMARK_RUN_ID"] = runID
        app.launchEnvironment["LONGHOUSE_TRANSCRIPT_BENCHMARK_BUILD_CONFIGURATION"] = BuildMetadata.buildConfiguration
        app.launchEnvironment["LONGHOUSE_TRANSCRIPT_BENCHMARK_DEBUGGER"] = BuildMetadata.debugger
        app.launchEnvironment["LONGHOUSE_TRANSCRIPT_BENCHMARK_TEMPERATURE"] = BuildMetadata.temperature
        app.launchArguments += ["-LONGHOUSE_UI_TEST_APPEARANCE", "light"]

        let launchStartedAt = Date()
        app.launch()

        addTeardownBlock { [weak self] in
            guard let self else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "transcript-benchmark-\(renderer)"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }

        let transcript = app.descendants(matching: .any)["session-chat-transcript"]
        let composer = app.textFields["session-chat-composer"]
        let status = app.staticTexts["transcript-benchmark-status"]
        XCTAssertTrue(transcript.waitForExistence(timeout: 30), "Benchmark transcript did not appear.")
        XCTAssertTrue(composer.waitForExistence(timeout: 30), "Benchmark composer did not appear.")
        XCTAssertTrue(status.waitForExistence(timeout: 30), "Benchmark status did not appear.")
        XCTAssertTrue(
            waitForProbe(probeURL, status: status, timeout: 30) { $0.benchmarkPhase == "ready" },
            readProbe(probeURL, status: status)
        )

        let launchToReadyMs = elapsedMs(since: launchStartedAt)
        let initial = BenchmarkProbeMetrics(readProbe(probeURL, status: status))
        XCTAssertEqual(initial.renderer, renderer)
        XCTAssertEqual(
            initial.semanticTier,
            renderer == "snapshot-webkit" ? "production" : "mechanical-lower-bound"
        )
        XCTAssertEqual(initial.rows, 120)

        let startButton = app.buttons["transcript-benchmark-start"]
        let continueButton = app.buttons["transcript-benchmark-continue"]
        XCTAssertTrue(startButton.waitForExistence(timeout: 5), "Benchmark start control did not appear.")
        XCTAssertTrue(continueButton.waitForExistence(timeout: 5), "Benchmark continue control did not appear.")
        let traceStartedAt = Date()
        startButton.tap()

        XCTAssertTrue(
            waitForProbe(probeURL, status: status, timeout: 30) { $0.benchmarkPhase == "scroll_ready" },
            readProbe(probeURL, status: status)
        )

        // Move away from the bottom at a deterministic pause, then keep the
        // active assistant message growing to detect any snap-back.
        let scrollStartedAt = Date()
        dragTowardOlderMessages(transcript)
        let scrollWallMs = elapsedMs(since: scrollStartedAt)
        continueButton.tap()

        // This includes XCUITest's own idle wait. The app-attributable portion
        // remains an Instruments/signpost measurement and is reported separately.
        let focusStartedAt = Date()
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 15), "Keyboard did not appear during the trace.")
        let xctestFocusWallMs = elapsedMs(since: focusStartedAt)

        XCTAssertTrue(
            waitForProbe(probeURL, status: status, timeout: 45) { $0.benchmarkPhase == "complete" },
            readProbe(probeURL, status: status)
        )
        let traceWallMs = elapsedMs(since: traceStartedAt)
        let final = BenchmarkProbeMetrics(readProbe(probeURL, status: status))

        XCTAssertEqual(final.benchmarkUpdates, 128, readProbe(probeURL, status: status))
        XCTAssertEqual(final.traceRepeats, 0, readProbe(probeURL, status: status))
        XCTAssertEqual(final.stick, 0, "Streaming snapped back to bottom after an intentional upward scroll. \(readProbe(probeURL, status: status))")
        XCTAssertGreaterThanOrEqual(final.rows, 174, readProbe(probeURL, status: status))

        let result = TranscriptBenchmarkResult(
            schemaVersion: 1,
            runID: runID,
            trace: "agent-core-v1",
            renderer: renderer,
            semanticTier: final.semanticTier,
            gitSHA: initial.buildCommit,
            buildDirty: initial.buildDirty,
            buildConfiguration: initial.buildConfiguration,
            deviceName: initial.deviceName,
            deviceModel: initial.deviceModel,
            osVersion: initial.osVersion,
            runTemperature: initial.runTemperature,
            debugger: initial.debugger,
            thermalState: initial.thermalState,
            lowPowerMode: initial.lowPowerMode,
            batteryState: initial.batteryState,
            batteryLevelPercent: initial.batteryLevelPercent,
            launchToReadyMs: launchToReadyMs,
            traceWallMs: traceWallMs,
            xctestScrollWallMs: scrollWallMs,
            xctestComposerFocusWallMs: xctestFocusWallMs,
            initialRows: initial.rows,
            finalRows: final.rows,
            finalPayloadBytes: final.bytes,
            coldRenderMaxMs: final.coldRenderMaxMs,
            renderCount: final.traceRenders,
            duplicateCount: final.traceDuplicates,
            repeatCount: final.traceRepeats,
            renderP50Ms: final.traceRenderP50Ms,
            renderP95Ms: final.traceRenderP95Ms,
            renderMaxMs: final.traceRenderMaxMs,
            benchmarkUpdateCount: final.benchmarkUpdates,
            attributedSourceRevisionCount: final.traceSourceRevisions,
            unattributedRenderCount: final.traceUnattributedRenders,
            prepareP95Ms: final.tracePrepareP95Ms,
            jsDecodeP95Ms: final.traceJSDecodeP95Ms,
            jsHTMLP95Ms: final.traceJSHTMLP95Ms,
            jsDOMP95Ms: final.traceJSDOMP95Ms,
            jsRAFP95Ms: final.traceJSRAFP95Ms,
            jsTotalP95Ms: final.traceJSTotalP95Ms,
            coldMainThreadStallCount: final.coldMainThreadStallCount,
            coldMainThreadStallMaxMs: final.coldMainThreadStallMaxMs,
            mainThreadStallCount: final.mainThreadStallCount,
            mainThreadStallMaxMs: final.mainThreadStallMaxMs,
            finalStickToBottom: final.stick,
            implementedOperations: [
                "initial_120_rows",
                "stream_12000_chars_20hz",
                "three_tool_transitions",
                "prepend_50_rows",
                "scroll_away_during_stream",
                "composer_focus_during_stream",
            ],
            deferredOperations: [
                "delayed_media_resize",
                "tool_disclosure_expansion",
                "collapsed_message_expansion",
                "measured_prepend_anchor_error",
            ]
        )
        let data = try JSONEncoder.pretty.encode(result)
        let json = String(decoding: data, as: UTF8.self)
        print("TRANSCRIPT_BENCHMARK_RESULT \(json.replacingOccurrences(of: "\n", with: ""))")

        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "transcript-benchmark-\(renderer).json"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func dragTowardOlderMessages(_ element: XCUIElement) {
        for _ in 0..<2 {
            let start = element.coordinate(withNormalizedOffset: CGVector(dx: 0.50, dy: 0.28))
            let end = element.coordinate(withNormalizedOffset: CGVector(dx: 0.50, dy: 0.90))
            start.press(forDuration: 0.08, thenDragTo: end)
        }
    }

    private func waitForProbe(
        _ url: URL,
        status: XCUIElement,
        timeout: TimeInterval,
        predicate: (BenchmarkProbeMetrics) -> Bool
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate(BenchmarkProbeMetrics(readProbe(url, status: status))) {
                return true
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        return false
    }

    private func readProbe(_ url: URL, status: XCUIElement) -> String {
        if let fileValue = try? String(contentsOf: url, encoding: .utf8), !fileValue.isEmpty {
            return fileValue
        }
        return status.exists ? status.label : ""
    }

    private func elapsedMs(since start: Date) -> Int {
        Int(Date().timeIntervalSince(start) * 1_000)
    }
}

private struct BenchmarkProbeMetrics {
    let renders: Int
    let duplicates: Int
    let repeats: Int
    let rows: Int
    let bytes: Int
    let stick: Int
    let renderP50Ms: Int
    let renderP95Ms: Int
    let renderMaxMs: Int
    let coldRenderMaxMs: Int
    let traceRenders: Int
    let traceDuplicates: Int
    let traceRepeats: Int
    let traceRenderP50Ms: Int
    let traceRenderP95Ms: Int
    let traceRenderMaxMs: Int
    let tracePrepareP95Ms: Int
    let traceJSDecodeP95Ms: Int
    let traceJSHTMLP95Ms: Int
    let traceJSDOMP95Ms: Int
    let traceJSRAFP95Ms: Int
    let traceJSTotalP95Ms: Int
    let traceSourceRevisions: Int
    let traceUnattributedRenders: Int
    let benchmarkPhase: String
    let benchmarkUpdates: Int
    let renderer: String
    let semanticTier: String
    let buildCommit: String
    let buildDirty: Bool
    let mainThreadStallCount: Int
    let mainThreadStallMaxMs: Int
    let coldMainThreadStallCount: Int
    let coldMainThreadStallMaxMs: Int
    let deviceName: String
    let deviceModel: String
    let osVersion: String
    let buildConfiguration: String
    let runTemperature: String
    let debugger: String
    let thermalState: String
    let lowPowerMode: Bool
    let batteryState: String
    let batteryLevelPercent: Int

    init(_ label: String) {
        let values = Dictionary(uniqueKeysWithValues: label.split(separator: " ").compactMap { token -> (String, String)? in
            let parts = token.split(separator: "=", maxSplits: 1)
            guard parts.count == 2 else { return nil }
            return (String(parts[0]), String(parts[1]))
        })
        renders = Int(values["renders"] ?? "") ?? 0
        duplicates = Int(values["duplicates"] ?? "") ?? 0
        repeats = Int(values["repeats"] ?? "") ?? 0
        rows = Int(values["rows"] ?? "") ?? 0
        bytes = Int(values["bytes"] ?? "") ?? 0
        stick = Int(values["stick"] ?? "") ?? 0
        renderP50Ms = Int(values["render_p50_ms"] ?? "") ?? 0
        renderP95Ms = Int(values["render_p95_ms"] ?? "") ?? 0
        renderMaxMs = Int(values["max_render_ms"] ?? "") ?? 0
        coldRenderMaxMs = Int(values["cold_render_max_ms"] ?? "") ?? 0
        traceRenders = Int(values["trace_renders"] ?? "") ?? 0
        traceDuplicates = Int(values["trace_duplicates"] ?? "") ?? 0
        traceRepeats = Int(values["trace_repeats"] ?? "") ?? 0
        traceRenderP50Ms = Int(values["trace_render_p50_ms"] ?? "") ?? 0
        traceRenderP95Ms = Int(values["trace_render_p95_ms"] ?? "") ?? 0
        traceRenderMaxMs = Int(values["trace_render_max_ms"] ?? "") ?? 0
        tracePrepareP95Ms = Int(values["trace_prepare_p95_ms"] ?? "") ?? 0
        traceJSDecodeP95Ms = Int(values["trace_js_decode_p95_ms"] ?? "") ?? 0
        traceJSHTMLP95Ms = Int(values["trace_js_html_p95_ms"] ?? "") ?? 0
        traceJSDOMP95Ms = Int(values["trace_js_dom_p95_ms"] ?? "") ?? 0
        traceJSRAFP95Ms = Int(values["trace_js_raf_p95_ms"] ?? "") ?? 0
        traceJSTotalP95Ms = Int(values["trace_js_total_p95_ms"] ?? "") ?? 0
        traceSourceRevisions = Int(values["trace_source_revisions"] ?? "") ?? 0
        traceUnattributedRenders = Int(values["trace_unattributed_renders"] ?? "") ?? 0
        benchmarkPhase = values["benchmark_phase"] ?? "none"
        benchmarkUpdates = Int(values["benchmark_updates"] ?? "") ?? 0
        renderer = values["benchmark_renderer"] ?? "none"
        semanticTier = values["semantic_tier"] ?? "none"
        buildCommit = values["build_commit"] ?? "unknown"
        buildDirty = values["build_dirty"] == "1"
        mainThreadStallCount = Int(values["main_stalls"] ?? "") ?? 0
        mainThreadStallMaxMs = Int(values["main_stall_max_ms"] ?? "") ?? 0
        coldMainThreadStallCount = Int(values["cold_main_stalls"] ?? "") ?? 0
        coldMainThreadStallMaxMs = Int(values["cold_main_stall_max_ms"] ?? "") ?? 0
        deviceName = values["device_name"]?.removingPercentEncoding ?? "unknown"
        deviceModel = values["device_model"]?.removingPercentEncoding ?? "unknown"
        osVersion = values["os_version"]?.removingPercentEncoding ?? "unknown"
        buildConfiguration = values["benchmark_build"]?.removingPercentEncoding ?? "unknown"
        runTemperature = values["benchmark_temperature"]?.removingPercentEncoding ?? "uncontrolled"
        debugger = values["benchmark_debugger"]?.removingPercentEncoding ?? "unknown"
        thermalState = values["thermal_state"] ?? "unknown"
        lowPowerMode = values["low_power"] == "1"
        batteryState = values["battery_state"] ?? "unknown"
        batteryLevelPercent = Int(values["battery_level_percent"] ?? "") ?? -1
    }
}

private struct TranscriptBenchmarkResult: Codable {
    let schemaVersion: Int
    let runID: String
    let trace: String
    let renderer: String
    let semanticTier: String
    let gitSHA: String
    let buildDirty: Bool
    let buildConfiguration: String
    let deviceName: String
    let deviceModel: String
    let osVersion: String
    let runTemperature: String
    let debugger: String
    let thermalState: String
    let lowPowerMode: Bool
    let batteryState: String
    let batteryLevelPercent: Int
    let launchToReadyMs: Int
    let traceWallMs: Int
    let xctestScrollWallMs: Int
    let xctestComposerFocusWallMs: Int
    let initialRows: Int
    let finalRows: Int
    let finalPayloadBytes: Int
    let coldRenderMaxMs: Int
    let renderCount: Int
    let duplicateCount: Int
    let repeatCount: Int
    let renderP50Ms: Int
    let renderP95Ms: Int
    let renderMaxMs: Int
    let benchmarkUpdateCount: Int
    let attributedSourceRevisionCount: Int
    let unattributedRenderCount: Int
    let prepareP95Ms: Int
    let jsDecodeP95Ms: Int
    let jsHTMLP95Ms: Int
    let jsDOMP95Ms: Int
    let jsRAFP95Ms: Int
    let jsTotalP95Ms: Int
    let coldMainThreadStallCount: Int
    let coldMainThreadStallMaxMs: Int
    let mainThreadStallCount: Int
    let mainThreadStallMaxMs: Int
    let finalStickToBottom: Int
    let implementedOperations: [String]
    let deferredOperations: [String]
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}
