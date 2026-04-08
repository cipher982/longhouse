import XCTest

@MainActor
final class LonghouseMenuBarWindowHostUITests: XCTestCase {
    private let app = XCUIApplication()

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testBrokenFixtureLaunchesWindowHost() throws {
        launchApp(fixture: "broken")

        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 5), "Window host did not appear")
        XCTAssertEqual(window.title, "Longhouse Local Health")

        let attachment = XCTAttachment(screenshot: window.screenshot())
        attachment.name = "broken-window-host"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func launchApp(fixture: String) {
        app.launchArguments = [
            "--input", fixtureURL(named: fixture).path,
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
}
