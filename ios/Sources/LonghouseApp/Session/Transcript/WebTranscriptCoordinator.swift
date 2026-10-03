import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

extension WebTranscriptView {
    final class Coordinator: NSObject, WKNavigationDelegate, UIScrollViewDelegate, WKScriptMessageHandler {
        var onOpenSubagent: ((String) -> Void)?
        var onEditSubmittedInput: ((String) -> Void)?
        var onDiscardSubmittedInput: ((String) -> Void)?
        var onRetrySubmittedInput: ((String) -> Void)?
        var onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)?
        var onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)?
        var onRefresh: (() async -> Void)?

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == WebTranscriptView.bridgeName,
                  let payload = message.body as? [String: Any],
                  let type = payload["type"] as? String
            else { return }
            if type == "openSubagent",
               let sessionId = payload["sessionId"] as? String,
               UUID(uuidString: sessionId) != nil,
               let handler = onOpenSubagent {
                Task { @MainActor in handler(sessionId) }
            } else if type == "editSubmitted",
                      let clientRequestId = payload["clientRequestId"] as? String,
                      !clientRequestId.isEmpty,
                      let handler = onEditSubmittedInput {
                Task { @MainActor in handler(clientRequestId) }
            } else if type == "discardSubmitted",
                      let clientRequestId = payload["clientRequestId"] as? String,
                      !clientRequestId.isEmpty,
                      let handler = onDiscardSubmittedInput {
                Task { @MainActor in handler(clientRequestId) }
            } else if type == "retrySubmitted",
                      let clientRequestId = payload["clientRequestId"] as? String,
                      !clientRequestId.isEmpty,
                      let handler = onRetrySubmittedInput {
                Task { @MainActor in handler(clientRequestId) }
            }
        }

        weak var webView: WKWebView?
        private let logger = Logger(subsystem: "ai.longhouse.ios", category: "WebTranscript")
        var isLoaded = false
        /// User intent only: false once the user deliberately scrolls toward
        /// older messages. Every change is pushed to the DOM, which owns the
        /// geometry and re-pins on viewport/content resize, so it never runs on
        /// a stale opinion.
        private var shouldStickToBottom = true {
            didSet {
                guard shouldStickToBottom != oldValue, isLoaded else { return }
                webView?.evaluateJavaScript(
                    "window.setStickToBottom && window.setStickToBottom(\(shouldStickToBottom ? "true" : "false"));"
                )
            }
        }
#if DEBUG
        /// Lets a test assert its own precondition. The intent is inferred from
        /// drag geometry, so a fixture can silently fail to become unpinned and
        /// make a re-pin assertion look like a product bug.
        var isStickingToBottomForTesting: Bool { shouldStickToBottom }
#endif
        private var userScrollInProgress = false
        /// Invalidates deferred viewport reconciliation across height changes and
        /// across WebView reuse.
        private var viewportReconcileGeneration = 0
        private var contentSizeObservation: NSKeyValueObservation?
        private var dragStartOffsetY: CGFloat?
        private static let historyFillSlack: CGFloat = 240
        weak var refreshControl: UIRefreshControl?
        private var refreshTask: Task<Void, Never>?

#if DEBUG
        func setNeedsMoreHistoryHandlerForTesting(_ handler: (() -> Void)?) {
            onNeedsMoreHistory = handler
        }
#endif

        /// Identity and input waiting behind the one active encoder. Keeping
        /// only the newest request bounds CPU/memory during a realtime burst.
        private struct PreparationRequest {
            let identity: ContentIdentity
            let input: WebTranscriptPayloadInput
            let forceRender: Bool
        }

        /// Identity of the transcript the most recent payload was prepared from.
        private var preparedIdentity: ContentIdentity?
        private var preparationTask: Task<Void, Never>?
        private var pendingPreparation: PreparationRequest?
        private var lastRetryRevision: UInt64 = 0
        private var pendingPayload: WebTranscriptPreparedPayload?
        private var inFlightPayload: WebTranscriptPreparedPayload?
        private var lastRenderedPayload: WebTranscriptPreparedPayload?
        private var lastPayload: String?
        private var lastDuplicatePayload: String?
        private var renderSequence = 0
        private var jsFailureCount = 0
        private var suppressNearTopUntil = Date.distantPast
        private var diagnosticsEnabled = WebTranscriptDiagnosticsFeature.isEnabled
        private var onNearTop: (() -> Void)?
        private var onNeedsMoreHistory: (() -> Void)?
        private var onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
        private var onLifecycle: ((String) -> Void)?
        private var lastNearTopRequestAt = Date.distantPast
        private var documentServerURL: String?
        private var mediaAuthSignature: String?
        private var mediaAuthPrimedServerURL: String?
        /// The latest navigation started by this coordinator. Delegate
        /// callbacks from an older load must not tear down the new document.
        private var activeNavigation: WKNavigation?
        /// Invalidates JavaScript completions from a document that was
        /// replaced or recycled. WebKit can deliver an old completion after a
        /// new HTML document has already started on the same view.
        private var documentGeneration: UInt64 = 0
        /// Armed by `loadDocument(serverURL:on:)` and consumed by the policy gate
        /// below: one navigation per load, and only the one this app started.
        private var awaitingDocumentNavigation = false

        /// The transcript pane has no URL bar, no back button, and no origin
        /// indicator, and it receives every payload through
        /// `window.renderTranscript` in the page's own content world. A link in
        /// attacker-controlled transcript text that navigated it in place would
        /// therefore be handed the transcript itself, and the hijacked document
        /// would survive into the next session through the WebView pool. Only our
        /// own document load happens here; a tapped link goes to the system
        /// browser, which has all the chrome this pane does not.
        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
        ) {
            guard self.webView === webView else {
                decisionHandler(.cancel)
                return
            }
            if awaitingDocumentNavigation, navigationAction.navigationType == .other {
                awaitingDocumentNavigation = false
                decisionHandler(.allow)
                return
            }
            decisionHandler(.cancel)
            // Web links only, and only ones that tried to replace this pane.
            // Custom schemes stay inert rather than becoming a way for transcript
            // text to reach another app, and the `target="_blank"` media links
            // stay inert because Safari cannot authenticate them.
            guard navigationAction.targetFrame?.isMainFrame == true,
                  let url = navigationAction.request.url,
                  let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https" else {
                return
            }
            Task { @MainActor in
                UIApplication.shared.open(url)
            }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard self.webView === webView, acceptsNavigation(navigation) else { return }
            isLoaded = true
            awaitingDocumentNavigation = false
            Task { @MainActor in
                self.onLifecycle?("webview_html_loaded")
            }
            // Intent changes recorded before the document existed never reached
            // the DOM; a recycled WebView also carries the previous session's
            // value. Resend unconditionally now that JS is there.
            webView.evaluateJavaScript(
                "window.setStickToBottom && window.setStickToBottom(\(shouldStickToBottom ? "true" : "false"));"
            )
            flushPendingPayload(
                to: webView,
                diagnosticsEnabled: diagnosticsEnabled,
                onDiagnostics: onDiagnostics
            )
        }
        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation!,
            withError error: Error
        ) {
            documentLoadFailed(on: webView, navigation: navigation, error: error)
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            documentLoadFailed(on: webView, navigation: navigation, error: error)
        }

        private func acceptsNavigation(_ navigation: WKNavigation?) -> Bool {
            guard let expected = activeNavigation else { return true }
            guard let navigation, navigation === expected else { return false }
            activeNavigation = nil
            return true
        }
        private func documentLoadFailed(
            on webView: WKWebView,
            navigation: WKNavigation?,
            error: Error
        ) {
            guard self.webView === webView, acceptsNavigation(navigation) else { return }
            let nsError = error as NSError
            guard !(nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled) else {
                return
            }
            isLoaded = false
            awaitingDocumentNavigation = false
            pendingPayload = pendingPayload ?? inFlightPayload ?? lastRenderedPayload
            inFlightPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            preparedIdentity = nil
            logger.error("webkit document load failed: \(error.localizedDescription, privacy: .public)")
            Task { @MainActor in
                self.onLifecycle?("webview_document_failed")
            }
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            guard self.webView === webView else { return }
            Task { @MainActor in
                self.onLifecycle?("webview_content_process_terminated")
            }
            isLoaded = false
            jsFailureCount += 1
            pendingPayload = pendingPayload ?? inFlightPayload ?? lastRenderedPayload
            inFlightPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            // The crash may have taken the only copy of the payload with it, and
            // the transcript can sit unchanged for minutes. Re-prepare on the
            // next update rather than trust a memo of a render that is gone; if
            // the payload did survive above, `send` discards the duplicate.
            preparedIdentity = nil
            loadDocument(serverURL: documentServerURL, on: webView)
        }

        func loadDocument(serverURL: String?, on webView: WKWebView) {
            documentServerURL = serverURL
            isLoaded = false
            awaitingDocumentNavigation = true
            documentGeneration &+= 1
            activeNavigation = webView.loadHTMLString(
                WebTranscriptView.documentHTML,
                baseURL: WebTranscriptView.documentBaseURL(serverURL)
            )
        }

        func adoptDocument(serverURL: String, loaded: Bool) {
            documentServerURL = serverURL
            isLoaded = loaded
            activeNavigation = nil
            // The adopted spare's navigation belongs to the pool, so the
            // coordinator cannot identify it by WKNavigation. A new document
            // generation still fences every callback owned by this coordinator.
            documentGeneration &+= 1
            // An unfinished spare's document load is the pool's, and this
            // coordinator takes over as navigation delegate mid-flight —
            // possibly before WebKit has asked anyone for a policy. Arm the
            // gate so that decision still resolves to the document we await.
            awaitingDocumentNavigation = !loaded
            // A recycled document keeps the previous session's stickiness;
            // `didFinish` will not fire again to reset it.
            guard loaded else { return }
            webView?.evaluateJavaScript(
                "window.setStickToBottom && window.setStickToBottom(\(shouldStickToBottom ? "true" : "false"));"
            )
        }

        func ensureDocumentServerURL(_ serverURL: String, on webView: WKWebView) {
            guard documentServerURL != serverURL else { return }
            pendingPayload = inFlightPayload ?? lastRenderedPayload ?? pendingPayload
            inFlightPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            loadDocument(serverURL: serverURL, on: webView)
        }

        func configureMediaAuth(serverURL: String, on webView: WKWebView) {
            var cookies = URL(string: serverURL)
                .flatMap { HTTPCookieStorage.shared.cookies(for: $0) }?
                .filter { SharedAuthStore.managedCookieNames.contains($0.name) } ?? []
            if cookies.isEmpty, mediaAuthPrimedServerURL != serverURL {
                // The app normally primes the shared jar at auth time, but a
                // pooled WebView can be the first surface after relaunch.
                // Pay the Keychain read once per coordinator/server, not on
                // every SwiftUI update.
                mediaAuthPrimedServerURL = serverURL
                let managedCookies = SharedAuthStore.managedCookies(for: serverURL)
                for cookie in managedCookies {
                    HTTPCookieStorage.shared.setCookie(cookie)
                }
                cookies = managedCookies
            }
            let signature = cookies
                .sorted { $0.name < $1.name }
                .map { "\($0.name)=\($0.value)@\($0.domain)" }
                .joined(separator: "|")
            guard signature != mediaAuthSignature else { return }
            mediaAuthSignature = signature
            let cookieStore = webView.configuration.websiteDataStore.httpCookieStore
            for cookie in cookies {
                cookieStore.setCookie(cookie)
            }
        }

        func observeContentSize(on webView: WKWebView) {
            contentSizeObservation?.invalidate()
            contentSizeObservation = webView.scrollView.observe(
                \.contentSize,
                options: [.new]
            ) { [weak self, weak webView] _, _ in
                guard webView != nil else { return }
                DispatchQueue.main.async { [weak self, weak webView] in
                    guard let self, let webView else { return }
                    self.contentSizeDidChange(on: webView)
                }
            }
        }

        /// Reconcile what a viewport or native content-size change breaks.
        /// Deferred off the layout/KVO callback so the scroll writes do not
        /// re-enter UIKit or race WebKit's own bounds update.
        ///
        /// Every deferred write is generation-guarded. Without that, a height
        /// change followed within a runloop turn by a dismissal — keyboard down
        /// then back, which is one gesture — lands the old session's offset on a
        /// pooled WebView that is already showing the next session. The same
        /// guard collapses a burst of changes to the last one.
        func viewportHeightDidChange(from previous: CGFloat, to height: CGFloat, on webView: WKWebView) {
            scheduleGeometryReconciliation(
                on: webView,
                viewportChange: (previous: previous, height: height)
            )
        }

        /// A retained DOM render changes `contentSize` after WebKit has
        /// acknowledged the JavaScript frame. UIKit does not clamp the native
        /// offset for that change, so observe the actual native geometry rather
        /// than assuming the JavaScript animation frame is the handoff point.
        func contentSizeDidChange(on webView: WKWebView) {
            scheduleGeometryReconciliation(on: webView, viewportChange: nil)
        }

        private func scheduleGeometryReconciliation(
            on webView: WKWebView,
            viewportChange: (previous: CGFloat, height: CGFloat)?
        ) {
            viewportReconcileGeneration &+= 1
            let generation = viewportReconcileGeneration
            let reconcile = { [weak self, weak webView] in
                guard let self, let webView, generation == self.viewportReconcileGeneration else { return }
                let scrollView = webView.scrollView
                // The valid range is bounded by the adjusted insets, not by
                // bounds alone. `contentInsetAdjustmentBehavior = .never` stops
                // UIKit adding safe-area insets; it does not prove that nothing
                // else set one, and clamping to the wrong maximum would strand
                // exactly the rows this exists to reach.
                let insets = scrollView.adjustedContentInset
                let minOffset = -insets.top
                let maxOffset = max(minOffset, scrollView.contentSize.height + insets.bottom - scrollView.bounds.height)
                // Keep the viewport transition evidence; a screenshot of a
                // blank band cannot show which native geometry was stale.
                if let viewportChange {
                    self.logger.info(
                        "webkit transcript viewport \(Int(viewportChange.previous), privacy: .public)->\(Int(viewportChange.height), privacy: .public) content=\(Int(scrollView.contentSize.height), privacy: .public) offset=\(Int(scrollView.contentOffset.y), privacy: .public) min=\(Int(minOffset), privacy: .public) max=\(Int(maxOffset), privacy: .public) inset=\(Int(insets.top), privacy: .public)/\(Int(insets.bottom), privacy: .public) stick=\(self.shouldStickToBottom, privacy: .public)"
                    )
                }
                let target = self.shouldStickToBottom && !self.userScrollInProgress
                    ? maxOffset
                    : min(max(scrollView.contentOffset.y, minOffset), maxOffset)
                // A compact tail can be slightly taller than the viewport
                // while still hiding every older page behind the initial
                // window, so leave enough spare height for a near-top gesture.
                if scrollView.contentSize.height > 0,
                   scrollView.contentSize.height <= scrollView.bounds.height + Self.historyFillSlack {
                    self.onNeedsMoreHistory?()
                }
                guard abs(scrollView.contentOffset.y - target) > 0.5 else { return }
                scrollView.setContentOffset(CGPoint(x: scrollView.contentOffset.x, y: target), animated: false)
            }
            DispatchQueue.main.async { [weak self, weak webView] in
                guard let self, let webView, generation == self.viewportReconcileGeneration else { return }
                reconcile()
                // WKWebView may apply its own bounds adjustment after our first
                // deferred write. Re-check once on the next main-loop turn; the
                // generation and live stickiness guards keep this from reviving
                // an old session or fighting a drag that began in between.
                DispatchQueue.main.async { [weak self, weak webView] in
                    guard let self, webView != nil, generation == self.viewportReconcileGeneration else { return }
                    reconcile()
                }
            }
        }
        @objc func refreshControlDidChange(_ sender: UIRefreshControl) {
            guard refreshTask == nil else { return }
            refreshTask = Task { @MainActor [weak self, weak sender] in
                guard let self else {
                    sender?.endRefreshing()
                    return
                }
                await self.onRefresh?()
                sender?.endRefreshing()
                self.refreshTask = nil
            }
        }

        func scrollViewDidScroll(_ scrollView: UIScrollView) {
            emitNearTopIfNeeded(scrollView)
        }

        func scrollViewWillBeginDragging(_ scrollView: UIScrollView) {
            userScrollInProgress = true
            dragStartOffsetY = scrollView.contentOffset.y
            shouldStickToBottom = false
        }

        func scrollViewDidEndDragging(_ scrollView: UIScrollView, willDecelerate decelerate: Bool) {
            guard !decelerate else { return }
            finishUserScroll(scrollView)
        }

        func scrollViewDidEndDecelerating(_ scrollView: UIScrollView) {
            finishUserScroll(scrollView)
        }

        private func finishUserScroll(_ scrollView: UIScrollView) {
            userScrollInProgress = false
            // 8pt absorbs tap jitter while preserving an intentional move into older messages.
            let movedTowardOlderMessages = dragStartOffsetY.map { scrollView.contentOffset.y < $0 - 8 } ?? false
            dragStartOffsetY = nil
            guard !movedTowardOlderMessages else {
                shouldStickToBottom = false
                return
            }
            updateStickiness(scrollView)
        }

        private func updateStickiness(_ scrollView: UIScrollView) {
            let distanceFromBottom = scrollView.contentSize.height - scrollView.contentOffset.y - scrollView.bounds.height
            shouldStickToBottom = distanceFromBottom < 96
        }

        private func emitNearTopIfNeeded(_ scrollView: UIScrollView) {
            guard inFlightPayload == nil else { return }
            guard userScrollInProgress || !shouldStickToBottom else { return }
            guard Date() >= suppressNearTopUntil else { return }
            guard scrollView.contentSize.height > scrollView.bounds.height + Self.historyFillSlack else { return }
            guard scrollView.contentOffset.y < 180 else { return }
            let now = Date()
            guard now.timeIntervalSince(lastNearTopRequestAt) > 0.75 else { return }
            lastNearTopRequestAt = now
            onNearTop?()
        }

        func prepareForReuse() {
            preparedIdentity = nil
            preparationTask?.cancel()
            preparationTask = nil
            pendingPreparation = nil
            lastRetryRevision = 0
            // Strands any deferred viewport write before the WebView is recycled.
            viewportReconcileGeneration &+= 1
            documentGeneration &+= 1
            activeNavigation = nil
            contentSizeObservation?.invalidate()
            webView = nil
            onOpenSubagent = nil
            onEditSubmittedInput = nil
            onDiscardSubmittedInput = nil
            onRetrySubmittedInput = nil
            onNearTop = nil
            onNeedsMoreHistory = nil
            onDiagnostics = nil
            onLifecycle = nil
            onFrameFailed = nil
            refreshTask?.cancel()
            refreshTask = nil
            refreshControl?.removeTarget(
                self,
                action: #selector(refreshControlDidChange(_:)),
                for: .valueChanged
            )
            refreshControl?.endRefreshing()
            refreshControl = nil
            onRefresh = nil
            onFrameRendered = nil
            pendingPayload = nil
            inFlightPayload = nil
            lastRenderedPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            userScrollInProgress = false
            dragStartOffsetY = nil
            mediaAuthSignature = nil
            mediaAuthPrimedServerURL = nil
            shouldStickToBottom = true
        }

        /// The callbacks close over the current SwiftUI state, so they are
        /// rebound on every update; the payload is only prepared when the
        /// transcript behind it actually changed.
        func send(
            contentIdentity: ContentIdentity,
            preparationInput: WebTranscriptView.WebTranscriptPayloadInput,
            to webView: WKWebView,
            diagnosticsEnabled: Bool,
            onNearTop: (() -> Void)?,
            onNeedsMoreHistory: (() -> Void)?,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?,
            onLifecycle: ((String) -> Void)?,
            onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)?,
            onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)?
        ) {
            self.webView = webView
            self.diagnosticsEnabled = diagnosticsEnabled
            self.onNearTop = onNearTop
            self.onNeedsMoreHistory = onNeedsMoreHistory
            self.onDiagnostics = onDiagnostics
            self.onLifecycle = onLifecycle
            self.onFrameFailed = onFrameFailed
            self.onFrameRendered = onFrameRendered
            let forceRender = contentIdentity.retryRevision != lastRetryRevision
            lastRetryRevision = contentIdentity.retryRevision
            guard forceRender || contentIdentity != preparedIdentity else { return }
            let request = PreparationRequest(
                identity: contentIdentity,
                input: preparationInput,
                forceRender: forceRender
            )
            preparedIdentity = contentIdentity
            if preparationTask != nil {
                let forceRender = request.forceRender || pendingPreparation?.forceRender == true
                pendingPreparation = PreparationRequest(
                    identity: request.identity,
                    input: request.input,
                    forceRender: forceRender
                )
                return
            }
            beginPreparation(request)
        }

        private func beginPreparation(_ request: PreparationRequest) {
            preparationTask = Task { @MainActor [weak self] in
                let payload = await Task.detached(priority: .userInitiated) {
                    WebTranscriptView.preparedPayload(input: request.input)
                }.value
                guard !Task.isCancelled,
                      let self
                else { return }
                self.preparationTask = nil
                let pending = self.pendingPreparation
                self.pendingPreparation = nil
                guard let webView = self.webView else {
                    // The representable can be dismantled while encoding is
                    // finishing. Leave the request eligible for the next
                    // mounted WebView rather than marking it prepared forever.
                    self.preparedIdentity = nil
                    self.pendingPreparation = pending
                    return
                }
                // A newer request may have replaced this one while it was
                // encoding. The completed payload is still useful: dispatch
                // it as the first usable frame, then drain the newest request
                // behind it. Dropping every completed older payload made a
                // sustained stream look blank until the provider went quiet.
                self.send(
                    payload,
                    to: webView,
                    diagnosticsEnabled: self.diagnosticsEnabled,
                    onNearTop: self.onNearTop,
                    onNeedsMoreHistory: self.onNeedsMoreHistory,
                    onDiagnostics: self.onDiagnostics,
                    onLifecycle: self.onLifecycle,
                    onFrameFailed: self.onFrameFailed,
                    onFrameRendered: self.onFrameRendered,
                    forceRender: request.forceRender
                )
                if let pending {
                    self.beginPreparation(pending)
                }
                return
            }
        }

        func send(
            _ payload: WebTranscriptPreparedPayload,
            to webView: WKWebView,
            diagnosticsEnabled: Bool,
            onNearTop: (() -> Void)?,
            onNeedsMoreHistory: (() -> Void)?,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?,
            onLifecycle: ((String) -> Void)?,
            onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)?,
            onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)?,
            forceRender: Bool = false
        ) {
            self.webView = webView
            self.diagnosticsEnabled = diagnosticsEnabled
            self.onNearTop = onNearTop
            self.onNeedsMoreHistory = onNeedsMoreHistory
            self.onDiagnostics = onDiagnostics
            self.onLifecycle = onLifecycle
            self.onFrameFailed = onFrameFailed
            self.onFrameRendered = onFrameRendered
            if forceRender {
                lastPayload = nil
                lastDuplicatePayload = nil
            } else if payloadMatchesRendered(payload)
                || payloadMatches(payload, inFlightPayload)
                || payloadMatches(payload, pendingPayload) {
                emitDuplicateDiagnosticsOnce(
                    payload: payload,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                return
            }
            lastDuplicatePayload = nil
            pendingPayload = payload
            guard isLoaded else {
                if forceRender, let serverURL = documentServerURL {
                    // A failed initial navigation leaves WebKit without a
                    // document. The retry revision is the explicit user
                    // request to start that one document load again.
                    loadDocument(serverURL: serverURL, on: webView)
                }
                emitDiagnostics(
                    stage: "queued",
                    payload: payload,
                    sequence: renderSequence + 1,
                    error: nil,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                return
            }
            flushPendingPayload(
                to: webView,
                diagnosticsEnabled: diagnosticsEnabled,
                onDiagnostics: onDiagnostics
            )
        }

        private func payloadMatches(
            _ lhs: WebTranscriptPreparedPayload,
            _ rhs: WebTranscriptPreparedPayload?
        ) -> Bool {
            guard let rhs else { return false }
            // Revisions and the fingerprint first: the base64 is the whole
            // transcript, and a changed payload almost always differs there.
            return lhs.contentRevision == rhs.contentRevision
                && lhs.transcriptReadThrough == rhs.transcriptReadThrough
                && lhs.retryRevision == rhs.retryRevision
                && lhs.payloadFingerprint == rhs.payloadFingerprint
                && lhs.base64 == rhs.base64
        }

        private func renderReceipt(for payload: WebTranscriptPreparedPayload) -> WebTranscriptRenderReceipt {
            WebTranscriptRenderReceipt(
                contentRevision: payload.contentRevision,
                transcriptReadThrough: payload.transcriptReadThrough,
                retryRevision: payload.retryRevision,
                payloadFingerprint: payload.payloadFingerprint,
                latestItemId: payload.latestItemId
            )
        }

        private func payloadMatchesRendered(_ payload: WebTranscriptPreparedPayload) -> Bool {
            payloadMatches(payload, lastRenderedPayload)
                && payload.base64 == lastPayload
        }

        private func flushPendingPayload(
            to webView: WKWebView,
            diagnosticsEnabled: Bool = WebTranscriptDiagnosticsFeature.isEnabled,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil
        ) {
            guard inFlightPayload == nil else { return }
            guard let payload = pendingPayload else { return }
            pendingPayload = nil
            guard !payloadMatchesRendered(payload) else {
                emitDuplicateDiagnosticsOnce(
                    payload: payload,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                return
            }

            renderSequence += 1
            let sequence = renderSequence
            let stick = shouldStickToBottom && !userScrollInProgress
            let renderMode = UITestHooks.transcriptBenchmarkRenderer == "retained-webkit"
                ? "retained"
                : "snapshot"
            inFlightPayload = payload
            let renderStartedAt = Date()
            let documentGeneration = self.documentGeneration
            if shouldStickToBottom && !userScrollInProgress {
                suppressNearTopUntil = renderStartedAt.addingTimeInterval(0.75)
            }
            webView.evaluateJavaScript(
                "window.renderTranscript('\(payload.base64)', \(stick ? "true" : "false"), \(sequence), '\(renderMode)');"
            ) { [weak self] value, error in
                guard let self,
                      self.webView === webView,
                      self.documentGeneration == documentGeneration
                else { return }
                let renderDurationMs = Int(Date().timeIntervalSince(renderStartedAt) * 1000)
                let synchronousMetrics = value.flatMap(WebTranscriptJavaScriptMetrics.init)
                let receipt = self.renderReceipt(for: payload)
                if error == nil {
                    self.lastPayload = payload.base64
                    self.lastRenderedPayload = payload
                } else {
                    self.jsFailureCount += 1
                    // Keep the identity: Renderer Retry increments its retry
                    // nonce and explicitly forces this same payload again.
                }
                if stick, self.shouldStickToBottom, !self.userScrollInProgress {
                    self.suppressNearTopUntil = Date().addingTimeInterval(0.75)
                }
                self.inFlightPayload = nil
                self.flushPendingPayload(
                    to: webView,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                guard error == nil else {
                    Task { @MainActor in
                        self.onFrameFailed?(receipt)
                        self.onLifecycle?("transcript_frame_failed")
                    }
                    self.emitDiagnostics(
                        stage: "failed",
                        payload: payload,
                        sequence: sequence,
                        error: error,
                        renderDurationMs: renderDurationMs,
                        javaScriptMetrics: synchronousMetrics,
                        diagnosticsEnabled: diagnosticsEnabled,
                        onDiagnostics: onDiagnostics
                    )
                    return
                }
                webView.callAsyncJavaScript(
                    """
                    return await Promise.race([
                        window.waitForTranscriptFrame(sequence),
                        new Promise((_, reject) => setTimeout(
                            () => reject(new Error("transcript frame acknowledgement timed out")),
                            3000
                        ))
                    ]);
                    """,
                    arguments: ["sequence": sequence],
                    in: nil,
                    in: .page
                ) { [weak self] frameResult in
                    guard let self,
                          self.webView === webView,
                          self.documentGeneration == documentGeneration
                    else { return }
                    let renderDurationMs = Int(Date().timeIntervalSince(renderStartedAt) * 1000)
                    let receipt = self.renderReceipt(for: payload)
                    let frameMetrics: WebTranscriptJavaScriptMetrics?
                    switch frameResult {
                    case .success(let value):
                        frameMetrics = WebTranscriptJavaScriptMetrics(value)
                    case .failure(let error):
                        Task { @MainActor in
                            self.onFrameFailed?(receipt)
                            self.onLifecycle?("transcript_frame_failed")
                        }
                        self.emitDiagnostics(
                            stage: "failed",
                            payload: payload,
                            sequence: sequence,
                            error: error,
                            renderDurationMs: renderDurationMs,
                            javaScriptMetrics: synchronousMetrics,
                            diagnosticsEnabled: diagnosticsEnabled,
                            onDiagnostics: onDiagnostics
                        )
                        return
                    }
                    Task { @MainActor in
                        self.onFrameRendered?(receipt)
                        self.onLifecycle?("transcript_frame_rendered")
                    }
                    self.emitDiagnostics(
                        stage: "rendered",
                        payload: payload,
                        sequence: sequence,
                        error: nil,
                        renderDurationMs: renderDurationMs,
                        javaScriptMetrics: synchronousMetrics?.merging(frameMetrics),
                        diagnosticsEnabled: diagnosticsEnabled,
                        onDiagnostics: onDiagnostics
                    )
                    // A render changes the content height the same way a
                    // keyboard changes the viewport height: the offset UIKit is
                    // holding may now be past the last row. Re-clamp through
                    // the same generation-guarded path so a pinned transcript
                    // never rests on a blank band after content shrinks.
                    let height = webView.bounds.height
                    self.viewportHeightDidChange(from: height, to: height, on: webView)
                }
            }
        }

        private func emitDiagnostics(
            stage: String,
            payload: WebTranscriptPreparedPayload,
            sequence: Int,
            error: Error?,
            renderDurationMs: Int? = nil,
            javaScriptMetrics: WebTranscriptJavaScriptMetrics? = nil,
            diagnosticsEnabled: Bool,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
        ) {
            guard diagnosticsEnabled else { return }
            let diagnostics = RenderBeaconReporter.WebKitDiagnostics(
                stage: stage,
                payload_byte_size: payload.payloadByteSize,
                row_count: payload.rowCount,
                latest_item_id: payload.latestItemId,
                payload_fingerprint: payload.payloadFingerprint,
                render_sequence: sequence,
                js_failure_count: jsFailureCount,
                should_stick_to_bottom: shouldStickToBottom,
                web_view_loaded: isLoaded,
                source_revision: payload.sourceRevision,
                source_operation: payload.sourceOperation,
                swift_prepare_duration_ms: payload.prepareDurationMs,
                render_duration_ms: renderDurationMs,
                js_decode_duration_ms: javaScriptMetrics?.decodeDurationMs,
                js_html_duration_ms: javaScriptMetrics?.htmlDurationMs,
                js_dom_duration_ms: javaScriptMetrics?.domDurationMs,
                js_raf_duration_ms: javaScriptMetrics?.rafDurationMs,
                js_total_duration_ms: javaScriptMetrics?.totalDurationMs,
                error_description: error.map { String(describing: $0) }
            )
            logger.debug(
                "webkit transcript stage=\(stage, privacy: .public) sequence=\(sequence) revision=\(payload.sourceRevision ?? -1) operation=\(payload.sourceOperation ?? "none", privacy: .public) rows=\(payload.rowCount) bytes=\(payload.payloadByteSize) latest=\(payload.latestItemId ?? "none", privacy: .public) failures=\(self.jsFailureCount) stick=\(self.shouldStickToBottom) prepare_ms=\(payload.prepareDurationMs) render_ms=\(renderDurationMs ?? -1) js_decode_ms=\(javaScriptMetrics?.decodeDurationMs ?? -1) js_html_ms=\(javaScriptMetrics?.htmlDurationMs ?? -1) js_dom_ms=\(javaScriptMetrics?.domDurationMs ?? -1) js_raf_ms=\(javaScriptMetrics?.rafDurationMs ?? -1)"
            )
            onDiagnostics?(diagnostics)
        }

        private func emitDuplicateDiagnosticsOnce(
            payload: WebTranscriptPreparedPayload,
            diagnosticsEnabled: Bool,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
        ) {
            guard payload.base64 != lastDuplicatePayload else { return }
            lastDuplicatePayload = payload.base64
            emitDiagnostics(
                stage: "duplicate",
                payload: payload,
                sequence: renderSequence,
                error: nil,
                diagnosticsEnabled: diagnosticsEnabled,
                onDiagnostics: onDiagnostics
            )
        }
    }
}

private struct WebTranscriptJavaScriptMetrics {
    let decodeDurationMs: Int?
    let htmlDurationMs: Int?
    let domDurationMs: Int?
    let rafDurationMs: Int?
    let totalDurationMs: Int?

    init?(_ value: Any) {
        guard let dictionary = value as? [String: Any] else { return nil }
        func milliseconds(_ key: String) -> Int? {
            guard let value = dictionary[key] as? NSNumber else { return nil }
            return Int(value.doubleValue.rounded())
        }
        decodeDurationMs = milliseconds("decode_ms")
        htmlDurationMs = milliseconds("html_ms")
        domDurationMs = milliseconds("dom_ms")
        rafDurationMs = milliseconds("raf_ms")
        totalDurationMs = milliseconds("total_ms")
    }

    private init(
        decodeDurationMs: Int?,
        htmlDurationMs: Int?,
        domDurationMs: Int?,
        rafDurationMs: Int?,
        totalDurationMs: Int?
    ) {
        self.decodeDurationMs = decodeDurationMs
        self.htmlDurationMs = htmlDurationMs
        self.domDurationMs = domDurationMs
        self.rafDurationMs = rafDurationMs
        self.totalDurationMs = totalDurationMs
    }

    func merging(_ other: WebTranscriptJavaScriptMetrics?) -> WebTranscriptJavaScriptMetrics {
        WebTranscriptJavaScriptMetrics(
            decodeDurationMs: decodeDurationMs ?? other?.decodeDurationMs,
            htmlDurationMs: htmlDurationMs ?? other?.htmlDurationMs,
            domDurationMs: domDurationMs ?? other?.domDurationMs,
            rafDurationMs: rafDurationMs ?? other?.rafDurationMs,
            totalDurationMs: totalDurationMs ?? other?.totalDurationMs
        )
    }
}
