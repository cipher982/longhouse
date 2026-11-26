import CoreGraphics
import ImageIO
import XCTest

/// What every session chat UI test shares: launching a fixture, waits and layout assertions.
/// The tests are split over the classes below because parallel testing hands a simulator
/// clone whole classes, and this was one class of 34 tests, three quarters of the UI scheme.
@MainActor
class SessionChatUITestCase: FixtureUITestCase {
    /// Every positive wait below is scaled by this. A hosted macOS VM runs the
    /// same UI three times slower on a bad day (compile 3.4 vs 10.7 min), and
    /// WebKit publishing text to XCUITest, or XCUITest answering a snapshot, can
    /// take longer than the 5, 8 and 12 second waits these were written with:
    /// "Waiting 12.0s" lasted 25 s and still missed the element (run
    /// 36658111569). A wait returns as soon as its condition holds, so patience
    /// costs nothing on a healthy runner; only negative waits stay short.
    fileprivate static let patience: TimeInterval = 3
    fileprivate static func patient(_ seconds: TimeInterval) -> TimeInterval { seconds * patience }
    fileprivate static let webTranscriptTimeout: TimeInterval = 12 * patience

    fileprivate enum LaunchEnvironment {
        static let chatFixture = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
        static let chatEventCount = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
        static let mobileDetailDelayMs = "LONGHOUSE_UI_TEST_MOBILE_DETAIL_DELAY_MS"
        static let mobileTailDelayMs = "LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS"
        static let chatTriggerPath = "LONGHOUSE_UI_TEST_CHAT_TRIGGER_PATH"
    }

    fileprivate enum Appearance: String {
        case light
        case dark
    }

    func disabled_testLargeTranscriptScrollPerformance() {
        let app = launchChatFixture(name: "stress", eventCount: 500)
        let transcript = transcriptElement(app)

        XCTAssertTrue(transcript.waitToExist(timeout: Self.patient(10)))
        XCTAssertTrue(app.staticTexts["Assistant fixture message 499: streaming-style response with enough body to exercise row layout."].waitToExist(timeout: Self.webTranscriptTimeout))

        let options = XCTMeasureOptions()
        options.iterationCount = 3
        measure(options: options) {
            transcript.swipeDown()
            transcript.swipeUp()
            transcript.swipeUp()
        }
    }

    /// The app showing a chat fixture: a new session view over the named fixture, opened
    /// in the app the earlier tests used (FixtureApp) or a new launch.
    fileprivate func launchChatFixture(
        name: String = "basic",
        eventCount: Int,
        appearance: Appearance = .light,
        tailDelayMs: Int? = nil,
        detailDelayMs: Int? = nil,
        dynamicTypeCategory: String? = nil,
        triggerPath: String? = nil,
        file: StaticString = #filePath,
        line: UInt = #line
    ) -> XCUIApplication {
        var environment = [
            LaunchEnvironment.chatFixture: name,
            LaunchEnvironment.chatEventCount: String(eventCount),
        ]
        environment[LaunchEnvironment.chatTriggerPath] = triggerPath
        environment[LaunchEnvironment.mobileTailDelayMs] = tailDelayMs.map(String.init)
        environment[LaunchEnvironment.mobileDetailDelayMs] = detailDelayMs.map(String.init)
        let app = FixtureApp.open(
            FixtureApp.Fixture(
                environment: environment,
                appearance: appearance.rawValue,
                contentSizeCategory: dynamicTypeCategory
            ),
            file: file,
            line: line
        )
        addTeardownBlock { [weak self] in
            guard let self, (self.testRun?.failureCount ?? 0) > 0 else { return }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "\(self.name)-failure"
            attachment.lifetime = .keepAlways
            self.add(attachment)
        }
        return app
    }

    /// A file the fixture waits for before it sends its scripted update (LONGHOUSE_UI_TEST_CHAT_TRIGGER_PATH).
    fileprivate func makeTrigger() -> URL {
        let trigger = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-fixture-trigger-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: trigger) }
        return trigger
    }

    fileprivate func fire(_ trigger: URL) {
        XCTAssertNoThrow(try "1".write(to: trigger, atomically: true, encoding: .utf8))
    }

    /// Wait for the transcript to finish its first render: the native beacon, because WebKit
    /// does not reliably publish DOM text to XCUITest's accessibility tree.
    fileprivate func waitForRenderedTranscript(
        _ app: XCUIApplication,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(8)), file: file, line: line)
        let renderStatus = app.staticTexts["transcript-benchmark-status"]
        XCTAssertTrue(renderStatus.waitToExist(timeout: Self.patient(8)), file: file, line: line)
        XCTAssertTrue(
            waitForLabel(renderStatus, containing: "stage=rendered", timeout: Self.webTranscriptTimeout),
            renderStatus.label,
            file: file,
            line: line
        )
    }

    fileprivate func captureSessionScreenshot(
        fixtureName: String,
        eventCount: Int,
        appearance: Appearance,
        outputName: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        let app = launchChatFixture(name: fixtureName, eventCount: eventCount, appearance: appearance)

        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(8)), file: file, line: line)
        // WebKit does not reliably publish DOM text into XCUITest's
        // cross-process accessibility tree. The native render beacon is the
        // authoritative signal that the deterministic fixture reached the DOM.
        let renderStatus = app.staticTexts["transcript-benchmark-status"]
        XCTAssertTrue(renderStatus.waitToExist(timeout: Self.patient(8)), file: file, line: line)
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
        // Also attached, so `make ios-ui-shot` exports the frame from a VM.
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = (outputName as NSString).deletingPathExtension
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    fileprivate func waitForScreenshotMatchingAppearance(
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

    fileprivate func screenshot(meanLuminance: Double, matches appearance: Appearance) -> Bool {
        switch appearance {
        case .light:
            return meanLuminance > 0.55
        case .dark:
            return meanLuminance < 0.35
        }
    }

    fileprivate func transcriptElement(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["session-chat-transcript"]
    }

    /// The element is gone, or its settled frame lies outside the screen.
    ///
    /// This does not ask `isHittable`. While WebKit relayouts under the keyboard
    /// it publishes text with a frame that is briefly invalid (the same window
    /// `waitForBottomGap` waits out), and XCTest answers `isHittable` on such an
    /// element with a hard failure, "Activation point invalid and no suggested
    /// hit points based on element frame", instead of `false`. It failed
    /// testAssistantStreamingWithKeyboardOpenKeepsPinnedTranscriptAtBottom on
    /// hosted CI (run 36368089332) and once in thirty runs on a loaded bench.
    fileprivate func assertNotVisible(
        _ element: XCUIElement,
        in app: XCUIApplication,
        timeout: TimeInterval = SessionChatUITestCase.patient(5),
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let deadline = Date().addingTimeInterval(timeout)
        var visible = true
        var sawValidFrame = false
        while true {
            if !element.exists {
                visible = false
                break
            }
            let frame = element.frame
            let screen = app.frame
            if frame.isFiniteAndNonNull, screen.isFiniteAndNonNull {
                sawValidFrame = true
                visible = frame.intersects(screen)
                if !visible { break }
            }
            if Date() >= deadline { break }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        let reason = sawValidFrame ? "is still on screen" : "never reported a valid frame"
        XCTAssertFalse(visible, "\(element) \(reason)", file: file, line: line)
    }

    fileprivate func assertTranscriptElementsVisibleInOrder(
        _ elements: [XCUIElement],
        in app: XCUIApplication,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let screen = app.frame
        XCTAssertTrue(screen.isFiniteAndNonNull, "The app reported no usable screen frame", file: file, line: line)
        var previousFrame: CGRect?
        for element in elements {
            let frame = element.frame
            XCTAssertTrue(element.exists, "Transcript row does not exist: \(element)", file: file, line: line)
            XCTAssertTrue(frame.isFiniteAndNonNull, "Transcript row has no rendered frame: \(element)", file: file, line: line)
            XCTAssertTrue(frame.intersects(screen), "Transcript row is not visible on screen: \(element)", file: file, line: line)
            if let previousFrame {
                XCTAssertGreaterThan(
                    frame.minY,
                    previousFrame.maxY,
                    "Transcript rows are not top-to-bottom in the expected chronology: \(element)",
                    file: file,
                    line: line
                )
            }
            previousFrame = frame
        }
    }

    /// Give the composer keyboard focus, which every composer test needs first.
    ///
    /// Waiting for the field to exist was not enough: on the hosted VM a
    /// synthesized tap occasionally reached an existing, hittable composer and
    /// left no focus, no caret and no keyboard (run 38015970561: the update was
    /// already rendered, the tap synthesized cleanly, nothing changed after it).
    /// Wait until the field can take the tap; if the first tap brings up no
    /// keyboard, keep the evidence and tap once more. A composer that never
    /// takes focus still fails, so a real focus bug cannot hide here. The
    /// keyboard is the signal: the CI simulator shows the software keyboard,
    /// and a private focus key would crash the run rather than fail it.
    fileprivate func focusComposer(_ composer: XCUIElement, in app: XCUIApplication) {
        XCTAssertTrue(composer.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(waitUntilHittable(composer, timeout: Self.patient(5)), "Composer should accept a tap")
        composer.tap()
        if app.keyboards.firstMatch.waitToExist(timeout: Self.patient(3)) { return }
        XCTContext.runActivity(named: "First composer tap took no focus; tapping once more") { activity in
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "composer-tap-without-focus"
            screenshot.lifetime = .keepAlways
            activity.add(screenshot)
        }
        composer.tap()
        XCTAssertTrue(
            app.keyboards.firstMatch.waitToExist(timeout: Self.patient(3)),
            "Composer keyboard should appear promptly"
        )
    }

    fileprivate func waitUntilHittable(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        poll(timeout: timeout) { element.exists && element.isHittable }
    }

    fileprivate func waitForLabel(_ element: XCUIElement, containing value: String, timeout: TimeInterval) -> Bool {
        poll(timeout: timeout) { element.exists && element.label.contains(value) }
    }

    fileprivate func assertClearsBottomChrome(
        _ element: XCUIElement,
        app: XCUIApplication,
        minimumGap: CGFloat = 0,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let bottomChromeCard = app.descendants(matching: .any)["session-chat-bottom-chrome-card"]
        XCTAssertTrue(bottomChromeCard.waitToExist(timeout: Self.patient(5)), file: file, line: line)
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

    fileprivate func assertAnchoredAboveBottomChrome(
        _ element: XCUIElement,
        app: XCUIApplication,
        minimumGap: CGFloat = 8,
        maximumGap: CGFloat = 96,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let bottomChromeCard = app.descendants(matching: .any)["session-chat-bottom-chrome-card"]
        XCTAssertTrue(bottomChromeCard.waitToExist(timeout: Self.patient(5)), file: file, line: line)

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
    fileprivate func waitForBottomGap(
        _ element: XCUIElement,
        bottomChromeCard: XCUIElement,
        minimumGap: CGFloat,
        maximumGap: CGFloat,
        timeout: Duration = .seconds(SessionChatUITestCase.patient(5))
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

    fileprivate func assertScreenIsVisiblyRendered(
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

    fileprivate func meanLuminance(of screenshot: XCUIScreenshot) -> Double? {
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

/// Transcript rendering, and the marketing captures (skipped by the smoke plan; `make
/// ios-ui-shot` runs them by this class name).
@MainActor
final class SessionChatUITests: SessionChatUITestCase {
    /// Tool-heavy and narration transcripts reach a finished render: the renderer accepted
    /// every row kind these fixtures carry. The styling of those rows is asserted by
    /// TranscriptStyleContractTests and the renderer's own Vitest suite; WebView DOM text
    /// for <summary>/<span> nodes is not dependably exposed to XCUITest.
    func testToolAndNarrationTranscriptsRender() {
        for fixture in ["tools", "narration"] {
            let app = launchChatFixture(name: fixture, eventCount: fixture == "tools" ? 9 : 0)
            XCTContext.runActivity(named: "\(fixture) fixture renders") { _ in
                waitForRenderedTranscript(app)
            }
            attachScreenshot(app, named: "\(fixture)-transcript")
        }
    }

    /// Background-job results arrive as provider notifications with up to 2000
    /// characters of output. Each is one collapsed row until tapped.
    func testProviderNotificationRendersAsCompactRow() {
        let app = launchChatFixture(name: "provider-notification", eventCount: 0)
        waitForRenderedTranscript(app)

        // WebKit exposes a <summary> as a button and a <pre> as static text.
        let title = app.buttons.containing(NSPredicate(format: "label CONTAINS %@", "Background job bg_96 has completed")).firstMatch
        let output = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "artifact://335")).firstMatch
        XCTAssertTrue(title.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertFalse(output.exists, "the job's output stays behind the row until it is tapped")
        let closeNotice = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "left any that remain alone"))
            .firstMatch
        XCTAssertTrue(
            closeNotice.waitToExist(timeout: Self.webTranscriptTimeout),
            "the close notice keeps its cleanup line"
        )
        attachScreenshot(app, named: "provider-notification-collapsed")

        title.tap()
        XCTAssertTrue(output.waitToExist(timeout: Self.webTranscriptTimeout))
        attachScreenshot(app, named: "provider-notification-expanded")
    }

    func testLongLoadingTitleStaysClearOfOverflowControl() {
        let app = launchChatFixture(
            name: "loading-long-title",
            eventCount: 9,
            appearance: .dark,
            tailDelayMs: 30000,
            detailDelayMs: 30000
        )

        let title = app.descendants(matching: .any)["session-navigation-title"]
        let actions = app.buttons["Session actions"]
        XCTAssertTrue(title.waitToExist(timeout: Self.patient(8)))
        XCTAssertTrue(actions.waitToExist(timeout: Self.patient(8)))
        XCTAssertLessThan(title.frame.maxX, actions.frame.minX)
        attachScreenshot(app, named: "session-loading-long-title")
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

    /// A lite page sends a tool's output as its collapsed preview; expanding
    /// the row loads the whole body and keeps the row open.
    func testLiteToolRowLoadsItsFullBodyWhenExpanded() {
        let app = launchChatFixture(name: "lite-bodies", eventCount: 0)
        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(5)))
        // WebKit may expose the summary as text, a button or a group; match the element itself.
        let command = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "pytest -q tests/")).firstMatch
        XCTAssertTrue(command.waitToExist(timeout: Self.webTranscriptTimeout))
        attachScreenshot(app, named: "lite-row-collapsed")

        command.tap()
        // The loading note is transient (the fixture answers in ~0.6 s), so the
        // test asserts the end state: the whole body, and no note left behind.
        let tail = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Full body loaded: 40 passed")).firstMatch
        XCTAssertTrue(tail.waitToExist(timeout: Self.webTranscriptTimeout))
        let loadingNote = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Loading the full output")).firstMatch
        XCTAssertFalse(loadingNote.exists)
        attachScreenshot(app, named: "lite-row-loaded")
    }

    func testTranscriptStartsPinnedToLatestMessage() {
        let app = launchChatFixture(eventCount: 120)
        let latestMessage = app.staticTexts["Assistant fixture message 119: streaming-style response with enough body to exercise row layout."]

        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(5)))
        XCTAssertTrue(latestMessage.waitToExist(timeout: Self.webTranscriptTimeout))
        assertClearsBottomChrome(latestMessage, app: app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }

    func testTranscriptStartsPinnedWithUserTailClearOfBottomChrome() {
        let app = launchChatFixture(eventCount: 119)
        let latestMessage = app.staticTexts["User fixture message 118: request text for chat scroll anchoring."]

        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(5)))
        XCTAssertTrue(latestMessage.waitToExist(timeout: Self.webTranscriptTimeout))
        assertClearsBottomChrome(latestMessage, app: app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }

    /// The provider's turn accounting renders as a footer inside the reply row.
    /// The fixture stamps every assistant reply with a 2m 9s turn, the way
    /// Claude writes `turn_duration` after each interactive turn.
    func testTurnFooterRendersUnderTheProviderReply() {
        let app = launchChatFixture(eventCount: 9)
        XCTAssertTrue(
            app.staticTexts["Assistant fixture message 7: streaming-style response with enough body to exercise row layout."]
                .waitToExist(timeout: Self.webTranscriptTimeout)
        )
        let footer = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "Worked for 2m 9s")).firstMatch
        XCTAssertTrue(footer.waitToExist(timeout: Self.webTranscriptTimeout), "the reply row carries the provider's turn footer")
        XCTAssertTrue(footer.label.contains("Turn finished"), "the footer clearly identifies the completed parent turn")
        attachScreenshot(app, named: "turn-footer")
    }

    /// A running Claude session shows its live HUD: background work, permission mode and context.
    func testCaptureClaudeFidelitySessionScreenshot() {
        let app = launchChatFixture(name: "claude-fidelity", eventCount: 0)
        waitForRenderedTranscript(app)
        let summary = app.buttons["session-runtime-background-summary"]
        XCTAssertTrue(summary.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(app.descendants(matching: .any)["session-hud-permission-mode"].waitToExist(timeout: Self.patient(5)))
        XCTAssertTrue(app.descendants(matching: .any)["session-context-ring"].waitToExist(timeout: Self.patient(5)))
        attachScreenshot(app, named: "claude-fidelity-session")
    }

    /// The background-work sheet lists a subagent and the agent it started.
    func testCaptureClaudeFidelitySheetScreenshot() {
        let app = launchChatFixture(name: "claude-fidelity-sheet", eventCount: 0)
        let parentAgent = app.buttons["session-runtime-background-task-sub-1"]
        let childAgent = app.buttons["session-runtime-background-task-sub-2"]
        XCTAssertTrue(parentAgent.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(childAgent.waitToExist(timeout: Self.patient(5)))
        attachScreenshot(app, named: "claude-fidelity-sheet")
    }
}

/// Sending from the composer: optimistic rows, their durable echoes and the send controls.
@MainActor
final class SessionChatComposerUITests: SessionChatUITestCase {
    func testComposeActionsExplainWhyImagesAreUnavailable() {
        // A Claude Console session launched from the phone advertises no image
        // attachments. The "+" control must still open a menu that says so;
        // an empty menu reads as a button that does nothing.
        let app = launchChatFixture(name: "basic", eventCount: 3)

        let actions = app.buttons["session-chat-compose-actions"]
        XCTAssertTrue(actions.waitToExist(timeout: Self.patient(8)), "composer action menu did not load")
        actions.tap()

        let unavailable = app.buttons["session-chat-attach-unavailable"]
        XCTAssertTrue(unavailable.waitToExist(timeout: Self.patient(5)), "the + menu opened without an attach item")
        XCTAssertFalse(unavailable.isEnabled, "attach must be disabled when the session cannot accept images")
        XCTAssertFalse(app.buttons["session-chat-attach"].exists)
        attachScreenshot(app, named: "compose-actions-images-unavailable")
    }

    func testSendShowsOptimisticMessageImmediatelyAndClearsComposer() {
        let app = launchChatFixture(eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let sendButton = app.buttons["session-chat-send"]
        let message = "ui harness immediate reveal"

        focusComposer(composer, in: app)
        composer.typeText(message)
        sendButton.tap()

        XCTAssertTrue(app.staticTexts[message].waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(app.staticTexts["Longhouse"].waitToExist(timeout: Self.patient(5)))
        XCTAssertEqual(composer.value as? String, "Steer this turn")
    }

    func testConsoleReplyReplacesOptimisticBubbleWithoutDuplicateWorkingRow() {
        let app = launchChatFixture(name: "console-reconcile", eventCount: 0)
        let composer = app.textFields["session-chat-composer"]
        let sendButton = app.buttons["session-chat-send"]
        let message = "console reconciliation probe"

        focusComposer(composer, in: app)
        composer.typeText(message)
        sendButton.tap()

        XCTAssertTrue(app.staticTexts["Console fixture durable reply."].waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertEqual(app.staticTexts.matching(identifier: message).count, 1)
        XCTAssertFalse(app.staticTexts["Working..."].exists)
        attachScreenshot(app, named: "console-send-reconciled")
    }

    /// A running turn offers "Send to this turn", and a steer shows one optimistic row
    /// until its durable echo replaces it with one clean message.
    func testHelmSteerReplacesOptimisticRowWithOneCleanDurableMessage() {
        // The fixture holds the durable echo until this file exists, so the optimistic state is
        // checked before the echo can replace it.
        let trigger = makeTrigger()
        let app = launchChatFixture(name: "helm-channel-reconcile", eventCount: 0, triggerPath: trigger.path)
        let composer = app.textFields["session-chat-composer"]
        let sendButton = app.buttons["session-chat-send"]
        let message = "Keep the current diagnostics and inspect the changed hose."

        XCTAssertTrue(composer.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(sendButton.waitToExist(timeout: Self.patient(5)))
        XCTAssertEqual(sendButton.label, "Send to this turn")
        focusComposer(composer, in: app)
        composer.typeText(message)
        attachScreenshot(app, named: "running-turn-composer")
        sendButton.tap()

        let optimisticMessage = app.staticTexts[message]
        XCTAssertTrue(optimisticMessage.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertFalse(app.staticTexts["Longhouse"].exists)
        XCTAssertEqual(app.staticTexts.matching(identifier: message).count, 1)
        XCTAssertTrue(app.staticTexts["Sending…"].exists)
        attachScreenshot(app, named: "helm-send-optimistic")
        fire(trigger)

        let longhouseOrigin = app.staticTexts["Longhouse"]
        XCTAssertTrue(longhouseOrigin.waitToExist(timeout: 30))
        XCTAssertEqual(app.staticTexts.matching(identifier: message).count, 1)
        XCTAssertFalse(app.staticTexts["Sending…"].exists)
        attachScreenshot(app, named: "helm-send-durable")
    }

    func testEndedCodexHelmResumeHandoffAndContinueToChildSession() {
        let app = launchChatFixture(name: "ended-codex-helm", eventCount: 4)

        let resume = app.buttons["session-resume-button"]
        XCTAssertTrue(resume.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertFalse(app.textFields["session-chat-composer"].exists)
        attachScreenshot(app, named: "ended-codex-helm-footer")

        resume.tap()
        let handoff = app.navigationBars["Resume command"]
        XCTAssertTrue(handoff.waitToExist(timeout: Self.patient(5)))
        attachScreenshot(app, named: "ended-codex-helm-resume-handoff")

        app.buttons["Done"].tap()
        XCTAssertFalse(app.textFields["session-chat-composer"].exists)
        let branchInput = app.textFields["session-branch-input"]
        XCTAssertTrue(branchInput.waitToExist(timeout: Self.webTranscriptTimeout))
        branchInput.tap()
        branchInput.typeText("continue with the migration")
        app.buttons["session-branch-button"].tap()

        XCTAssertTrue(app.navigationBars["Codex Helm branch"].waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(app.textFields["session-chat-composer"].waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(transcriptElement(app).waitToExist(timeout: Self.patient(8)))
        attachScreenshot(app, named: "ended-codex-helm-child")
    }
}

/// The transcript stays pinned to its latest row through updates, streaming and the keyboard.
@MainActor
final class SessionChatPinningUITests: SessionChatUITestCase {
    func testKeyboardFocusKeepsLatestTranscriptMessageVisible() {
        let app = launchChatFixture(eventCount: 40)
        let composer = app.textFields["session-chat-composer"]
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]

        XCTAssertTrue(currentLastMessage.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(waitUntilHittable(currentLastMessage, timeout: Self.patient(5)))
        focusComposer(composer, in: app)
        composer.typeText("typing keeps transcript pinned")

        XCTAssertTrue(waitUntilHittable(currentLastMessage, timeout: Self.patient(5)))
        assertAnchoredAboveBottomChrome(currentLastMessage, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
        attachScreenshot(app, named: "ledger-keyboard-draft")
    }

    func testAssistantUpdateKeepsPinnedTranscriptAtBottom() {
        let trigger = makeTrigger()
        let app = launchChatFixture(name: "assistant-update", eventCount: 40, triggerPath: trigger.path)
        let currentLastMessage = app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]
        let liveUpdate = app.staticTexts["Assistant fixture live update at bottom."]

        XCTAssertTrue(currentLastMessage.waitToExist(timeout: Self.webTranscriptTimeout))
        fire(trigger)
        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: Self.patient(5)))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }

    func testLongAssistantUpdateKeepsWrappedTailAboveBottomChrome() {
        let trigger = makeTrigger()
        let app = launchChatFixture(name: "assistant-update-long", eventCount: 40, triggerPath: trigger.path)
        let liveUpdate = app.staticTexts["Assistant fixture live update with wrapped tail above the floating composer card."]

        XCTAssertTrue(
            app.staticTexts["Assistant fixture message 39: streaming-style response with enough body to exercise row layout."]
                .waitToExist(timeout: Self.webTranscriptTimeout)
        )
        fire(trigger)
        XCTAssertTrue(waitUntilHittable(liveUpdate, timeout: 20))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }

    func testAssistantUpdateWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let trigger = makeTrigger()
        let app = launchChatFixture(name: "assistant-update-keyboard", eventCount: 40, triggerPath: trigger.path)
        let composer = app.textFields["session-chat-composer"]
        let liveUpdate = app.staticTexts["Assistant fixture keyboard update at bottom."]

        focusComposer(composer, in: app)
        // The update arrives only now, with the keyboard up.
        fire(trigger)

        XCTAssertTrue(liveUpdate.waitToExist(timeout: Self.webTranscriptTimeout))
        assertAnchoredAboveBottomChrome(liveUpdate, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }

    func testAssistantStreamingWithKeyboardOpenKeepsPinnedTranscriptAtBottom() {
        let trigger = makeTrigger()
        let app = launchChatFixture(name: "assistant-stream-keyboard", eventCount: 40, triggerPath: trigger.path)
        let composer = app.textFields["session-chat-composer"]
        let finalChunk = app.staticTexts["Assistant fixture streaming update at bottom."]

        focusComposer(composer, in: app)
        // The stream starts only now, with the keyboard up.
        fire(trigger)

        XCTAssertTrue(finalChunk.waitToExist(timeout: Self.webTranscriptTimeout))
        assertAnchoredAboveBottomChrome(finalChunk, app: app)
        assertScreenIsVisiblyRendered(app)
        assertNotVisible(app.staticTexts["User fixture message 0: request text for chat scroll anchoring."], in: app)
    }
}

/// Background work: the summary line, its sheet and when it goes away. Which delegation
/// facts show a summary at all is decided by SessionDelegationSummary and tested in
/// SessionDelegationSummaryTests against these same fixtures.
@MainActor
final class SessionChatBackgroundTaskUITests: SessionChatUITestCase {
    func testBackgroundCompletionReceiptsStayBeforeLaterReplies() {
        let app = launchChatFixture(
            name: "background-completion-receipts",
            eventCount: 0,
            appearance: .dark
        )
        let firstReply = app.staticTexts["Assistant reply before background work."]
        let wakeNotice = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Run the checks"))
            .firstMatch
        let middleReply = app.staticTexts["Assistant reply between completion notices."]
        let longhouseNotice = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Longhouse couldn't confirm"))
            .firstMatch
        let lastReply = app.staticTexts["Assistant reply after completion notices."]
        let initialOrder = [firstReply, wakeNotice, middleReply, longhouseNotice, lastReply]

        for element in initialOrder {
            XCTAssertTrue(
                element.waitToExist(timeout: Self.webTranscriptTimeout),
                "Expected transcript row to render: \(element)"
            )
        }
        assertTranscriptElementsVisibleInOrder(initialOrder, in: app)
        XCTAssertTrue(wakeNotice.label.contains("3 updates"), "three consecutive wake notices render as one counted row")
        let newestReply = app.staticTexts["Assistant fixture live update at bottom."]
        XCTAssertFalse(newestReply.exists, "The first frame must precede the fixture assistant update")
        attachScreenshot(app, named: "background-completion-receipts-dark")

        // Drive the update only after observing the initial rendered order.
        app.buttons["background-completion-append"].tap()
        XCTAssertTrue(newestReply.waitToExist(timeout: Self.webTranscriptTimeout))
        assertTranscriptElementsVisibleInOrder(initialOrder + [newestReply], in: app)
        XCTAssertGreaterThan(
            newestReply.frame.minY,
            longhouseNotice.frame.maxY,
            "The new assistant reply must stay below the earlier Longhouse completion notice"
        )
    }

    /// The summary sits under the parent's own headline ("Idle" stays), counts the work,
    /// and its sheet opens the exact child transcript.
    func testBackgroundTaskSheetOpensExactChildTranscript() {
        let app = launchChatFixture(name: "background-tasks", eventCount: 3)
        let summary = app.buttons["session-runtime-background-summary"]
        XCTAssertTrue(summary.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(app.staticTexts["Idle"].exists, "the summary must not replace the parent's headline")
        XCTAssertTrue(summary.label.contains("1 agent"))
        XCTAssertTrue(summary.label.contains("1 command"))
        summary.tap()
        let childTask = app.buttons["session-runtime-background-task-agent-1"]
        XCTAssertTrue(childTask.waitToExist(timeout: Self.patient(5)))
        attachScreenshot(app, named: "background-task-sheet")
        childTask.tap()

        let childID = "019fc50b-1111-4111-8111-111111111111"
        XCTAssertTrue(app.staticTexts["Child transcript"].waitToExist(timeout: Self.patient(5)))
        XCTAssertTrue(app.staticTexts[childID].exists)
        attachScreenshot(app, named: "background-task-child-route")
    }

    func testBackgroundTaskExpiredPositiveEvidenceRemainsUnknown() {
        let app = launchChatFixture(name: "background-tasks-expired-positive", eventCount: 0)
        let summary = app.buttons["session-runtime-background-summary"]
        XCTAssertTrue(summary.waitToExist(timeout: Self.webTranscriptTimeout))
        XCTAssertTrue(summary.label.contains("unknown"))
        summary.tap()
        XCTAssertTrue(app.staticTexts["Background work status unknown"].waitToExist(timeout: Self.patient(5)))
        attachScreenshot(app, named: "background-task-unknown")
    }

    /// At the largest accessibility text size the sheet's rows grow with their text and
    /// still fit the screen. The default-size row is measured first, so the test fails if
    /// the larger size never took effect.
    func testBackgroundTaskSheetRemainsReadableAtAccessibilitySize() {
        let defaultHeight = backgroundAgentRowFrame(contentSize: nil).height
        let frame = backgroundAgentRowFrame(contentSize: "UICTContentSizeCategoryAccessibilityXXXL")
        XCTAssertGreaterThan(frame.height, defaultHeight * 1.5, "the row did not grow with the text size (default \(defaultHeight))")
        XCTAssertGreaterThanOrEqual(frame.minX, 0)
    }

    private func backgroundAgentRowFrame(contentSize: String?) -> CGRect {
        let app = launchChatFixture(name: "background-tasks", eventCount: 0, dynamicTypeCategory: contentSize)
        let summary = app.buttons["session-runtime-background-summary"]
        XCTAssertTrue(summary.waitToExist(timeout: Self.webTranscriptTimeout))
        summary.tap()
        let agent = app.buttons["session-runtime-background-task-agent-1"]
        XCTAssertTrue(agent.waitToExist(timeout: Self.patient(5)))
        XCTAssertLessThanOrEqual(agent.frame.maxX, app.windows.firstMatch.frame.width)
        attachScreenshot(app, named: "background-task-sheet-\(contentSize ?? "default")")
        return agent.frame
    }

    /// A snapshot that reports no background work removes the summary line, and it stays gone.
    func testBackgroundTaskEmptySnapshotRemovesSummaryLine() {
        let app = launchChatFixture(name: "background-tasks-transition", eventCount: 0)
        let summary = app.buttons["session-runtime-background-summary"]
        XCTAssertTrue(summary.waitToExist(timeout: Self.webTranscriptTimeout))
        let clear = app.buttons["background-tasks-clear"]
        XCTAssertTrue(clear.waitToExist(timeout: Self.patient(5)))
        clear.tap()
        XCTAssertTrue(poll(timeout: Self.patient(5)) { !summary.exists }, "the summary line stayed after an empty snapshot")
        XCTAssertFalse(poll(timeout: 1) { summary.exists }, "the summary line came back after an empty snapshot")
    }
}

private extension CGRect {
    /// `frame` on a WebKit accessibility element can be infinite or NaN while
    /// layout is in flux; such a rect says nothing about where the element is.
    var isFiniteAndNonNull: Bool {
        !isNull && !isInfinite && origin.x.isFinite && origin.y.isFinite && size.width.isFinite && size.height.isFinite
    }
}
