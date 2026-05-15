import CoreGraphics
import ImageIO
import XCTest

@MainActor
final class SessionChatUITests: XCTestCase {
    private var app: XCUIApplication?

    private enum LaunchEnvironment {
        static let chatFixture = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    override func tearDownWithError() throws {
        if let app, testRun?.failureCount ?? 0 > 0 {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(name)-failure"
            attachment.lifetime = .keepAlways
            add(attachment)
        }
        app = nil
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
        XCTAssertEqual(composer.value as? String, "Send a message to the live Codex session...")
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
        assertScreenIsVisiblyRendered(app)
        XCTAssertFalse(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."].exists)
    }

    func testAssistantStreamingWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-stream-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let finalChunk = app.staticTexts["Assistant fixture streaming update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()

        XCTAssertTrue(waitUntilHittable(finalChunk, timeout: 7))
        assertScreenIsVisiblyRendered(app)
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
        app.launchArguments += ["-AppleInterfaceStyle", "Light"]
        app.launch()
        self.app = app
        return app
    }

    private func waitUntilHittable(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "hittable == true")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func assertScreenIsVisiblyRendered(
        _ app: XCUIApplication,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let screenshot = app.screenshot()
        guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            XCTFail("Could not decode screenshot", file: file, line: line)
            return
        }

        let width = 32
        let height = 64
        let bytesPerPixel = 4
        var pixels = [UInt8](repeating: 0, count: width * height * bytesPerPixel)
        guard let context = CGContext(
            data: &pixels,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * bytesPerPixel,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            XCTFail("Could not create screenshot sampling context", file: file, line: line)
            return
        }

        context.interpolationQuality = .low
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

        var luminanceTotal = 0.0
        var visiblyLitPixels = 0
        for offset in stride(from: 0, to: pixels.count, by: bytesPerPixel) {
            let red = Double(pixels[offset]) / 255.0
            let green = Double(pixels[offset + 1]) / 255.0
            let blue = Double(pixels[offset + 2]) / 255.0
            let luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            luminanceTotal += luminance
            if luminance > 0.08 {
                visiblyLitPixels += 1
            }
        }

        let sampleCount = width * height
        let meanLuminance = luminanceTotal / Double(sampleCount)
        let litPixelFraction = Double(visiblyLitPixels) / Double(sampleCount)
        XCTAssertGreaterThan(meanLuminance, 0.03, "Screen rendered close to black", file: file, line: line)
        XCTAssertGreaterThan(litPixelFraction, 0.02, "Screen did not contain enough visible pixels", file: file, line: line)
    }
}
