import XCTest

/// The timeline (the inbox) over its gallery fixture: every kind of session row.
@MainActor
final class TimelineUITests: FixtureUITestCase {
    /// The timeline lists the gallery's sessions and keeps them through a scroll.
    func testTimelineListsSessionsAndScrolls() {
        let app = FixtureApp.open(FixtureApp.Fixture(environment: ["LONGHOUSE_UI_TEST_INBOX_GALLERY_FIXTURE": "1"]))
        XCTAssertTrue(app.navigationBars["Timeline"].waitToExist(timeout: 20))
        let rows = app.descendants(matching: .any).matching(identifier: "timeline-session-row")
        XCTAssertTrue(rows.firstMatch.waitToExist(timeout: 10), "the timeline rendered no session rows")
        XCTAssertGreaterThan(rows.count, 3, "the gallery fixture has more sessions than this")
        app.scrollViews.firstMatch.swipeUp()
        XCTAssertTrue(rows.firstMatch.waitToExist(timeout: 5), "the timeline lost its rows after a scroll")
    }
}

/// Frames of the inbox gallery fixture for before/after visual comparison
/// (`make ios-ui-shot TEST=InboxCaptureUITests/<test>`). Skipped by the smoke plan method
/// by method (a class-level skip would also swallow an explicit -only-testing selection):
/// they capture, they do not gate.
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

    /// The timeline fires scrolling with their rows (live counters, so the
    /// working rows flare and spark). Record the simulator while it runs:
    /// the fires must stay on their rows through drags and momentum.
    func testCaptureHearthScroll() {
        let app = XCUIApplication()
        app.launchEnvironment["LONGHOUSE_UI_TEST_INBOX_GALLERY_FIXTURE"] = "1"
        app.launchEnvironment["LONGHOUSE_UI_TEST_HEARTH_LIVE"] = "1"
        app.launchArguments += ["-LONGHOUSE_UI_TEST_APPEARANCE", "dark"]
        app.launch()

        XCTAssertTrue(app.navigationBars["Timeline"].waitToExist(timeout: 10))
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        let list = app.scrollViews.firstMatch
        for _ in 0..<2 {
            list.swipeUp(velocity: .slow)
            RunLoop.current.run(until: Date().addingTimeInterval(1.5))
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.name = "hearth-scrolled"
            shot.lifetime = .keepAlways
            add(shot)
            list.swipeDown(velocity: .fast)
            RunLoop.current.run(until: Date().addingTimeInterval(1.5))
        }
    }

    private func capture(appearance: String) {
        let app = XCUIApplication()
        app.launchEnvironment["LONGHOUSE_UI_TEST_INBOX_GALLERY_FIXTURE"] = "1"
        app.launchArguments += ["-LONGHOUSE_UI_TEST_APPEARANCE", appearance]
        app.launch()

        XCTAssertTrue(app.navigationBars["Timeline"].waitToExist(timeout: 10))
        // Let the list settle past its first layout pass.
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "inbox-gallery-\(appearance)"
        shot.lifetime = .keepAlways
        add(shot)
    }
}
