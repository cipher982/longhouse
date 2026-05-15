import XCTest

@MainActor
final class SessionChatUITests: XCTestCase {
    private enum LaunchEnvironment {
        static let chatFixture = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testTranscriptStartsPinnedToLatestMessage() {
        let app = launchChatFixture(eventCount: 120)

        XCTAssertTrue(app.scrollViews["session-chat-transcript"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Assistant fixture message 119: streaming-style response with enough body to exercise row layout."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."].exists)
    }

    func testSendShowsOptimisticMessageImmediatelyAndClearsComposer() {
        let app = launchChatFixture(eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let sendButton = app.buttons["session-chat-send"]
        let message = "ui harness immediate reveal"

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText(message)
        sendButton.tap()

        XCTAssertTrue(app.staticTexts[message].waitForExistence(timeout: 1))
        XCTAssertEqual(composer.value as? String, "Reply")
    }

    func testAssistantUpdateKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update", eventCount: 40)
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]
        let liveUpdate = app.staticTexts["Assistant fixture live update at bottom."]

        XCTAssertTrue(currentLastMessage.waitForExistence(timeout: 5))
        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 5))
        XCTAssertFalse(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."].exists)
    }

    func testAssistantUpdateWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let liveUpdate = app.staticTexts["Assistant fixture keyboard update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()

        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 6))
        XCTAssertFalse(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."].exists)
    }

    func testLargeTranscriptScrollPerformance() {
        let app = launchChatFixture(name: "stress", eventCount: 500)
        let transcript = app.scrollViews["session-chat-transcript"]

        XCTAssertTrue(transcript.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Assistant fixture message 499: streaming-style response with enough body to exercise row layout."].waitForExistence(timeout: 10))

        let options = XCTMeasureOptions()
        options.iterationCount = 3
        measure(options: options) {
            transcript.swipeDown()
            transcript.swipeUp()
            transcript.swipeUp()
        }
    }

    private func launchChatFixture(name: String = "basic", eventCount: Int) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.chatFixture] = name
        app.launchEnvironment[LaunchEnvironment.chatEventCount] = String(eventCount)
        app.launch()
        return app
    }

    private func waitUntilHittable(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "hittable == true")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }
}
