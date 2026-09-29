import XCTest

@MainActor
final class HostedLoginSmokeUITests: XCTestCase {
    private enum LaunchEnvironment {
        static let resetState = "LONGHOUSE_UI_TEST_RESET_STATE"
        static let captureHostedAuth = "LONGHOUSE_UI_TEST_CAPTURE_HOSTED_AUTH"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func disabled_testHostedBootstrapShowsContinueButtonWithoutConfiguredServer() {
        let app = launchApp()

        XCTAssertTrue(app.buttons["login.continueWithLonghouse"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["login.serverConfig"].exists)
    }

    func testHostedBootstrapStartsFromControlPlaneOpenInstanceURL() throws {
        let app = launchApp()
        let continueButton = app.buttons["login.continueWithLonghouse"]

        XCTAssertTrue(continueButton.waitForExistence(timeout: 5))
        continueButton.tap()

        let attemptedURLLabel = app.staticTexts["login.hostedAuthAttemptURL"]
        XCTAssertTrue(attemptedURLLabel.waitForExistence(timeout: 5))
        let attemptedURL = URL(string: attemptedURLLabel.label)
        let components = URLComponents(url: try XCTUnwrap(attemptedURL), resolvingAgainstBaseURL: false)
        XCTAssertEqual(components?.scheme, "https")
        XCTAssertEqual(components?.host, "control.longhouse.ai")
        XCTAssertEqual(components?.path, "/auth/native/open-instance")
        XCTAssertNotNil(components?.queryItems?.first(where: { $0.name == "tenant_state" })?.value)
    }

    // Needs network: the demo is the public longhouse.ai server. Runs in the
    // disposable hosted VM, which has it.
    func testExploreDemoOpensTheTimelineWithoutAnAccount() {
        let app = launchApp()
        let explore = app.buttons["login.exploreDemo"]

        XCTAssertTrue(explore.waitForExistence(timeout: 5))
        attachFrame(app, named: "login-with-demo-entry")
        explore.tap()

        XCTAssertTrue(app.descendants(matching: .any)["timeline-session-row"].firstMatch.waitForExistence(timeout: 30))
        XCTAssertFalse(app.buttons["login.continueWithLonghouse"].exists)
        attachFrame(app, named: "demo-timeline")
    }

    private func attachFrame(_ app: XCUIApplication, named name: String) {
        let frame = XCTAttachment(screenshot: app.screenshot())
        frame.name = name
        frame.lifetime = .keepAlways
        add(frame)
    }

    private func launchApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.resetState] = "1"
        app.launchEnvironment[LaunchEnvironment.captureHostedAuth] = "1"
        app.launch()
        return app
    }
}
