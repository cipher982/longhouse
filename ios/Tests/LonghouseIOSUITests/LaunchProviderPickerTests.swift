import CoreGraphics
import XCTest

/// The Console launch sheet's pickers are plain `List`s of full-width rows. A
/// row has to accept a tap anywhere across it, not only on the name text at its
/// leading edge: the blank half of a row was transparent to hit testing, so
/// tapping the right side — where the checkmark sits — did nothing.
@MainActor
final class LaunchProviderPickerTests: FixtureUITestCase {
    /// The reported defect, then the positive control: the trailing half of the Claude
    /// row selects Claude, and the name of the Codex row (the fixture's default) selects
    /// Codex again, so each tap is seen to change the selection.
    func testProviderRowAcceptsTapAcrossItsWidth() {
        let app = launchLaunchSheet()
        XCTAssertTrue(selectedProvider(in: app)?.hasPrefix("Codex") == true, "the fixture starts on Codex")

        openProviderPicker(in: app)
        let claude = app.buttons["launch-provider-row-claude"]
        XCTAssertTrue(claude.waitToExist(timeout: 5), "Provider picker did not list Claude.")
        attachScreenshot(app, named: "provider-picker")
        let row = claude.frame
        claude.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()
        XCTAssertTrue(
            poll(timeout: 3) { self.selectedProvider(in: app)?.hasPrefix("Claude") == true },
            "Tapping the trailing side of the Claude row did not select it. row=\(row) app=\(app.frame)"
        )

        openProviderPicker(in: app)
        let codex = app.buttons["launch-provider-row-codex"]
        XCTAssertTrue(codex.waitToExist(timeout: 5), "Provider picker did not list Codex.")
        codex.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5)).tap()
        XCTAssertTrue(
            poll(timeout: 3) { self.selectedProvider(in: app)?.hasPrefix("Codex") == true },
            "Tapping the provider name did not select Codex. row=\(String(describing: selectedProvider(in: app)))"
        )
    }

    /// The machine chooser is the same list-of-rows shape inside the same flow,
    /// and it had the same dead half.
    func testMachineRowAcceptsTapOnItsTrailingHalf() {
        let app = launchLaunchSheet()
        openMachineChooser(in: app)

        let cinder = app.buttons["launch-machine-row-cinder"]
        XCTAssertTrue(cinder.waitToExist(timeout: 5), "Machine chooser did not list cinder.")
        attachScreenshot(app, named: "machine-chooser")

        let row = cinder.frame
        cinder.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()

        XCTAssertTrue(
            poll(timeout: 3) { app.navigationBars["New Session"].exists },
            "Tapping the trailing side of the cinder row did not choose it. row=\(row) app=\(app.frame)"
        )
    }

    /// The model row says what it is, and its chooser lists recent models with when each
    /// was last used. The provider row directly above already reads "Coding agent", so a
    /// bare provider name or a "Coding model" subtitle on the model row reads as a
    /// near-duplicate label rather than as an answer about which model will run.
    func testModelRowStatesTheChoiceAndListsRecentModels() {
        let app = launchLaunchSheet()

        let row = app.descendants(matching: .any)["launch-model-picker"]
        XCTAssertTrue(row.waitToExist(timeout: 20), "Launch sheet did not render the model row.")
        XCTAssertTrue(row.label.contains("Model ·"), "Model row does not name itself. label=\(row.label)")
        XCTAssertFalse(
            row.label.contains("Coding model"),
            "Model row still uses the near-duplicate subtitle. label=\(row.label)"
        )
        attachScreenshot(app, named: "launch-model-row")

        open(picker: "launch-model-picker", pushed: "Choose Model", in: app)
        let recent = app.buttons["launch-model-row-gpt-5.6-luna"]
        XCTAssertTrue(recent.waitToExist(timeout: 5), "Model chooser did not list the recent model.")
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'Last used '")).firstMatch.waitToExist(timeout: 3),
            "Recent model did not render a relative last-used label."
        )
        attachScreenshot(app, named: "model-chooser")
    }

    private func launchLaunchSheet() -> XCUIApplication {
        let app = FixtureApp.open(FixtureApp.Fixture(environment: ["LONGHOUSE_UI_TEST_LAUNCH_SESSION_FIXTURE": "1"]))
        XCTAssertTrue(app.navigationBars["New Session"].waitToExist(timeout: 20), app.debugDescription)
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
        XCTAssertTrue(row.waitToExist(timeout: 20), "Launch sheet did not render \(identifier).")
        row.tap()
        if app.navigationBars[title].waitToExist(timeout: 3) { return }
        // The sheet is still settling right after launch; one tap can be dropped.
        row.tap()
        XCTAssertTrue(
            app.navigationBars[title].waitToExist(timeout: 5),
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
}
