import XCTest

/// One app for the UI tests that run against a fixture, launched once and switched from
/// fixture to fixture in process.
///
/// A launch costs a test 5 to 8 s on the bench simulator (terminate, automation session,
/// first idle): 273 s of the 411 s UI suite when every test launched (run 45695623475).
/// The app watches a command directory (UITestFixtureChannel in the app) and, for each
/// command, closes what the last fixture left open and rebuilds its root view with the new
/// fixture's environment, appearance and text size: a new session view, view model and
/// stores, as a launch would make. It relaunches when the running app was not launched
/// with the channel (another test launched its own) and after a test failed, so a broken
/// state never reaches the next test.
@MainActor
enum FixtureApp {
    struct Fixture {
        /// LONGHOUSE_UI_TEST_ keys, the same a launch environment would carry.
        var environment: [String: String]
        var appearance = "light"
        /// A UIContentSizeCategory raw value; nil is the simulator's default size.
        var contentSizeCategory: String?
    }

    private static var app: XCUIApplication?
    private static let channel = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
        .appendingPathComponent("lh-ui-fixture-channel-\(UUID().uuidString)", isDirectory: true)
    private static var relaunchBeforeNextTest = false

    /// The next test starts from a new launch (a failed test may have left anything behind).
    static func relaunchBeforeNextTest(_ value: Bool = true) {
        relaunchBeforeNextTest = relaunchBeforeNextTest || value
    }

    static func open(_ fixture: Fixture, file: StaticString = #filePath, line: UInt = #line) -> XCUIApplication {
        if let app, !relaunchBeforeNextTest, app.state == .runningForeground,
           app.descendants(matching: .any)["ui-test-fixture-ready"].exists {
            send(fixture, to: app, file: file, line: line)
            return app
        }
        relaunchBeforeNextTest = false
        let app = XCUIApplication()
        app.launchEnvironment = fixture.environment
        app.launchEnvironment["LONGHOUSE_UI_TEST_FIXTURE_CHANNEL"] = channel.path
        app.launchArguments += ["-LONGHOUSE_UI_TEST_APPEARANCE", fixture.appearance]
        app.launch()
        self.app = app
        if fixture.contentSizeCategory != nil {
            send(fixture, to: app, file: file, line: line)
        }
        return app
    }

    private static func send(_ fixture: Fixture, to app: XCUIApplication, file: StaticString, line: UInt) {
        let token = UUID().uuidString
        var command: [String: Any] = [
            "token": token,
            "environment": fixture.environment,
            "appearance": fixture.appearance,
        ]
        command["contentSizeCategory"] = fixture.contentSizeCategory
        do {
            try FileManager.default.createDirectory(at: channel, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: command)
            // Atomic: the app reads the file when the rename lands in the directory.
            try data.write(to: channel.appendingPathComponent("command.json"), options: .atomic)
        } catch {
            XCTFail("Could not write the fixture command: \(error)", file: file, line: line)
            return
        }
        let ready = app.descendants(matching: .any)["ui-test-fixture-ready"]
        XCTAssertTrue(
            poll(timeout: 20) { ready.exists && ready.label == token },
            "The app did not switch to the next fixture (last token \(ready.exists ? ready.label : "none"))",
            file: file,
            line: line
        )
        // The app ends editing before it rebuilds; let the keyboard finish leaving, so a
        // test that waits for a keyboard cannot see the last fixture's.
        XCTAssertTrue(
            poll(timeout: 10) { app.keyboards.count == 0 },
            "The last fixture's keyboard did not close",
            file: file,
            line: line
        )
    }
}

/// A UI test that opens fixtures through FixtureApp.
@MainActor
class FixtureUITestCase: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    override func tearDown() {
        if (testRun?.failureCount ?? 0) > 0 {
            FixtureApp.relaunchBeforeNextTest()
        }
        super.tearDown()
    }

    /// Attach a screenshot for a person to look at. The merge gate does not keep them (a
    /// failing test still gets XCTest's own); `make ios-ui-shot` sets LONGHOUSE_UI_SHOTS.
    func attachScreenshot(_ app: XCUIApplication, named name: String) {
        guard ProcessInfo.processInfo.environment["LONGHOUSE_UI_SHOTS"] == "1" else { return }
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
