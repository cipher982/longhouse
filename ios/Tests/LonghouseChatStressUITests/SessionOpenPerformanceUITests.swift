import XCTest

@MainActor
final class SessionOpenPerformanceUITests: XCTestCase {
    private enum LaunchEnvironment {
        static let timelineOpenFixture = "LONGHOUSE_UI_TEST_TIMELINE_OPEN_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
        static let diagnostics = "LONGHOUSE_WEBKIT_TRANSCRIPT_DIAGNOSTICS"
        static let probePath = "LONGHOUSE_UI_TEST_CHAT_PROBE_PATH"
        static let mobileTailDelayMs = "LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS"
    }

    private enum LaunchArgument {
        static let appearanceOverride = "-LONGHOUSE_UI_TEST_APPEARANCE"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testTimelineTapToTranscriptPaintPerformance() throws {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-session-open-\(UUID().uuidString)", isDirectory: true)
        let probeURL = scratch.appendingPathComponent("probe.txt")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)

        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.timelineOpenFixture] = "1"
        app.launchEnvironment[LaunchEnvironment.chatEventCount] = "120"
        app.launchEnvironment[LaunchEnvironment.diagnostics] = "1"
        app.launchEnvironment[LaunchEnvironment.probePath] = probeURL.path
        if let delayMs = ProcessInfo.processInfo.environment[LaunchEnvironment.mobileTailDelayMs] {
            app.launchEnvironment[LaunchEnvironment.mobileTailDelayMs] = delayMs
        }
        app.launchArguments += [LaunchArgument.appearanceOverride, "light"]
        app.launch()

        addTeardownBlock { [weak self] in
            guard let self, (self.testRun?.failureCount ?? 0) > 0 else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(self.name)-failure"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }

        let row = app.descendants(matching: .any)["timeline-open-session-1"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))

        var samplesMs: [Int] = []
        for _ in 0..<5 {
            try? FileManager.default.removeItem(at: probeURL)
            let startedAt = Date()
            row.tap()

            XCTAssertTrue(waitForProbeFile(probeURL, timeout: 10) { metrics in
                metrics.isPaintComplete
            }, readProbe(probeURL))

            samplesMs.append(Int(Date().timeIntervalSince(startedAt) * 1000))
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(row.waitForExistence(timeout: 5))
        }

        let sorted = samplesMs.sorted()
        let average = samplesMs.reduce(0, +) / max(1, samplesMs.count)
        let p50 = sorted[sorted.count / 2]
        let p90 = sorted[min(sorted.count - 1, Int(Double(sorted.count - 1) * 0.9))]
        let maxMs = sorted.last ?? 0
        print("SESSION_OPEN_SIM_METRIC samples_ms=\(samplesMs) avg_ms=\(average) p50_ms=\(p50) p90_ms=\(p90) max_ms=\(maxMs)")

        XCTAssertLessThan(p50, 1_500, "Simulator tap-to-paint median regressed. samples_ms=\(samplesMs)")
    }

    func testTimelineTapToTranscriptPaintProfile() throws {
        let config = ProfileConfig.fromDisk()
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-session-profile-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)

        var records: [ProfileRecord] = []

        let freshProbe = scratch.appendingPathComponent("fresh.txt")
        let app = configuredApp(probeURL: freshProbe, eventCount: config.eventCount, delayMs: nil)
        let launchStartedAt = Date()
        app.launch()
        addFailureScreenshot(app)

        let row = app.descendants(matching: .any)["timeline-open-session-1"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        try append(config.record(
            scenario: "fresh_launch_to_timeline",
            sample: 0,
            elapsedMs: elapsedMs(since: launchStartedAt),
            delayMs: nil,
            metrics: nil
        ), to: &records)

        let freshOpen = try measureOpen(app: app, row: row, probeURL: freshProbe, timeout: 12)
        try append(config.record(
            scenario: "fresh_timeline_tap_to_transcript_paint",
            sample: 0,
            elapsedMs: freshOpen.elapsedMs,
            delayMs: nil,
            metrics: freshOpen.metrics
        ), to: &records)
        returnToTimeline(app: app, row: row)

        for sample in 0..<3 {
            let open = try measureOpen(app: app, row: row, probeURL: freshProbe, timeout: 10)
            try append(config.record(
                scenario: "warm_reopen_tap_to_transcript_paint",
                sample: sample,
                elapsedMs: open.elapsedMs,
                delayMs: nil,
                metrics: open.metrics
            ), to: &records)
            returnToTimeline(app: app, row: row)
        }
        app.terminate()

        for sample in 0..<2 {
            let probe = scratch.appendingPathComponent("cold-\(sample).txt")
            let coldApp = configuredApp(probeURL: probe, eventCount: config.eventCount, delayMs: nil)
            let coldLaunchStartedAt = Date()
            coldApp.launch()
            addFailureScreenshot(coldApp)
            let coldRow = coldApp.descendants(matching: .any)["timeline-open-session-1"]
            XCTAssertTrue(coldRow.waitForExistence(timeout: 10))
            try append(config.record(
                scenario: "cold_relaunch_to_timeline",
                sample: sample,
                elapsedMs: elapsedMs(since: coldLaunchStartedAt),
                delayMs: nil,
                metrics: nil
            ), to: &records)
            let coldOpen = try measureOpen(app: coldApp, row: coldRow, probeURL: probe, timeout: 12)
            try append(config.record(
                scenario: "cold_relaunch_tap_to_transcript_paint",
                sample: sample,
                elapsedMs: coldOpen.elapsedMs,
                delayMs: nil,
                metrics: coldOpen.metrics
            ), to: &records)
            coldApp.terminate()
        }

        let delayedProbe = scratch.appendingPathComponent("delayed-tail.txt")
        let delayedApp = configuredApp(
            probeURL: delayedProbe,
            eventCount: config.eventCount,
            delayMs: config.delayedTailMs
        )
        let delayedLaunchStartedAt = Date()
        delayedApp.launch()
        addFailureScreenshot(delayedApp)
        let delayedRow = delayedApp.descendants(matching: .any)["timeline-open-session-1"]
        XCTAssertTrue(delayedRow.waitForExistence(timeout: 10))
        try append(config.record(
            scenario: "delayed_tail_launch_to_timeline",
            sample: 0,
            elapsedMs: elapsedMs(since: delayedLaunchStartedAt),
            delayMs: config.delayedTailMs,
            metrics: nil
        ), to: &records)
        let delayedOpen = try measureOpen(app: delayedApp, row: delayedRow, probeURL: delayedProbe, timeout: 20)
        try append(config.record(
            scenario: "delayed_tail_tap_to_transcript_paint",
            sample: 0,
            elapsedMs: delayedOpen.elapsedMs,
            delayMs: config.delayedTailMs,
            metrics: delayedOpen.metrics
        ), to: &records)
        delayedApp.terminate()

        try persist(records: records, outputPath: config.outputPath)
    }

    private func configuredApp(probeURL: URL, eventCount: Int, delayMs: Int?) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.timelineOpenFixture] = "1"
        app.launchEnvironment[LaunchEnvironment.chatEventCount] = "\(eventCount)"
        app.launchEnvironment[LaunchEnvironment.diagnostics] = "1"
        app.launchEnvironment[LaunchEnvironment.probePath] = probeURL.path
        if let delayMs {
            app.launchEnvironment[LaunchEnvironment.mobileTailDelayMs] = "\(delayMs)"
        }
        app.launchArguments += [LaunchArgument.appearanceOverride, "light"]
        return app
    }

    private func measureOpen(
        app: XCUIApplication,
        row: XCUIElement,
        probeURL: URL,
        timeout: TimeInterval
    ) throws -> (elapsedMs: Int, metrics: ProbeMetrics) {
        try? FileManager.default.removeItem(at: probeURL)
        let startedAt = Date()
        row.tap()
        XCTAssertTrue(waitForProbeFile(probeURL, timeout: timeout) { metrics in
            metrics.isPaintComplete
        }, readProbe(probeURL))
        return (elapsedMs(since: startedAt), probeMetrics(readProbe(probeURL)))
    }

    private func returnToTimeline(app: XCUIApplication, row: XCUIElement) {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5))
    }

    private func addFailureScreenshot(_ app: XCUIApplication) {
        addTeardownBlock { [weak self] in
            guard let self, (self.testRun?.failureCount ?? 0) > 0 else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(self.name)-failure"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }
    }

    private func elapsedMs(since startedAt: Date) -> Int {
        Int(Date().timeIntervalSince(startedAt) * 1000)
    }

    private func append(
        _ record: ProfileRecord,
        to records: inout [ProfileRecord]
    ) throws {
        records.append(record)
        print("IOS_PROFILE_METRIC \(try encode(record))")
    }

    private func persist(records: [ProfileRecord], outputPath: String?) throws {
        guard let outputPath, !outputPath.isEmpty else { return }
        let lines = try records.map(encode)
        let url = URL(fileURLWithPath: outputPath)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try (lines.joined(separator: "\n") + "\n").write(to: url, atomically: true, encoding: .utf8)
    }

    private func encode(_ record: ProfileRecord) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(record)
        guard let line = String(data: data, encoding: .utf8) else {
            throw CocoaError(.fileWriteUnknown)
        }
        return line
    }

    private func waitForProbeFile(
        _ url: URL,
        timeout: TimeInterval,
        predicate: (ProbeMetrics) -> Bool
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate(probeMetrics(readProbe(url))) {
                return true
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return false
    }

    private func readProbe(_ url: URL) -> String {
        (try? String(contentsOf: url, encoding: .utf8)) ?? ""
    }

    private func probeMetrics(_ label: String) -> ProbeMetrics {
        ProbeMetrics(label: label)
    }
}

private struct ProbeMetrics {
    let rows: Int
    let bytes: Int
    let stage: String
    let renderMs: Int
    let maxRenderMs: Int

    var isPaintComplete: Bool {
        stage == "rendered" && rows >= 50 && bytes > 0
    }

    init(label: String) {
        let pairs = Dictionary(uniqueKeysWithValues: label.split(separator: " ").compactMap { token -> (String, String)? in
            let parts = token.split(separator: "=", maxSplits: 1)
            guard parts.count == 2 else { return nil }
            return (String(parts[0]), String(parts[1]))
        })
        rows = Int(pairs["rows"] ?? "0") ?? 0
        bytes = Int(pairs["bytes"] ?? "0") ?? 0
        stage = pairs["stage"] ?? "none"
        renderMs = Int(pairs["render_ms"] ?? "0") ?? 0
        maxRenderMs = Int(pairs["max_render_ms"] ?? "0") ?? 0
    }
}

private struct ProfileConfig {
    private static let diskConfigURL = URL(fileURLWithPath: "/tmp/longhouse-ios-session-open-profile-config.json")

    let eventCount: Int
    let delayedTailMs: Int
    let outputPath: String?

    static func fromDisk() -> ProfileConfig {
        let diskConfig = try? JSONDecoder().decode(
            ProfileConfigDisk.self,
            from: Data(contentsOf: diskConfigURL)
        )
        return ProfileConfig(
            eventCount: diskConfig?.eventCount ?? 120,
            delayedTailMs: diskConfig?.delayedTailMs ?? 1_500,
            outputPath: diskConfig?.outputPath
        )
    }

    func record(
        scenario: String,
        sample: Int,
        elapsedMs: Int,
        delayMs: Int?,
        metrics: ProbeMetrics?
    ) -> ProfileRecord {
        ProfileRecord(
            scenario: scenario,
            sample: sample,
            elapsedMs: elapsedMs,
            delayMs: delayMs,
            rows: metrics?.rows,
            bytes: metrics?.bytes,
            stage: metrics?.stage,
            renderMs: metrics?.renderMs,
            maxRenderMs: metrics?.maxRenderMs
        )
    }
}

private struct ProfileConfigDisk: Decodable {
    let eventCount: Int?
    let delayedTailMs: Int?
    let outputPath: String?
}

private struct ProfileRecord: Encodable {
    let scenario: String
    let sample: Int
    let elapsedMs: Int
    let delayMs: Int?
    let rows: Int?
    let bytes: Int?
    let stage: String?
    let renderMs: Int?
    let maxRenderMs: Int?
}
