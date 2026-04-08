import XCTest

@MainActor
final class LonghouseMenuBarWindowHostUITests: XCTestCase {
    private let app = XCUIApplication()

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testBrokenFixtureRendersExpectedUI() throws {
        let actionLogURL = makeTempURL(name: "window-ui-actions.jsonl")
        launchApp(fixture: "broken", actionLogURL: actionLogURL)

        let headline = app.staticTexts["Longhouse engine service is stopped"]
        XCTAssertTrue(headline.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Refresh"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.buttons["Doctor"].exists)
        XCTAssertTrue(app.buttons["Repair"].exists)
        XCTAssertTrue(app.buttons["Copy JSON"].exists)
    }

    func testControlsWriteDryRunActionLog() throws {
        let actionLogURL = makeTempURL(name: "window-ui-controls.jsonl")
        launchApp(fixture: "broken", actionLogURL: actionLogURL)

        XCTAssertTrue(app.staticTexts["Longhouse engine service is stopped"].waitForExistence(timeout: 5))

        app.buttons["Refresh"].tap()
        app.buttons["Doctor"].tap()
        app.buttons["Repair"].tap()
        app.buttons["Copy JSON"].tap()
        app.buttons["Logs"].tap()
        app.buttons["Open Longhouse"].tap()

        let expectedActions: Set<String> = [
            "refresh",
            "runDoctor",
            "repairInstall",
            "copyDiagnostics",
            "openLogs",
            "openLonghouse",
        ]

        let observedActions = try waitForActions(in: actionLogURL, expectedCount: expectedActions.count)
        XCTAssertEqual(Set(observedActions), expectedActions)
    }

    private func launchApp(fixture: String, actionLogURL: URL) {
        try? FileManager.default.createDirectory(
            at: actionLogURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        app.launchArguments = [
            "--input", fixtureURL(named: fixture).path,
            "--action-log", actionLogURL.path,
            "--effect-mode", "log-only",
        ]
        app.launch()
        app.activate()
    }

    private func fixtureURL(named name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("../Fixtures/\(name).json")
            .standardizedFileURL
    }

    private func makeTempURL(name: String) -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent(name)
    }

    private func waitForActions(in url: URL, expectedCount: Int) throws -> [String] {
        let fileManager = FileManager.default
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if fileManager.fileExists(atPath: url.path) {
                let content = try String(contentsOf: url)
                let actions = content
                    .split(separator: "\n")
                    .compactMap { line -> String? in
                        guard let data = line.data(using: .utf8),
                              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                            return nil
                        }
                        return object["action"] as? String
                    }
                if Set(actions).count >= expectedCount {
                    return actions
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        XCTFail("Timed out waiting for action log at \(url.path)")
        return []
    }
}
