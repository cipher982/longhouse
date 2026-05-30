import CoreGraphics
import ImageIO
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

    // The tool-bearing transcript loads and renders (prose + interleaved tool
    // rows) without crashing the WebView pipeline. The demoted-row STYLING and
    // the dropped-result attention treatment are asserted separately and more
    // reliably by TranscriptStyleContractTests (CSS) — WebView DOM text for
    // <summary>/<span> nodes is not dependably exposed to XCUITest.
    func testToolTranscriptRendersWithoutBreakingPipeline() {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.chatFixture] = "tools"
        app.launchEnvironment[LaunchEnvironment.chatEventCount] = "9"
        app.launchArguments += ["-AppleInterfaceStyle", "Light"]
        app.launch()

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 8))
        // Assistant prose renders, confirming the tool-bearing fixture loaded
        // and the timeline built through the tool/orphan-tool pairing path.
        XCTAssertTrue(
            app.staticTexts["The MR was renamed by Oleg at 18:42, then moved back to In Review."]
                .waitForExistence(timeout: 6)
        )
    }

    func testTranscriptStartsPinnedToLatestMessage() {
        let app = launchChatFixture(eventCount: 120)

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Assistant fixture message 119: streaming-style response with enough body to exercise row layout."].waitForExistence(timeout: 5))
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
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
        XCTAssertTrue(app.staticTexts["Longhouse"].waitForExistence(timeout: 5))
        XCTAssertEqual(composer.value as? String, "Send a message to the live Codex session...")
    }

    func testAssistantUpdateKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update", eventCount: 40)
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]
        let liveUpdate = app.staticTexts["Assistant fixture live update at bottom."]

        XCTAssertTrue(currentLastMessage.waitForExistence(timeout: 5))
        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 5))
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testAssistantUpdateWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let liveUpdate = app.staticTexts["Assistant fixture keyboard update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()

        XCTAssertTrue(liveUpdate.waitForExistence(timeout: 10))
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testAssistantStreamingWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-stream-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let finalChunk = app.staticTexts["Assistant fixture streaming update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()

        XCTAssertTrue(finalChunk.waitForExistence(timeout: 10))
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testLargeTranscriptScrollPerformance() {
        let app = launchChatFixture(name: "stress", eventCount: 500)
        let transcript = transcriptElement(app)

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
        addTeardownBlock { [weak self] in
            guard let self, (self.testRun?.failureCount ?? 0) > 0 else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(self.name)-failure"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }
        return app
    }

    private func transcriptElement(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["session-chat-transcript"]
    }

    private func assertNotVisible(
        _ element: XCUIElement,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertFalse(element.exists && element.isHittable, file: file, line: line)
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
