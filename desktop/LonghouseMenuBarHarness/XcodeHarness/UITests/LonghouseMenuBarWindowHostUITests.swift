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
        XCTAssertEqual(window.title, "Longhouse Local Health")
        XCTAssertTrue(app.staticTexts["Longhouse engine service is stopped"].waitForExistence(timeout: 5))
        let repairButton = try XCTUnwrap(
            findButton(in: app, candidates: [AccessibilityID.Button.repair, "Repair"], container: window),
            "Repair button was not found"
        )
        tapWhenVisible(repairButton, in: window)

        let copyButton = try XCTUnwrap(
            findButton(in: app, candidates: [AccessibilityID.Button.copyDiagnostics, "Copy Diagnostics", "Copy JSON"], container: window),
            "Copy diagnostics button was not found"
        )
        tapWhenVisible(copyButton, in: window)

        let actions = try waitForActionRecords(at: actionLogURL, count: 2)
        XCTAssertEqual(actions.map(\.action), ["repairInstall", "copyDiagnostics"])
        XCTAssertEqual(Set(actions.map(\.headline)), ["Longhouse engine service is stopped"])

        let attachment = XCTAttachment(screenshot: window.screenshot())
        attachment.name = "broken-window-host"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor
    private func launchApp(fixture: String, actionLogURL: URL) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = [
            "-ApplePersistenceIgnoreState", "YES",
            "--input", fixtureURL(named: fixture).path,
            "--effect-mode", "log-only",
            "--action-log", actionLogURL.path,
        ]
        app.launch()
        app.activate()
        return app
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
        static let copyDiagnostics = "LonghouseMenuBar.Button.CopyDiagnostics"
    }
}
