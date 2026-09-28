import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

@MainActor
enum WebTranscriptWebViewPool {
    struct PooledWebView {
        let webView: TranscriptWebView
        let reused: Bool
        let isLoaded: Bool
    }

    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "WebTranscript")
    private static var warmedWebView: TranscriptWebView?
    private static var spareDelegate: WebTranscriptSpareDelegate?

    static func prewarm() {
        guard warmedWebView == nil else { return }
        let startedAt = Date()
        logger.info("webkit prewarm requested")
        let webView = configuredWebView()
        let delegate = WebTranscriptSpareDelegate(
            allowsDocumentLoad: true,
            onLoaded: {
                // `documentLoaded` is published synchronously by the delegate;
                // this callback is logging only and must not gate adoption.
                logger.info("webkit prewarm loaded")
            },
            onFailed: { [weak webView] in
                Task { @MainActor in
                    guard let webView, warmedWebView === webView else { return }
                    webView.prepareForTranscriptReuse()
                    webView.navigationDelegate = nil
                    warmedWebView = nil
                    spareDelegate = nil
                    logger.info("webkit prewarm evicted after navigation failure")
                }
            }
        )
        spareDelegate = delegate
        webView.navigationDelegate = delegate
        webView.loadHTMLString(WebTranscriptView.documentHTML, baseURL: nil)
        warmedWebView = webView
        logger.info("webkit prewarm started sync_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
    }
    static func discardWarmSpare() {
        guard let webView = warmedWebView else { return }
        webView.prepareForTranscriptReuse()
        webView.navigationDelegate = nil
        warmedWebView = nil
        spareDelegate = nil
        logger.info("webkit prewarm discarded for memory pressure")
    }

    static func takeOrCreate() -> PooledWebView {
        if let webView = warmedWebView {
            let delegate = spareDelegate
            warmedWebView = nil
            spareDelegate = nil
            // `didFinish` can have fired while its callback was queued for the
            // main actor. Read the delegate's synchronous publication, otherwise
            // adoption waits for a callback that already happened.
            let loaded = delegate?.documentLoaded == true
            logger.info(
                "webkit prewarm reused id=\(webView.transcriptInstanceID, privacy: .public) loaded=\(loaded, privacy: .public)"
            )
            return PooledWebView(webView: webView, reused: true, isLoaded: loaded)
        }
        logger.info("webkit prewarm miss")
        return PooledWebView(webView: configuredWebView(), reused: false, isLoaded: false)
    }

    static func logAdoption(_ webView: TranscriptWebView, reused: Bool, loaded: Bool) {
        logger.info(
            "webkit adopted id=\(webView.transcriptInstanceID, privacy: .public) reused=\(reused, privacy: .public) loaded=\(loaded, privacy: .public)"
        )
    }

    static func recycle(_ webView: TranscriptWebView, documentIsLoaded: Bool) {
        // A just-popped transcript is a better warm spare than a new WebView
        // still starting its content process. Keep one globally bounded spare.
        webView.prepareForTranscriptReuse()
        // A loaded spare refuses navigation. An in-flight spare allows only the
        // document navigation already started by Longhouse and reports whether
        // it completed before the next session adopts it.
        let delegate = WebTranscriptSpareDelegate(
            allowsDocumentLoad: !documentIsLoaded,
            documentLoaded: documentIsLoaded,
            onLoaded: documentIsLoaded ? nil : { [weak webView] in
                Task { @MainActor in
                    guard let webView, warmedWebView === webView else { return }
                    logger.info("webkit recycled load completed id=\(webView.transcriptInstanceID, privacy: .public)")
                }
            },
            onFailed: { [weak webView] in
                Task { @MainActor in
                    guard let webView, warmedWebView === webView else { return }
                    webView.prepareForTranscriptReuse()
                    webView.navigationDelegate = nil
                    warmedWebView = nil
                    spareDelegate = nil
                    logger.error("webkit recycled spare evicted after load failure id=\(webView.transcriptInstanceID, privacy: .public)")
                }
            }
        )
        spareDelegate = delegate
        webView.navigationDelegate = delegate
        warmedWebView = webView
        logger.info(
            "webkit recycled id=\(webView.transcriptInstanceID, privacy: .public) loaded=\(documentIsLoaded, privacy: .public)"
        )
    }

    private static func configuredWebView() -> TranscriptWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        let webView = TranscriptWebView(frame: .zero, configuration: configuration)
        // A long press on a URL in transcript text otherwise fetches and
        // previews it — a load the navigation policy above never sees.
        webView.allowsLinkPreview = false
        return webView
    }
}

/// Owns navigation for a WebView that no session is attached to: the prewarmed
/// spare and the recycled spare. It allows the one document load the pool itself
/// starts and cancels everything else, so a spare can never be navigated away
/// from the transcript document while it waits to be adopted.
private final class WebTranscriptSpareDelegate: NSObject, WKNavigationDelegate {
    private let onLoaded: (() -> Void)?
    private let onFailed: (() -> Void)?
    private var allowsDocumentLoad: Bool
    private(set) var documentLoaded: Bool

    init(
        allowsDocumentLoad: Bool,
        documentLoaded: Bool = false,
        onLoaded: (() -> Void)? = nil,
        onFailed: (() -> Void)? = nil
    ) {
        self.allowsDocumentLoad = allowsDocumentLoad
        self.documentLoaded = documentLoaded
        self.onLoaded = onLoaded
        self.onFailed = onFailed
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        guard allowsDocumentLoad, navigationAction.navigationType == .other else {
            decisionHandler(.cancel)
            return
        }
        allowsDocumentLoad = false
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        documentLoaded = true
        onLoaded?()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        onFailed?()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        onFailed?()
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        onFailed?()
    }
}
