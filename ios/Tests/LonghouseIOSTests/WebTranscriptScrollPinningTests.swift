import Foundation
import UIKit
import WebKit
import XCTest

@testable import Longhouse

/// Behavioural regression tests for transcript scroll pinning.
///
/// These drive the real transcript document in a real WKWebView and assert on
/// its native UIScrollView. The regression was a frozen native contentOffset,
/// so DOM-only scrollY assertions are not an adequate guard.
@MainActor
final class WebTranscriptScrollPinningTests: XCTestCase {
    private var window: UIWindow!
    private var webView: TranscriptWebView!
    private var nativeViewportCoordinator: WebTranscriptView.Coordinator?

    override func setUp() async throws {
        try await super.setUp()
        window = UIWindow(frame: CGRect(x: 0, y: 0, width: 320, height: 640))
        webView = TranscriptWebView(frame: CGRect(x: 0, y: 0, width: 320, height: 400))
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        window.addSubview(webView)
        window.makeKeyAndVisible()
        try await loadTranscriptDocument()
    }

    override func tearDown() async throws {
        nativeViewportCoordinator = nil
        webView.removeFromSuperview()
        webView = nil
        window.isHidden = true
        window = nil
        try await super.tearDown()
    }

    /// Recreate the exact native failure state: the viewport has shrunk but
    /// UIScrollView still holds the old bottom offset. The DOM resize hook must
    /// move the native scroll view to its new maximum.
    func testViewportResizeRepinsFrozenNativeContentOffset() async throws {
        try await render(rowCount: 40, stick: true)
        try await assertNativePinnedToBottom("initial render")
        let oldBottom = webView.scrollView.contentOffset.y

        try await resizeViewport(height: 240)
        webView.scrollView.setContentOffset(CGPoint(x: 0, y: oldBottom), animated: false)
        XCTAssertGreaterThan(nativeMaxScrollOffset() - oldBottom, 100, "the forced offset must reproduce hidden tail rows")

        _ = try await evaluate("window.dispatchEvent(new Event('resize')); 1")
        try await assertNativePinnedToBottom("after the viewport resize event")
    }

    /// Content can grow without a render when media resolves. ResizeObserver
    /// owns that path and must reconcile the native offset as root grows.
    func testContentResizeRepinsNativeContentOffset() async throws {
        try await render(rowCount: 40, stick: true)
        try await assertNativePinnedToBottom("initial render")
        let oldBottom = webView.scrollView.contentOffset.y

        _ = try await evaluate(
            "document.getElementById('root').insertAdjacentHTML('beforeend', '<div id=delayed-growth style=\"height:240px\"></div>'); 1"
        )
        try await waitUntil("content size grows") {
            self.nativeMaxScrollOffset() - oldBottom > 150
        }
        try await assertNativePinnedToBottom("after root content grew")
    }

    /// Re-pinning must not fight a user who deliberately scrolled up. Native
    /// owns that intent; a viewport change must not silently re-stick.
    func testUnpinnedTranscriptIsNotRepinnedByAViewportChange() async throws {
        let coordinator = attachNativeViewportHook()
        try await render(rowCount: 40, stick: true)
        try await setStickToBottom(false)

        // Record the same intent natively that a real drag toward older messages
        // does, so the native re-pin is under test here too. The scroll itself
        // is native: routing it through JS raced the delegate callbacks, and a
        // drag that had not landed yet reads as "still at the bottom".
        coordinator.scrollViewWillBeginDragging(webView.scrollView)
        webView.scrollView.setContentOffset(.zero, animated: false)
        coordinator.scrollViewDidEndDragging(webView.scrollView, willDecelerate: false)
        try await settle()

        try await resizeViewport(height: 240)
        try await settle()

        XCTAssertEqual(
            webView.scrollView.contentOffset.y,
            0,
            accuracy: 1,
            "A viewport change must not re-pin a deliberately scrolled-up transcript"
        )
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

    /// The production trigger, not a synthetic one.
    ///
    /// `testViewportResizeRepinsFrozenNativeContentOffset` hand-dispatches a
    /// `resize` event, so it proves the DOM handler while assuming WebKit
    /// delivers that event on a native frame change. This asserts the native
    /// path instead: the DOM is explicitly told NOT to stick, so the only thing
    /// that can land the scroll view on the last row is `layoutSubviews`.
    ///
    /// On growth the same hook also sends a no-op scroll update, which is what
    /// makes WebKit paint the newly exposed strip. That half is not assertable
    /// here — WebKit renders out of process — so the offset is the guard.
    func testNativeFrameChangeAloneRepinsWithoutTheDOMResizeHandler() async throws {
        attachNativeViewportHook()

        try await render(rowCount: 40, stick: true)
        try await assertNativePinnedToBottom("initial render")
        try await setStickToBottom(false)

        // Shrink, as the control card growing or the keyboard appearing does.
        try await resizeNativeFrameOnly(height: 240)
        try await assertNativePinnedToBottom("after the frame shrank")

        // Grow back, as dismissing the keyboard does. This is the direction that
        // left an unpainted band the height of whatever went away.
        try await resizeNativeFrameOnly(height: 400)
        try await assertNativePinnedToBottom("after the frame grew back")
    }

    /// The paint half, which the offset assertions above cannot see.
    ///
    /// Growth with the offset already in range does no clamping, so nothing
    /// moves and nothing tells WebKit its exposed region grew. The fix sends a
    /// deliberate 1pt scroll and returns to the same offset. Assert the exact
    /// detour, not merely "something scrolled" — WebKit syncs the offset on its
    /// own during a resize and would satisfy a looser check.
    func testGrowthSendsAScrollUpdateWhenNothingNeedsRepinning() async throws {
        attachNativeViewportHook()
        try await render(rowCount: 40, stick: true)

        // Deliberately scrolled up, and far enough from both ends that neither
        // viewport height clamps it.
        let resting: CGFloat = 1000
        nativeViewportCoordinator?.scrollViewWillBeginDragging(webView.scrollView)
        webView.scrollView.setContentOffset(CGPoint(x: 0, y: resting), animated: false)
        nativeViewportCoordinator?.scrollViewDidEndDragging(webView.scrollView, willDecelerate: false)
        try await setStickToBottom(false)
        try await resizeNativeFrameOnly(height: 240)
        try await settle()
        XCTAssertEqual(
            webView.scrollView.contentOffset.y,
            resting,
            accuracy: 1,
            "precondition: the shrink must not have moved an in-range offset"
        )

        var observed: [CGFloat] = []
        let observation = webView.scrollView.observe(\.contentOffset, options: [.new]) { scrollView, _ in
            observed.append(scrollView.contentOffset.y)
        }
        defer { observation.invalidate() }

        try await resizeNativeFrameOnly(height: 400)
        try await settle()

        XCTAssertTrue(
            observed.contains { abs($0 - (resting - 1)) < 0.01 },
            "growth must send a scroll update so WebKit repaints the newly exposed strip; saw \(observed)"
        )
        XCTAssertEqual(
            webView.scrollView.contentOffset.y,
            resting,
            accuracy: 1,
            "the scroll update must land back where it started, so it is invisible"
        )
    }

    /// Wire the production `layoutSubviews` hook to a real coordinator.
    @discardableResult
    private func attachNativeViewportHook() -> WebTranscriptView.Coordinator {
        let coordinator = WebTranscriptView.Coordinator()
        coordinator.webView = webView
        webView.onViewportHeightChange = { [weak webView] previous, height in
            guard let webView else { return }
            coordinator.viewportHeightDidChange(from: previous, to: height, on: webView)
        }
        nativeViewportCoordinator = coordinator
        return coordinator
    }

    /// Change only the native frame: no `dispatchEvent`, no JS at all.
    private func resizeNativeFrameOnly(height: CGFloat) async throws {
        webView.frame = CGRect(x: 0, y: 0, width: webView.frame.width, height: height)
        webView.layoutIfNeeded()
        window.layoutIfNeeded()
        try await settle()
    }

    // MARK: - Harness

    private func loadTranscriptDocument() async throws {
        webView.loadHTMLString(WebTranscriptView.documentHTMLForTesting, baseURL: nil)
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(10))
        while clock.now < deadline {
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

    /// Resize the native frame and wait for the web process to actually see it.
    /// The frame change crosses a process boundary, so `window.innerHeight` lags
    /// the assignment; returning early lets the next step render against the
    /// old viewport.
    private func resizeViewport(height: CGFloat) async throws {
        webView.frame = CGRect(x: 0, y: 0, width: webView.frame.width, height: height)
        webView.layoutIfNeeded()
        window.layoutIfNeeded()
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(3))
        while clock.now < deadline {
            if abs(try await number("window.innerHeight") - Double(height)) <= 1 { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try await settle()
    }

    private func assertNativePinnedToBottom(
        _ context: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        try await waitUntil(context) {
            let maxScroll = self.nativeMaxScrollOffset()
            return maxScroll > 0
                && abs(self.webView.scrollView.contentOffset.y - maxScroll) <= 2
        }
        let offset = webView.scrollView.contentOffset.y
        let maxScroll = nativeMaxScrollOffset()
        XCTAssertGreaterThan(maxScroll, 0, "\(context): document must overflow the viewport for this test to mean anything", file: file, line: line)
        XCTAssertEqual(
            offset,
            maxScroll,
            accuracy: 2,
            "\(context): native scroll view must remain pinned to its last row",
            file: file,
            line: line
        )
    }

    private func nativeMaxScrollOffset() -> CGFloat {
        max(0, webView.scrollView.contentSize.height - webView.scrollView.bounds.height)
    }

    private func waitUntil(
        _ context: String,
        timeout: Duration = .seconds(3),
        condition: () -> Bool
    ) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while clock.now < deadline {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("Timed out waiting for \(context)")
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
