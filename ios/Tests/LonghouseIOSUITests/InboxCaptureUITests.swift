import XCTest

/// Frames of the inbox gallery fixture in both appearances, for before/after
/// visual comparison (`make ios-ui-shot TEST=InboxCaptureUITests/<test>`). Skipped by
/// the smoke plan method by method: a class-level skip would also swallow an
/// explicit -only-testing selection. They capture, they do not gate.
@MainActor
final class InboxCaptureUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testCaptureInboxGalleryLight() {
        capture(appearance: "light")
    }

    func testCaptureInboxGalleryDark() {
        capture(appearance: "dark")
    }

    private func capture(appearance: String) {
        let app = XCUIApplication()
        app.launchEnvironment["LONGHOUSE_UI_TEST_INBOX_GALLERY_FIXTURE"] = "1"
        app.launchArguments += ["-LONGHOUSE_UI_TEST_APPEARANCE", appearance]
        app.launch()

        XCTAssertTrue(app.navigationBars["Timeline"].waitForExistence(timeout: 10))
        // Let the list settle past its first layout pass.
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "inbox-gallery-\(appearance)"
        shot.lifetime = .keepAlways
        add(shot)
    }
}
