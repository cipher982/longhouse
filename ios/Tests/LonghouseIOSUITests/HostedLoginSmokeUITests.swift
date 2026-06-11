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

    func testHostedBootstrapStartsFromControlPlaneOpenInstanceURL() {
        let app = launchApp()
        let continueButton = app.buttons["login.continueWithLonghouse"]

        XCTAssertTrue(continueButton.waitForExistence(timeout: 5))
        continueButton.tap()

        let attemptedURLLabel = app.staticTexts["login.hostedAuthAttemptURL"]
        XCTAssertTrue(attemptedURLLabel.waitForExistence(timeout: 5))
        XCTAssertEqual(
            attemptedURLLabel.label,
            "https://control.longhouse.ai/auth/native/open-instance"
        )
    }

    private func launchApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.resetState] = "1"
        app.launchEnvironment[LaunchEnvironment.captureHostedAuth] = "1"
        app.launch()
        return app
    }
}
