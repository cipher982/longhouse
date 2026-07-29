import Foundation
import UIKit
import WebKit
import XCTest

@testable import Longhouse

/// Behavioural regression tests for transcript scroll pinning.
///
/// These drive the real transcript document in a real WKWebView because the
/// bug they exist for is invisible to a string assertion: the WebView's frame
/// is not constant (the floating control card, the keyboard, and the safe area
/// all resize it through SwiftUI), and UIScrollView does not re-clamp
/// contentOffset when its bounds change. A pinned transcript silently lost its
/// last rows on every viewport change until the DOM started re-pinning.
@MainActor
final class WebTranscriptScrollPinningTests: XCTestCase {
    private var window: UIWindow!
    private var webView: WKWebView!

    override func setUp() async throws {
        try await super.setUp()
        window = UIWindow(frame: CGRect(x: 0, y: 0, width: 320, height: 640))
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 320, height: 400))
        window.addSubview(webView)
        window.makeKeyAndVisible()
        try await loadTranscriptDocument()
    }

    override func tearDown() async throws {
        webView.removeFromSuperview()
        webView = nil
        window.isHidden = true
        window = nil
        try await super.tearDown()
    }

    /// The regression this file exists for. Pinned to the bottom, then the
    /// viewport shrinks — as it does when the control card grows or the
    /// keyboard opens. Every row must stay reachable.
    func testPinnedTranscriptStaysPinnedWhenViewportShrinks() async throws {
        try await render(rowCount: 40, stick: true)
        try await assertPinnedToBottom("initial render")

        try await resizeViewport(height: 240)
        try await assertPinnedToBottom("after viewport shrank 400 -> 240")
    }

    /// The same failure in the other direction: the viewport grows, the frozen
    /// offset is now past the end of the document, and the transcript shows
    /// blank space below its last row.
    func testPinnedTranscriptStaysPinnedWhenViewportGrows() async throws {
        try await resizeViewport(height: 240)
        try await render(rowCount: 40, stick: true)
        try await assertPinnedToBottom("initial render at 240")

        try await resizeViewport(height: 400)
        try await assertPinnedToBottom("after viewport grew 240 -> 400")
    }

    /// Re-pinning must not fight a user who deliberately scrolled up. Native
    /// owns that intent; a viewport change must not silently re-stick.
    func testUnpinnedTranscriptIsNotRepinnedByAViewportChange() async throws {
        try await render(rowCount: 40, stick: true)
        try await setStickToBottom(false)
        _ = try await evaluate("window.scrollTo(0, 0); 1")
        try await settle()

        try await resizeViewport(height: 240)
        try await settle()

        let scrollY = try await number("window.scrollY")
        XCTAssertEqual(scrollY, 0, accuracy: 1, "A viewport change must not re-pin a deliberately scrolled-up transcript")
    }

    /// Cheap guard against a JavaScript error in the transcript document —
    /// a syntax or TypeError in that 700-line string literal fails silently in
    /// production, because Swift cannot validate it at build time.
    func testRenderTranscriptCompletesWithoutJavaScriptError() async throws {
        let metrics = try await render(rowCount: 8, stick: true)
        XCTAssertNotNil(metrics["dom_ms"], "renderTranscript must return its timing metrics")
        let rows = try await number("document.querySelectorAll('#root > *').length")
        XCTAssertEqual(rows, 8, accuracy: 0, "Every payload row must reach the DOM")
    }

    // MARK: - Harness

    private func loadTranscriptDocument() async throws {
        webView.loadHTMLString(WebTranscriptView.documentHTMLForTesting, baseURL: nil)
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            let ready = try? await webView.evaluateJavaScript("typeof window.renderTranscript === 'function'")
            if (ready as? Bool) == true { return }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTFail("Transcript document never finished loading")
    }

    @discardableResult
    private func render(rowCount: Int, stick: Bool) async throws -> [String: Any] {
        let items: [TimelineItem] = (0..<rowCount).map { index in
            .assistant(makeAssistantEvent(
                id: index + 1,
                // Long enough that the document overflows any test viewport.
                content: "Assistant row \(index + 1) with enough body text to wrap across several lines and give the transcript real height to scroll through."
            ))
        }
        let payload = WebTranscriptView.preparedPayload(
            timelineItems: items,
            submittedInputs: [],
            errorMessage: nil
        )
        let value = try await evaluate(
            "window.renderTranscript('\(payload.base64)', \(stick ? "true" : "false"), 1, 'snapshot');"
        )
        try await settle()
        return value as? [String: Any] ?? [:]
    }

    private func setStickToBottom(_ stick: Bool) async throws {
        _ = try await evaluate("window.setStickToBottom(\(stick ? "true" : "false")); 1")
    }

    private func resizeViewport(height: CGFloat) async throws {
        webView.frame = CGRect(x: 0, y: 0, width: webView.frame.width, height: height)
        webView.layoutIfNeeded()
        window.layoutIfNeeded()
        try await settle()
    }

    private func assertPinnedToBottom(_ context: String, file: StaticString = #filePath, line: UInt = #line) async throws {
        let scrollY = try await number("window.scrollY")
        let maxScroll = try await number("document.documentElement.scrollHeight - window.innerHeight")
        XCTAssertGreaterThan(maxScroll, 0, "\(context): document must overflow the viewport for this test to mean anything", file: file, line: line)
        XCTAssertEqual(
            scrollY,
            maxScroll,
            accuracy: 2,
            "\(context): transcript must remain pinned to its last row",
            file: file,
            line: line
        )
    }

    /// Two frames: `scrollToBottom` re-scrolls inside a requestAnimationFrame,
    /// and the resize re-pin schedules one of its own.
    private func settle() async throws {
        for _ in 0..<2 {
            _ = try? await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(1))))")
            try await Task.sleep(nanoseconds: 60_000_000)
        }
    }

    private func evaluate(_ javaScript: String) async throws -> Any? {
        try await webView.evaluateJavaScript(javaScript)
    }

    private func number(_ expression: String) async throws -> Double {
        let value = try await evaluate("Number(\(expression))")
        return (value as? NSNumber)?.doubleValue ?? .nan
    }

    private func makeAssistantEvent(id: Int, content: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "assistant",
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }
}
