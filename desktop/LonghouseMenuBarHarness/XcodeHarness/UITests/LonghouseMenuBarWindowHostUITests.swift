import XCTest

final class LonghouseMenuBarWindowHostUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testBrokenFixtureLaunchesWindowHost() throws {
        let actionLogURL = makeTemporaryFileURL(named: "actions.jsonl")
        defer {
            try? FileManager.default.removeItem(at: actionLogURL.deletingLastPathComponent())
        }

        let app = launchApp(fixture: "broken", actionLogURL: actionLogURL)

        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 5), "Window host did not appear")
        XCTAssertEqual(window.title, "Longhouse Desktop")
        XCTAssertTrue(app.staticTexts["Local shipping needs repair"].waitForExistence(timeout: 5))
        let scrollingContent = app.scrollViews.firstMatch
        XCTAssertTrue(scrollingContent.waitForExistence(timeout: 5), "Scrollable panel content was not found")
        let repairButton = try XCTUnwrap(
            findButton(in: app, candidates: [AccessibilityID.Button.repair, "Repair"], container: scrollingContent),
            "Repair button was not found"
        )
        tapWhenVisible(repairButton, in: scrollingContent)

        let logsButton = try XCTUnwrap(
            findButton(in: app, candidates: [AccessibilityID.Button.openLogs, "Logs"], container: scrollingContent),
            "Logs button was not found"
        )
        tapWhenVisible(logsButton, in: scrollingContent)

        let actions = try waitForActionRecords(at: actionLogURL, count: 2)
        XCTAssertEqual(actions.map(\.action), ["repairInstall", "openLogs"])
        XCTAssertEqual(Set(actions.map(\.headline)), ["Longhouse engine service is stopped"])

        let attachment = XCTAttachment(screenshot: window.screenshot())
        attachment.name = "broken-window-host"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor
    func testHighCardinalitySessionsKeepActionsInViewport() throws {
        let fixtureURL = try makeHighCardinalityBrokenFixture(sessionCount: 34)
        let actionLogURL = makeTemporaryFileURL(named: "actions.jsonl")
        defer {
            try? FileManager.default.removeItem(at: fixtureURL.deletingLastPathComponent())
            try? FileManager.default.removeItem(at: actionLogURL.deletingLastPathComponent())
        }

        let app = launchApp(fixtureURL: fixtureURL, actionLogURL: actionLogURL)
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 5), "High-cardinality window did not appear")
        XCTAssertLessThanOrEqual(window.frame.height, 800, "Panel exceeded its bounded viewport")
        XCTAssertTrue(app.scrollViews.firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons[AccessibilityID.Button.repair].isHittable)
        XCTAssertTrue(app.buttons[AccessibilityID.Button.openLogs].isHittable)
    }

    @MainActor
    private func launchApp(fixture: String, actionLogURL: URL) -> XCUIApplication {
        launchApp(fixtureURL: fixtureURL(named: fixture), actionLogURL: actionLogURL)
    }

    @MainActor
    private func launchApp(fixtureURL: URL, actionLogURL: URL) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = [
            "-ApplePersistenceIgnoreState", "YES",
            "--input", fixtureURL.path,
            "--effect-mode", "log-only",
            "--action-log", actionLogURL.path,
        ]
        app.launch()
        app.activate()
        return app
    }

    private func makeHighCardinalityBrokenFixture(sessionCount: Int) throws -> URL {
        var broken = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL(named: "broken"))) as? [String: Any]
        )
        let degraded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL(named: "managed-degraded")))
                as? [String: Any]
        )
        let template = try XCTUnwrap((degraded["managed_sessions"] as? [[String: Any]])?.first)
        broken["managed_sessions"] = (0..<sessionCount).map { index in
            var session = template
            session["session_id"] = "fixture-session-\(index)"
            session["workspace_label"] = "workspace-\(index)"
            if index == sessionCount - 1 {
                session["state"] = "detached"
                session["phase"] = "provider thread switched"
                session["bridge_status"] = "detached"
                session["reason_codes"] = ["provider_thread_switched"]
            } else {
                session["state"] = "attached"
                session["bridge_status"] = "running"
                session["reason_codes"] = []
            }
            return session
        }
        broken["managed_summary"] = [
            "attached_count": sessionCount - 1,
            "detached_count": 1,
            "degraded_count": 0,
            "orphan_bridge_count": 0,
        ]
        var engineStatus = try XCTUnwrap(broken["engine_status"] as? [String: Any])
        var payload = try XCTUnwrap(engineStatus["payload"] as? [String: Any])
        payload["storage_v2_outbox"] = [
            "pending_count": 480,
            "pending_bytes": 1_966_080,
            "blocked_source_count": 480,
            "blocked_bytes": 1_966_080,
            "latest_block_kind": "source_epoch_conflict_unresolved",
            "latest_block_detail": "source_epoch_not_found",
            "byte_limit": 1_073_741_824,
        ]
        engineStatus["payload"] = payload
        broken["engine_status"] = engineStatus
        let outputURL = makeTemporaryFileURL(named: "high-cardinality-broken.json")
        try JSONSerialization.data(withJSONObject: broken).write(to: outputURL)
        return outputURL
    }

    private func fixtureURL(named name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("../Fixtures/\(name).json")
            .standardizedFileURL
    }

    private func makeTemporaryFileURL(named name: String) -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("longhouse-menubar-xcuitests", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent(name)
    }

    private func waitForActionRecords(at logURL: URL, count: Int, timeout: TimeInterval = 5) throws -> [ActionRecord] {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let data = try? Data(contentsOf: logURL),
               !data.isEmpty,
               let text = String(data: data, encoding: .utf8) {
                let lines = text
                    .split(whereSeparator: \.isNewline)
                    .map(String.init)
                    .filter { !$0.isEmpty }
                if lines.count >= count {
                    return try lines.prefix(count).map(ActionRecord.init(jsonLine:))
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }

        XCTFail("Timed out waiting for \(count) action log records at \(logURL.path)")
        return []
    }

    @MainActor
    private func tapWhenVisible(_ element: XCUIElement, in container: XCUIElement, attempts: Int = 6) {
        for _ in 0..<attempts {
            if element.isHittable {
                element.tap()
                return
            }
            container.swipeUp()
        }
        XCTFail("Element was never hittable: \(element)")
    }

    @MainActor
    private func findButton(
        in app: XCUIApplication,
        candidates: [String],
        container: XCUIElement,
        attempts: Int = 6
    ) -> XCUIElement? {
        for _ in 0..<attempts {
            for candidate in candidates {
                let button = app.buttons[candidate]
                if button.exists {
                    return button
                }
            }
            container.swipeUp()
        }
        return nil
    }
}

private struct ActionRecord: Decodable {
    let action: String
    let headline: String

    init(jsonLine: String) throws {
        self = try JSONDecoder().decode(ActionRecord.self, from: Data(jsonLine.utf8))
    }
}

private enum AccessibilityID {
    enum Button {
        static let repair = "LonghouseMenuBar.Button.Repair"
        static let openLogs = "LonghouseMenuBar.Button.OpenLogs"
    }
}
