import CoreGraphics
import ImageIO
import XCTest

@MainActor
final class SessionChatUITests: XCTestCase {
    private static let webTranscriptTimeout: TimeInterval = 12

    private enum LaunchEnvironment {
        static let chatFixture = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
    }

    private enum LaunchArgument {
        static let appearanceOverride = "-LONGHOUSE_UI_TEST_APPEARANCE"
    }

    private enum Appearance: String {
        case light
        case dark
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
        app.launchArguments += [LaunchArgument.appearanceOverride, Appearance.light.rawValue]
        app.launch()

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 8))
        let renderStatus = app.staticTexts["transcript-benchmark-status"]
        XCTAssertTrue(renderStatus.waitForExistence(timeout: 8))
        // Consume the WebKit render beacon rather than a DOM accessibility
        // child: the simulator intermittently publishes the WebView without
        // any of its text descendants even after rendering has completed.
        XCTAssertTrue(
            waitForLabel(renderStatus, containing: "stage=rendered", timeout: Self.webTranscriptTimeout),
            renderStatus.label
        )
    }

    func testCaptureToolsTranscriptLightScreenshot() throws {
        try captureSessionScreenshot(
            fixtureName: "marketing",
            eventCount: 10,
            appearance: .light,
            outputName: "session-light.png"
        )
    }

    func testCaptureToolsTranscriptDarkScreenshot() throws {
        try captureSessionScreenshot(
            fixtureName: "marketing",
            eventCount: 10,
            appearance: .dark,
            outputName: "session-dark.png"
        )
    }

    func testTranscriptStartsPinnedToLatestMessage() {
        let app = launchChatFixture(eventCount: 120)
        let latestMessage = app.staticTexts["Assistant fixture message 119: streaming-style response with enough body to exercise row layout."]

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 5))
        XCTAssertTrue(latestMessage.waitForExistence(timeout: Self.webTranscriptTimeout))
        assertClearsBottomChrome(latestMessage, app: app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testTranscriptStartsPinnedWithUserTailClearOfBottomChrome() {
        let app = launchChatFixture(eventCount: 119)
        let latestMessage = app.staticTexts["User fixture message 118: request text for chat scroll anchoring."]

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 5))
        XCTAssertTrue(latestMessage.waitForExistence(timeout: Self.webTranscriptTimeout))
        assertClearsBottomChrome(latestMessage, app: app)
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

        XCTAssertTrue(app.staticTexts[message].waitForExistence(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(app.staticTexts["Longhouse"].waitForExistence(timeout: 5))
        XCTAssertEqual(composer.value as? String, "Send a message to the live Codex session...")
    }

    func testConsoleReplyReplacesOptimisticBubbleWithoutDuplicateWorkingRow() {
        let app = launchChatFixture(name: "console-reconcile", eventCount: 0)
        let composer = app.textFields["session-chat-composer"]
        let sendButton = app.buttons["session-chat-send"]
        let message = "console reconciliation probe"

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText(message)
        sendButton.tap()

        XCTAssertTrue(app.staticTexts["Console fixture durable reply."].waitForExistence(timeout: Self.webTranscriptTimeout))
        XCTAssertEqual(app.staticTexts.matching(identifier: message).count, 1)
        XCTAssertFalse(app.staticTexts["Working..."].exists)
    }

    func testKeyboardFocusKeepsLatestTranscriptMessageVisible() {
        let app = launchChatFixture(eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]

        XCTAssertTrue(currentLastMessage.waitForExistence(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(waitUntilHittable(currentLastMessage, timeout: 5))
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("typing keeps transcript pinned")

        XCTAssertTrue(waitUntilHittable(currentLastMessage, timeout: 5))
        assertAnchoredAboveBottomChrome(currentLastMessage, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testAssistantUpdateKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update", eventCount: 40)
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]
        let liveUpdate = app.staticTexts["Assistant fixture live update at bottom."]

        XCTAssertTrue(currentLastMessage.waitForExistence(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 5))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testLongAssistantUpdateKeepsWrappedTailAboveBottomChrome() {
        let app = launchChatFixture(name: "assistant-update-long", eventCount: 40)
        let liveUpdate = app.staticTexts["Assistant fixture live update with wrapped tail above the floating composer card."]

        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 10))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testAssistantUpdateWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-update-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let liveUpdate = app.staticTexts["Assistant fixture keyboard update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        XCTAssertTrue(
            app.keyboards.firstMatch.waitForExistence(timeout: 3),
            "Composer keyboard should appear promptly"
        )

        XCTAssertTrue(liveUpdate.waitForExistence(timeout: Self.webTranscriptTimeout))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func testAssistantStreamingWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let app = launchChatFixture(name: "assistant-stream-keyboard", eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let finalChunk = app.staticTexts["Assistant fixture streaming update at bottom."]

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        XCTAssertTrue(
            app.keyboards.firstMatch.waitForExistence(timeout: 3),
            "Composer keyboard should appear promptly"
        )

        XCTAssertTrue(finalChunk.waitForExistence(timeout: Self.webTranscriptTimeout))
        assertAnchoredAboveBottomChrome(finalChunk, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."])
    }

    func disabled_testLargeTranscriptScrollPerformance() {
        let app = launchChatFixture(name: "stress", eventCount: 500)
        let transcript = transcriptElement(app)

        XCTAssertTrue(transcript.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Assistant fixture message 499: streaming-style response with enough body to exercise row layout."].waitForExistence(timeout: Self.webTranscriptTimeout))

        let options = XCTMeasureOptions()
        options.iterationCount = 3
        measure(options: options) {
            transcript.swipeDown()
            transcript.swipeUp()
            transcript.swipeUp()
        }
    }

    private func launchChatFixture(
        name: String = "basic",
        eventCount: Int,
        appearance: Appearance = .light
    ) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment[LaunchEnvironment.chatFixture] = name
        app.launchEnvironment[LaunchEnvironment.chatEventCount] = String(eventCount)
        app.launchArguments += [LaunchArgument.appearanceOverride, appearance.rawValue]
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

    private func captureSessionScreenshot(
        fixtureName: String,
        eventCount: Int,
        appearance: Appearance,
        outputName: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        let app = launchChatFixture(name: fixtureName, eventCount: eventCount, appearance: appearance)

        XCTAssertTrue(transcriptElement(app).waitForExistence(timeout: 8), file: file, line: line)
        // WebKit does not reliably publish DOM text into XCUITest's
        // cross-process accessibility tree. The native render beacon is the
        // authoritative signal that the deterministic fixture reached the DOM.
        let renderStatus = app.staticTexts["transcript-benchmark-status"]
        XCTAssertTrue(renderStatus.waitForExistence(timeout: 8), file: file, line: line)
        XCTAssertTrue(
            waitForLabel(renderStatus, containing: "stage=rendered", timeout: Self.webTranscriptTimeout),
            renderStatus.label,
            file: file,
            line: line
        )

        let screenshot = try waitForScreenshotMatchingAppearance(app, appearance: appearance, timeout: 6)
        let directory = URL(fileURLWithPath: "/tmp/lh-shots", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try screenshot.pngRepresentation.write(to: directory.appendingPathComponent(outputName), options: .atomic)
    }

    private func waitForScreenshotMatchingAppearance(
        _ app: XCUIApplication,
        appearance: Appearance,
        timeout: TimeInterval,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws -> XCUIScreenshot {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(timeout))
        var lastScreenshot = app.screenshot()
        var lastMeanLuminance = meanLuminance(of: lastScreenshot)

        while clock.now < deadline {
            if let meanLuminance = lastMeanLuminance,
               screenshot(meanLuminance: meanLuminance, matches: appearance) {
                return lastScreenshot
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
            lastScreenshot = app.screenshot()
            lastMeanLuminance = meanLuminance(of: lastScreenshot)
        }

        let lastMeanDescription: String
        if let lastMeanLuminance {
            lastMeanDescription = String(format: "%.3f", lastMeanLuminance)
        } else {
            lastMeanDescription = "unreadable"
        }
        XCTFail(
            "Timed out waiting for \(appearance.rawValue) screenshot luminance; last mean=\(lastMeanDescription)",
            file: file,
            line: line
        )
        return lastScreenshot
    }

    private func screenshot(meanLuminance: Double, matches appearance: Appearance) -> Bool {
        switch appearance {
        case .light:
            return meanLuminance > 0.55
        case .dark:
            return meanLuminance < 0.35
        }
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

    private func waitForLabel(_ element: XCUIElement, containing value: String, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "label CONTAINS %@", value)
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func assertClearsBottomChrome(
        _ element: XCUIElement,
        app: XCUIApplication,
        minimumGap: CGFloat = 0,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let bottomChromeCard = app.descendants(matching: .any)["session-chat-bottom-chrome-card"]
        XCTAssertTrue(bottomChromeCard.waitForExistence(timeout: 5), file: file, line: line)
        let gap = waitForBottomGap(
            element,
            bottomChromeCard: bottomChromeCard,
            minimumGap: minimumGap,
            maximumGap: .greatestFiniteMagnitude
        ) ?? .nan
        XCTAssertGreaterThanOrEqual(
            gap,
            minimumGap,
            "Latest transcript row never settled clear of the floating control card",
            file: file,
            line: line
        )
    }

    private func assertAnchoredAboveBottomChrome(
        _ element: XCUIElement,
        app: XCUIApplication,
        minimumGap: CGFloat = 8,
        maximumGap: CGFloat = 96,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let bottomChromeCard = app.descendants(matching: .any)["session-chat-bottom-chrome-card"]
        XCTAssertTrue(bottomChromeCard.waitForExistence(timeout: 5), file: file, line: line)

        let gap = waitForBottomGap(
            element,
            bottomChromeCard: bottomChromeCard,
            minimumGap: minimumGap,
            maximumGap: maximumGap
        ) ?? .nan
        XCTAssertGreaterThanOrEqual(
            gap,
            minimumGap,
            "Latest transcript row overlaps the floating control card",
            file: file,
            line: line
        )
        XCTAssertLessThanOrEqual(
            gap,
            maximumGap,
            "Latest transcript row is visibly detached from the floating control card",
            file: file,
            line: line
        )
    }

    /// Accessibility geometry can briefly report infinite coordinates while
    /// WebKit relayout and keyboard presentation cross process boundaries.
    /// Wait for the layout invariant, not merely for the element to exist.
    private func waitForBottomGap(
        _ element: XCUIElement,
        bottomChromeCard: XCUIElement,
        minimumGap: CGFloat,
        maximumGap: CGFloat,
        timeout: Duration = .seconds(5)
    ) -> CGFloat? {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        var lastFiniteGap: CGFloat?
        while clock.now < deadline {
            let elementFrame = element.frame
            let chromeFrame = bottomChromeCard.frame
            let gap = chromeFrame.minY - elementFrame.maxY
            if gap.isFinite {
                lastFiniteGap = gap
                if gap >= minimumGap && gap <= maximumGap { return gap }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return lastFiniteGap
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

    private func meanLuminance(of screenshot: XCUIScreenshot) -> Double? {
        guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            return nil
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
            return nil
        }

        context.interpolationQuality = .low
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

        var luminanceTotal = 0.0
        for offset in stride(from: 0, to: pixels.count, by: bytesPerPixel) {
            let red = Double(pixels[offset]) / 255.0
            let green = Double(pixels[offset + 1]) / 255.0
            let blue = Double(pixels[offset + 2]) / 255.0
            luminanceTotal += 0.2126 * red + 0.7152 * green + 0.0722 * blue
        }
        return luminanceTotal / Double(width * height)
    }
}
