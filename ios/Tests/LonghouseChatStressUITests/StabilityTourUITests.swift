import XCTest

/// Opt-in, read-only tour of real sessions for the transcript stability work
/// (specs/ios-transcript-stability.md, L3). It opens the top sessions of a
/// Runtime Host and drags toward older messages in short bursts with pauses, the
/// way a person reads back, so lazy media and older history arrive while the
/// reader is mid-page. With `LONGHOUSE_STABILITY_LOG=1` the app logs, per
/// session, how many layout shifts the page absorbed for the reader and how big
/// they were; the wrapper script reads those lines.
final class StabilityTourUITests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    func testRealSessionScrollTour() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["LONGHOUSE_RUN_STABILITY_TOUR"] == "1" else {
            throw XCTSkip("The stability tour is opt-in.")
        }
        let app = XCUIApplication()
        app.launchEnvironment["LONGHOUSE_STABILITY_LOG"] = "1"
        for key in ["LONGHOUSE_HEADLESS_SERVER_URL", "LONGHOUSE_HEADLESS_AUTH_TOKEN"] {
            if let value = environment[key], !value.isEmpty {
                app.launchEnvironment[key] = value
            }
        }
        app.launch()

        let rows = app.buttons.matching(identifier: "timeline-session-row")
        XCTAssertTrue(rows.firstMatch.waitForExistence(timeout: 40), "Signed-in timeline did not appear.")
        let sessions = Int(environment["LONGHOUSE_STABILITY_TOUR_SESSIONS"] ?? "") ?? 4
        let bursts = Int(environment["LONGHOUSE_STABILITY_TOUR_BURSTS"] ?? "") ?? 8
        var opened = 0
        for index in 0..<min(sessions, rows.count) {
            let row = rows.element(boundBy: index)
            guard row.waitForExistence(timeout: 10), row.isHittable else { continue }
            row.tap()
            let transcript = app.descendants(matching: .any)["session-chat-transcript"]
            XCTAssertTrue(transcript.waitForExistence(timeout: 30), "Transcript did not open.")
            // Let the first frame and the older-history prefetch settle, then read back.
            Thread.sleep(forTimeInterval: 2.5)
            for _ in 0..<bursts {
                transcript.swipeDown(velocity: .default)
                Thread.sleep(forTimeInterval: 0.9)
            }
            // And forward again: new shifts can come from rows entering from below too.
            for _ in 0..<2 {
                transcript.swipeUp(velocity: .default)
                Thread.sleep(forTimeInterval: 0.6)
            }
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(rows.firstMatch.waitForExistence(timeout: 10))
            opened += 1
        }
        XCTAssertGreaterThan(opened, 0, "No session was opened.")
        print("STABILITY_TOUR_OPENED \(opened)")
    }
}
