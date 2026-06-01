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
        let max = sorted.last ?? 0
        print("SESSION_OPEN_SIM_METRIC samples_ms=\(samplesMs) avg_ms=\(average) p50_ms=\(p50) p90_ms=\(p90) max_ms=\(max)")

        XCTAssertLessThan(p50, 1_500, "Simulator tap-to-paint median regressed. samples_ms=\(samplesMs)")
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

    var isPaintComplete: Bool {
        (stage == "rendered" || stage == "duplicate") && rows >= 50 && bytes > 0
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
    }
}
