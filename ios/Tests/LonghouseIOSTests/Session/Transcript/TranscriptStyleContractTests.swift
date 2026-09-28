import WebKit
import XCTest
@testable import Longhouse

/// The native half of the transcript document contract. What the document
/// renders is tested where its source lives (web/src/embeds/ios-transcript,
/// Vitest); here only what Swift owns: the bundled resource loads, the palette
/// is spliced in at its marker, and the bridge functions native calls exist
/// once WebKit has loaded it.
@MainActor
final class TranscriptStyleContractTests: XCTestCase, WKNavigationDelegate {
    private var navigationFinished = false
    private var navigationError: Error?

    func testBundledDocumentLoads() throws {
        let url = try XCTUnwrap(
            Bundle.main.url(forResource: "transcript", withExtension: "html", subdirectory: "Transcript"),
            "Transcript/transcript.html must be bundled with the app"
        )
        let bundled = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(bundled.isEmpty)
        XCTAssertEqual(
            WebTranscriptView.documentHTMLForTesting,
            bundled.replacingOccurrences(of: "/* __LH_ROOT_BLOCK__ */", with: TranscriptPalette.cssRootBlock),
            "The WebView loads the bundled resource with only the palette spliced in"
        )
    }

    func testPaletteMarkerIsReplaced() {
        let document = WebTranscriptView.documentHTMLForTesting
        XCTAssertFalse(document.contains("__LH_ROOT_BLOCK__"), "Palette marker must be replaced, not shipped raw")
        XCTAssertTrue(document.contains(TranscriptPalette.cssRootBlock), "The palette block must be in the document")
    }

    func testBridgeFunctionsExistOnceLoaded() async throws {
        let webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 320, height: 400))
        webView.navigationDelegate = self
        defer { webView.navigationDelegate = nil }
        XCTAssertNotNil(webView.loadHTMLString(WebTranscriptView.documentHTMLForTesting, baseURL: nil))
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(30))
        while !navigationFinished && navigationError == nil && clock.now < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        if let navigationError { throw navigationError }
        XCTAssertTrue(navigationFinished, "Timed out loading the transcript document")

        let types = try await webView.evaluateJavaScript(
            "[typeof window.renderTranscript, typeof window.setStickToBottom, typeof window.waitForTranscriptFrame].join(',')"
        )
        XCTAssertEqual(types as? String, "function,function,function")

        // The palette reached the page as live CSS, not just text.
        let attention = try await webView.evaluateJavaScript(
            "getComputedStyle(document.documentElement).getPropertyValue('--attention').trim()"
        )
        XCTAssertFalse((attention as? String ?? "").isEmpty, "The palette's --attention variable must resolve")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        navigationFinished = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        navigationError = error
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        navigationError = error
    }
}
