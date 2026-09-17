import CoreGraphics
import XCTest

/// The Console launch sheet's pickers are plain `List`s of full-width rows. A
/// row has to accept a tap anywhere across it, not only on the name text at its
/// leading edge: the blank half of a row was transparent to hit testing, so
/// tapping the right side — where the checkmark sits — did nothing.
@MainActor
final class LaunchProviderPickerTests: XCTestCase {
    private enum LaunchEnvironment {
        static let launchSessionFixture = "LONGHOUSE_UI_TEST_LAUNCH_SESSION_FIXTURE"
    }

    private enum LaunchArgument {
        static let appearanceOverride = "-LONGHOUSE_UI_TEST_APPEARANCE"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    /// Positive control: the picker opens, and the tap that already worked still
    /// selects. Fails if the harness stops reaching the picker at all.
    func testProviderRowAcceptsTapOnItsName() {
        let app = launchLaunchSheet()
        openProviderPicker(in: app)

        let claude = app.buttons["launch-provider-row-claude"]
        XCTAssertTrue(claude.waitForExistence(timeout: 5), "Provider picker did not list Claude.")
        claude.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5)).tap()

        XCTAssertTrue(
            waitUntil(timeout: 3) { self.selectedProvider(in: app)?.hasPrefix("Claude") == true },
            "Tapping the provider name did not select Claude. row=\(String(describing: selectedProvider(in: app)))"
        )
    }

    /// The reported defect.
    func testProviderRowAcceptsTapOnItsTrailingHalf() {
        let app = launchLaunchSheet()
        openProviderPicker(in: app)

        let claude = app.buttons["launch-provider-row-claude"]
        XCTAssertTrue(claude.waitForExistence(timeout: 5), "Provider picker did not list Claude.")
        attach(app.screenshot(), name: "provider-picker")

        let row = claude.frame
        claude.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()

        XCTAssertTrue(
            waitUntil(timeout: 3) { self.selectedProvider(in: app)?.hasPrefix("Claude") == true },
            "Tapping the trailing side of the Claude row did not select it. row=\(row) app=\(app.frame)"
        )
        attach(app.screenshot(), name: "provider-picker-after-trailing-tap")
    }

    /// The machine chooser is the same list-of-rows shape inside the same flow,
    /// and it had the same dead half.
    func testMachineRowAcceptsTapOnItsTrailingHalf() {
        let app = launchLaunchSheet()
        openMachineChooser(in: app)

        let cinder = app.buttons["launch-machine-row-cinder"]
        XCTAssertTrue(cinder.waitForExistence(timeout: 5), "Machine chooser did not list cinder.")
        attach(app.screenshot(), name: "machine-chooser")

        let row = cinder.frame
        cinder.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()

        XCTAssertTrue(
            waitUntil(timeout: 3) { app.navigationBars["New Session"].exists },
            "Tapping the trailing side of the cinder row did not choose it. row=\(row) app=\(app.frame)"
        )
    }

    private func launchLaunchSheet() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.launchSessionFixture] = "1"
        app.launchArguments += [LaunchArgument.appearanceOverride, "light"]
        app.launch()

        XCTAssertTrue(app.navigationBars["New Session"].waitForExistence(timeout: 20), app.debugDescription)
        return app
    }

    private func openProviderPicker(in app: XCUIApplication) {
        open(picker: "launch-provider-picker", pushed: "Choose Agent", in: app)
    }

    private func openMachineChooser(in app: XCUIApplication) {
        open(picker: "launch-machine-picker", pushed: "Choose Machine", in: app)
    }

    private func open(picker identifier: String, pushed title: String, in app: XCUIApplication) {
        let row = app.descendants(matching: .any)[identifier]
        XCTAssertTrue(row.waitForExistence(timeout: 20), "Launch sheet did not render \(identifier).")
        row.tap()
        if app.navigationBars[title].waitForExistence(timeout: 3) { return }
        // The sheet is still settling right after launch; one tap can be dropped.
        row.tap()
        XCTAssertTrue(
            app.navigationBars[title].waitForExistence(timeout: 5),
            "\(identifier) did not push \(title). \(app.debugDescription)"
        )
    }

    /// The launch sheet's provider row, which carries the selection once the
    /// picker is gone.
    private func selectedProvider(in app: XCUIApplication) -> String? {
        guard app.navigationBars["New Session"].exists else { return nil }
        let row = app.descendants(matching: .any)["launch-provider-picker"]
        return row.exists ? row.label : nil
    }

    private func waitUntil(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        return condition()
    }

    private func attach(_ screenshot: XCUIScreenshot, name: String) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
