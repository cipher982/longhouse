import XCTest

@MainActor
final class SessionChatStressUITests: XCTestCase {
    private enum LaunchEnvironment {
        static let chatFixture = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
        static let diagnostics = "LONGHOUSE_WEBKIT_TRANSCRIPT_DIAGNOSTICS"
        static let probePath = "LONGHOUSE_UI_TEST_CHAT_PROBE_PATH"
        static let triggerPath = "LONGHOUSE_UI_TEST_CHAT_TRIGGER_PATH"
        static let replayPath = "LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH"
    }

    private enum LaunchArgument {
        static let appearanceOverride = "-LONGHOUSE_UI_TEST_APPEARANCE"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testLoadedTranscriptDoesNotRenderStormOrSnapBackDuringUserScroll() {
        runStressProbe(fixtureName: "render-storm", eventCount: "160", replayPath: nil, expectedInitialRows: 50)
    }

    func testReplayTranscriptDoesNotRenderStormOrSnapBackDuringUserScroll() throws {
        guard let replayPath = Self.replayPathFromEnvironmentOrDefault() else {
            throw XCTSkip("Set \(LaunchEnvironment.replayPath) or write /tmp/longhouse-chat-replay.json to run the local SQLite replay stress test.")
        }
        let replayURL = URL(fileURLWithPath: replayPath)
        let expectedRows = try Self.countReplayEvents(at: replayURL)
        XCTAssertGreaterThan(expectedRows, 0)
        runStressProbe(
            fixtureName: "replay-file",
            eventCount: nil,
            replayPath: replayURL.path,
            expectedInitialRows: nil,
            minimumInitialRows: 1,
            maximumInitialRows: min(50, expectedRows)
        )
    }

    private func runStressProbe(
        fixtureName: String,
        eventCount: String?,
        replayPath: String?,
        expectedInitialRows: Int?,
        minimumInitialRows: Int = 1,
        maximumInitialRows: Int? = nil
    ) {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-chat-stress-\(UUID().uuidString)", isDirectory: true)
        let probeURL = scratch.appendingPathComponent("probe.txt")
        let triggerURL = scratch.appendingPathComponent("trigger")
        try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)

        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.chatFixture] = fixtureName
        if let eventCount {
            app.launchEnvironment[LaunchEnvironment.chatEventCount] = eventCount
        }
        if let replayPath {
            app.launchEnvironment[LaunchEnvironment.replayPath] = replayPath
        }
        app.launchEnvironment[LaunchEnvironment.diagnostics] = "1"
        app.launchEnvironment[LaunchEnvironment.probePath] = probeURL.path
        app.launchEnvironment[LaunchEnvironment.triggerPath] = triggerURL.path
        app.launchArguments += [LaunchArgument.appearanceOverride, "light"]
        app.launch()

        addTeardownBlock { [weak self] in
            guard let self, (self.testRun?.failureCount ?? 0) > 0 else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(self.name)-failure"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }

        let transcript = app.descendants(matching: .any)["session-chat-transcript"]
        XCTAssertTrue(transcript.waitForExistence(timeout: 10))

        XCTAssertTrue(waitForProbeFile(probeURL, timeout: 15) { metrics in
            guard metrics.renders >= 1 else { return false }
            if let expectedInitialRows {
                return metrics.rows == expectedInitialRows
            }
            if let maximumInitialRows, metrics.rows > maximumInitialRows {
                return false
            }
            return metrics.rows >= minimumInitialRows && metrics.bytes > 0
        }, readProbe(probeURL))

        let afterInitialRender = probeMetrics(readProbe(probeURL))

        XCTAssertTrue(waitForProbeFile(probeURL, timeout: 5) { metrics in
            metrics.tick == 40
        }, readProbe(probeURL))

        let afterParentChurn = probeMetrics(readProbe(probeURL))
        XCTAssertLessThanOrEqual(afterParentChurn.renders, 1, readProbe(probeURL))
        XCTAssertEqual(afterParentChurn.repeats, 0, readProbe(probeURL))

        dragTowardOlderMessages(transcript)
        RunLoop.current.run(until: Date().addingTimeInterval(0.35))

        try? "1".write(to: triggerURL, atomically: true, encoding: .utf8)

        XCTAssertTrue(waitForProbeFile(probeURL, timeout: 10) { metrics in
            metrics.renders > afterInitialRender.renders
                && metrics.stage == "rendered"
                && (metrics.latest != afterInitialRender.latest || metrics.rows > afterInitialRender.rows)
        }, readProbe(probeURL))

        let afterLiveUpdate = probeMetrics(readProbe(probeURL))
        XCTAssertLessThanOrEqual(afterLiveUpdate.renders, 3, readProbe(probeURL))
        XCTAssertEqual(afterLiveUpdate.repeats, 0, readProbe(probeURL))
        XCTAssertLessThan(afterLiveUpdate.maxRenderMs, 2_500, "WebKit render should stay inside the mobile chat budget. \(readProbe(probeURL))")
        XCTAssertEqual(afterLiveUpdate.stick, 0, "Live update should not snap to bottom after user scrolled up. \(readProbe(probeURL))")
    }

    private static func countReplayEvents(at url: URL) throws -> Int {
        let data = try Data(contentsOf: url)
        let fixture = try JSONDecoder().decode(ReplayFixture.self, from: data)
        return fixture.events.count
    }

    private static func replayPathFromEnvironmentOrDefault() -> String? {
        let environment = ProcessInfo.processInfo.environment
        if let replayPath = environment[LaunchEnvironment.replayPath], !replayPath.isEmpty {
            return replayPath
        }
        let defaultPath = "/tmp/longhouse-chat-replay.json"
        return FileManager.default.fileExists(atPath: defaultPath) ? defaultPath : nil
    }

    private func dragTowardOlderMessages(_ element: XCUIElement) {
        for _ in 0..<2 {
            let start = element.coordinate(withNormalizedOffset: CGVector(dx: 0.50, dy: 0.28))
            let end = element.coordinate(withNormalizedOffset: CGVector(dx: 0.50, dy: 0.90))
            start.press(forDuration: 0.12, thenDragTo: end)
            RunLoop.current.run(until: Date().addingTimeInterval(0.20))
        }
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

private struct ReplayFixture: Decodable {
    let events: [ReplayEvent]
}

private struct ReplayEvent: Decodable {}

private struct ProbeMetrics {
    let renders: Int
    let duplicates: Int
    let repeats: Int
    let rows: Int
    let bytes: Int
    let latest: String
    let stage: String
    let stick: Int
    let renderMs: Int
    let maxRenderMs: Int
    let tick: Int

    init(label: String) {
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
        latest = values["latest"] ?? "none"
        stage = values["stage"] ?? "none"
        stick = Int(values["stick"] ?? "") ?? 0
        renderMs = Int(values["render_ms"] ?? "") ?? 0
        maxRenderMs = Int(values["max_render_ms"] ?? "") ?? 0
        tick = Int(values["tick"] ?? "") ?? 0
    }
}
