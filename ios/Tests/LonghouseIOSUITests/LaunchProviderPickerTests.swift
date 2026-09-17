import CoreGraphics
import XCTest

/// The Console launch sheet's pickers are plain `List`s of full-width rows.
/// A row has to accept a tap anywhere across it, not only on the name text at
/// its leading edge: tapping the trailing side of a provider row did nothing.
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

    /// Positive control for the harness and for the tap that already worked.
    func testProviderRowAcceptsTapOnItsName() {
        let app = launchLaunchSheet()
        openProviderPicker(in: app)

        let claude = app.buttons["Claude"]
        XCTAssertTrue(claude.waitForExistence(timeout: 5), "Provider picker did not list Claude.")
        claude.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5)).tap()

        XCTAssertTrue(
            waitUntil(timeout: 3) { self.summaryRow("Coding agent", in: app)?.label.contains("Claude") == true },
            "Tapping the provider name did not select Claude. row=\(String(describing: summaryRow("Coding agent", in: app)?.label))"
        )
    }

    /// The reported defect.
    func testProviderRowAcceptsTapOnItsTrailingHalf() {
        let app = launchLaunchSheet()
        openProviderPicker(in: app)

        let claude = app.buttons["Claude"]
        XCTAssertTrue(claude.waitForExistence(timeout: 5), "Provider picker did not list Claude.")
        attach(app.screenshot(), name: "provider-picker")

        let row = claude.frame
        claude.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()

        XCTAssertTrue(
            waitUntil(timeout: 3) { self.summaryRow("Coding agent", in: app)?.label.contains("Claude") == true },
            "Tapping the trailing side of the Claude row did not select it. row=\(row) app=\(app.frame)"
        )
        attach(app.screenshot(), name: "provider-picker-after-trailing-tap")
    }

    /// The machine chooser is the same list-of-rows shape inside the same flow.
    func testMachineRowAcceptsTapOnItsTrailingHalf() throws {
        let app = launchLaunchSheet()

        let machineRow = summaryRow("Ready", in: app)
        XCTAssertNotNil(machineRow, "Launch sheet did not render its machine row. \(app.debugDescription)")
        machineRow?.tap()
        XCTAssertTrue(
            app.navigationBars["Choose Machine"].waitForExistence(timeout: 5),
            "Machine row did not open the machine chooser. \(app.debugDescription)"
        )

        // The launch sheet keeps its own "cinder, Ready" row in the tree behind
        // the pushed chooser; only the chooser's copy is on screen.
        let cinder = try XCTUnwrap(
            app.buttons.matching(NSPredicate(format: "label == %@", "cinder, Ready"))
                .allElementsBoundByIndex.first { $0.isHittable },
            "Machine chooser did not expose a hittable cinder row."
        )
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

        XCTAssertTrue(app.navigationBars["New Session"].waitForExistence(timeout: 10), app.debugDescription)
        return app
    }

    private func openProviderPicker(in app: XCUIApplication) {
        let providerRow = summaryRow("Coding agent", in: app)
        XCTAssertNotNil(providerRow, "Launch sheet did not render its provider row. \(app.debugDescription)")
        providerRow?.tap()
        XCTAssertTrue(
            app.navigationBars["Choose Agent"].waitForExistence(timeout: 5),
            "Provider row did not open the agent picker. \(app.debugDescription)"
        )
    }

    /// The launch sheet's summary rows combine their title and subtitle into one
    /// accessibility element, so they are addressed by the half they carry.
    /// Elements behind a pushed screen stay in the tree; prefer the one on screen.
    private func summaryRow(_ containedText: String, in app: XCUIApplication) -> XCUIElement? {
        let predicate = NSPredicate(format: "label CONTAINS %@", containedText)
        let candidates = [
            app.buttons.matching(predicate).firstMatch,
            app.cells.matching(predicate).firstMatch,
            app.otherElements.matching(predicate).firstMatch,
            app.staticTexts[containedText],
        ]
        let present = candidates.filter { $0.exists }
        return present.first { $0.isHittable } ?? present.first
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
