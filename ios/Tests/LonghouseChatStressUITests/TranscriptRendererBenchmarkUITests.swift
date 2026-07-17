import XCTest

@MainActor
final class TranscriptRendererBenchmarkUITests: XCTestCase {
    private enum Environment {
        static let renderer = "IOS_TRANSCRIPT_BENCHMARK_RENDERER"
        static let temperature = "IOS_TRANSCRIPT_BENCHMARK_TEMPERATURE"
        static let debugger = "IOS_TRANSCRIPT_BENCHMARK_DEBUGGER"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testAgentCoreV1() throws {
        let environment = ProcessInfo.processInfo.environment
        let renderer = environment[Environment.renderer] ?? "snapshot-webkit"
        let runID = UUID().uuidString
        XCTAssertEqual(
            renderer,
            "snapshot-webkit",
            "The retained-WebKit and native-UIKit names are reserved but not implemented yet. Never publish baseline numbers under a candidate label."
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
        XCTAssertEqual(initial.semanticTier, "production")
        XCTAssertEqual(initial.rows, 120)

        let startButton = app.buttons["transcript-benchmark-start"]
        XCTAssertTrue(startButton.waitForExistence(timeout: 5), "Benchmark start control did not appear.")
        let traceStartedAt = Date()
        startButton.tap()

        // Move away from the bottom while the active assistant message grows.
        let scrollStartedAt = Date()
        dragTowardOlderMessages(transcript)
        let scrollWallMs = elapsedMs(since: scrollStartedAt)

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
            buildConfiguration: "Debug",
            deviceName: initial.deviceName,
            deviceModel: initial.deviceModel,
            osVersion: initial.osVersion,
            runTemperature: environment[Environment.temperature] ?? "cold",
            debugger: environment[Environment.debugger] ?? "none",
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
