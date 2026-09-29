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

    // Needs the public demo (longhouse.ai). An unreachable demo skips rather
    // than fails so a network blip never blocks an unrelated iOS change; a
    // reachable demo that the app cannot enter still fails.
    func testExploreDemoOpensTheTimelineWithoutAnAccount() throws {
        try XCTSkipUnless(Self.demoIsReachable(), "the public demo is unreachable from this runner")
        let app = launchApp()
        let explore = app.buttons["login.exploreDemo"]

        XCTAssertTrue(explore.waitForExistence(timeout: 5))
        attachFrame(app, named: "login-with-demo-entry")
        explore.tap()

        XCTAssertTrue(app.descendants(matching: .any)["timeline-session-row"].firstMatch.waitForExistence(timeout: 30))
        attachFrame(app, named: "demo-timeline")
    }

    private final class ReachabilityBox: @unchecked Sendable {
        var reachable = false
    }

    private static func demoIsReachable() -> Bool {
        guard let url = URL(string: "https://longhouse.ai/api/auth/methods") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        let box = ReachabilityBox()
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            box.reachable = (response as? HTTPURLResponse)?.statusCode == 200
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 10)
        return box.reachable
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
