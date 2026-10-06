import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

/// Renders the transcript body in WebKit while leaving the session chrome,
/// runtime controls, and composer native.
struct WebTranscriptView: UIViewRepresentable {
    let serverURL: String
    let items: [TimelineItem]
    /// Workers this session spawned, attached to the tool rows that spawned them.
    let subagents: [SessionSubagent]
    let submittedInputs: [SubmittedInput]
    let errorMessage: String?
    /// Changes exactly when the payload inputs above change. `updateUIView` runs
    /// on every invalidation of the session screen, and preparing the payload
    /// encodes and base64s the whole transcript, so the encode is gated on this
    /// rather than on the encoded bytes.
    let contentRevision: UInt64
    /// The transcript watermark belonging to this payload, not merely the
    /// currently visible session detail.
    let transcriptReadThrough: String?
    /// Nonzero only when the native surface is retrying a failed frame
    /// acknowledgement for an otherwise unchanged transcript payload.
    let retryRevision: UInt64
    let sourceRevision: Int?
    let sourceOperation: String?
    /// Full bodies loaded for rows a lite page cut. Changes bump the view
    /// model's transcript revision, so `contentRevision` covers them.
    let liteBodies: LiteBodyState
    let onNearTop: (() -> Void)?
    /// A render left too little scroll range for the near-top callback; the
    /// owner can load older history so the transcript reaches the composer.
    let onNeedsMoreHistory: (() -> Void)?
    /// Fires when the native transcript owner is pulled to refresh.
    let onRefresh: (() async -> Void)?
    /// Diagnostics from the acknowledged WebKit frame.
    let onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
    let onLifecycle: ((String) -> Void)?
    /// Tapping a worker row opens that child's transcript.
    let onOpenSubagent: ((String) -> Void)?
    /// Input retry/edit/discard actions stay native; the WebView never receives retained bytes.
    let onEditSubmittedInput: ((String) -> Void)?
    let onDiscardSubmittedInput: ((String) -> Void)?
    let onRetrySubmittedInput: ((String) -> Void)?
    /// An expanded row wants the full bodies of its cut events.
    let onLoadToolBodies: (([String]) -> Void)?
    /// Fires when WebKit rejects the payload's frame acknowledgement.
    let onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)?
    /// Fires only after this payload's DOM frame was acknowledged by WebKit.
    let onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)?

    init(
        serverURL: String,
        items: [TimelineItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        contentRevision: UInt64,
        transcriptReadThrough: String? = nil,
        retryRevision: UInt64 = 0,
        sourceRevision: Int? = nil,
        sourceOperation: String? = nil,
        liteBodies: LiteBodyState = LiteBodyState(),
        onNearTop: (() -> Void)? = nil,
        onNeedsMoreHistory: (() -> Void)? = nil,
        onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onLifecycle: ((String) -> Void)? = nil,
        onRefresh: (() async -> Void)? = nil,
        onOpenSubagent: ((String) -> Void)? = nil,
        onEditSubmittedInput: ((String) -> Void)? = nil,
        onDiscardSubmittedInput: ((String) -> Void)? = nil,
        onRetrySubmittedInput: ((String) -> Void)? = nil,
        onLoadToolBodies: (([String]) -> Void)? = nil,
        onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)? = nil,
        onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)? = nil
    ) {
        self.serverURL = serverURL
        self.items = items
        self.subagents = subagents
        self.onRefresh = onRefresh
        self.onOpenSubagent = onOpenSubagent
        self.onEditSubmittedInput = onEditSubmittedInput
        self.onDiscardSubmittedInput = onDiscardSubmittedInput
        self.onRetrySubmittedInput = onRetrySubmittedInput
        self.onLoadToolBodies = onLoadToolBodies
        self.liteBodies = liteBodies
        self.submittedInputs = submittedInputs
        self.errorMessage = errorMessage
        self.contentRevision = contentRevision
        self.transcriptReadThrough = transcriptReadThrough
        self.retryRevision = retryRevision
        self.sourceRevision = sourceRevision
        self.sourceOperation = sourceOperation
        self.onNearTop = onNearTop
        self.onNeedsMoreHistory = onNeedsMoreHistory
        self.onDiagnostics = onDiagnostics
        self.onLifecycle = onLifecycle
        self.onFrameFailed = onFrameFailed
        self.onFrameRendered = onFrameRendered
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> TranscriptWebView {
        let pooled = WebTranscriptWebViewPool.takeOrCreate()
        let webView = pooled.webView
        webView.navigationDelegate = context.coordinator
        // One narrowly-scoped bridge: a session id, validated as a UUID before
        // it reaches navigation. Custom-scheme links stay inert on purpose (see
        // `decidePolicyFor`), so transcript text still has no route out of here.
        context.coordinator.onOpenSubagent = onOpenSubagent
        context.coordinator.onEditSubmittedInput = onEditSubmittedInput
        context.coordinator.onDiscardSubmittedInput = onDiscardSubmittedInput
        context.coordinator.onRetrySubmittedInput = onRetrySubmittedInput
        context.coordinator.onLoadToolBodies = onLoadToolBodies
        context.coordinator.onRefresh = onRefresh
        context.coordinator.onFrameFailed = onFrameFailed
        context.coordinator.onFrameRendered = onFrameRendered
        let controller = webView.configuration.userContentController
        controller.removeScriptMessageHandler(forName: WebTranscriptView.bridgeName)
        controller.add(context.coordinator, name: WebTranscriptView.bridgeName)
        webView.scrollView.delegate = context.coordinator
        webView.scrollView.keyboardDismissMode = .interactive
        webView.scrollView.isScrollEnabled = true
        webView.scrollView.bounces = true
        webView.scrollView.alwaysBounceVertical = true
        webView.scrollView.scrollsToTop = true
        // SwiftUI's `.refreshable` only installs a refresh control for native
        // SwiftUI scroll containers. This transcript's owner is WebKit's
        // UIScrollView, so install the control on that owner explicitly.
        webView.scrollView.refreshControl?.removeTarget(
            nil,
            action: nil,
            for: .valueChanged
        )
        let refreshControl = UIRefreshControl()
        refreshControl.addTarget(
            context.coordinator,
            action: #selector(Coordinator.refreshControlDidChange(_:)),
            for: .valueChanged
        )
        webView.scrollView.refreshControl = refreshControl
        context.coordinator.refreshControl = refreshControl
        // SwiftUI lays this view out INSIDE the safe area, so the WebView frame
        // already stops at the top of the floating control card and the DOM's
        // 18px bottom padding is only the comfort gap above it. Disable the
        // scroll view's automatic safe-area inset so it cannot add a second
        // clearance on top of that padding.
        //
        // Consequence, and the reason the DOM re-pins on resize: the frame
        // height is NOT constant. It moves with the card, the keyboard, and the
        // safe area, and UIScrollView does not re-clamp contentOffset when its
        // bounds change.
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.backgroundColor = .clear
        // Capture the coordinator, not the whole representable context: an
        // escaping closure that holds Context also pins the SwiftUI environment.
        let coordinator = context.coordinator
        webView.onViewportHeightChange = { [weak webView] previous, height in
            guard let webView else { return }
            coordinator.viewportHeightDidChange(from: previous, to: height, on: webView)
        }
        coordinator.webView = webView
        coordinator.observeContentSize(on: webView)
        coordinator.configureMediaAuth(serverURL: serverURL, on: webView)
        let lifecycleStage = pooled.reused ? "webview_reused" : "webview_make"
        WebTranscriptWebViewPool.logAdoption(webView, reused: pooled.reused, loaded: pooled.isLoaded)
        Task { @MainActor in
            onLifecycle?(lifecycleStage)
        }
        if pooled.reused {
            // Adopt the warm spare's existing navigation, even if WebKit is
            // still finishing it. Restarting loadHTMLString() here discarded
            // launch prewarm work exactly when the user opened a session early.
            coordinator.adoptDocument(serverURL: serverURL, loaded: pooled.isLoaded)
            if pooled.isLoaded {
                Task { @MainActor in
                    onLifecycle?("webview_document_reused")
                }
            }
        } else {
            coordinator.loadDocument(serverURL: serverURL, on: webView)
        }
        return webView
    }

    func updateUIView(_ webView: TranscriptWebView, context: Context) {
        context.coordinator.configureMediaAuth(serverURL: serverURL, on: webView)
        context.coordinator.onRefresh = onRefresh
        context.coordinator.onEditSubmittedInput = onEditSubmittedInput
        context.coordinator.onDiscardSubmittedInput = onDiscardSubmittedInput
        context.coordinator.onRetrySubmittedInput = onRetrySubmittedInput
        context.coordinator.onLoadToolBodies = onLoadToolBodies
        context.coordinator.ensureDocumentServerURL(serverURL, on: webView)
        let preparationInput = WebTranscriptPayloadInput(
            serverURL: serverURL,
            timelineItems: items,
            subagents: subagents,
            submittedInputs: submittedInputs,
            errorMessage: errorMessage,
            contentRevision: contentRevision,
            transcriptReadThrough: transcriptReadThrough,
            retryRevision: retryRevision,
            sourceRevision: sourceRevision,
            sourceOperation: sourceOperation,
            liteBodies: liteBodies
        )
        context.coordinator.send(
            contentIdentity: ContentIdentity(
                serverURL: serverURL,
                revision: contentRevision,
                transcriptReadThrough: transcriptReadThrough,
                retryRevision: retryRevision
            ),
            preparationInput: preparationInput,
            to: webView,
            diagnosticsEnabled: WebTranscriptDiagnosticsFeature.isEnabled,
            onNearTop: onNearTop,
            onNeedsMoreHistory: onNeedsMoreHistory,
            onDiagnostics: onDiagnostics,
            onLifecycle: onLifecycle,
            onFrameFailed: onFrameFailed,
            onFrameRendered: onFrameRendered
        )
    }

    /// What a prepared payload was built from. Two updates carrying the same
    /// identity would encode to the same bytes, so the second one skips the work.
    struct ContentIdentity: Equatable, Sendable {
        let serverURL: String
        let revision: UInt64
        let transcriptReadThrough: String?
        let retryRevision: UInt64
    }

    static let bridgeName = "longhouse"

    /// The document renders untrusted transcript text, so it only ever gets a
    /// web origin. The base URL exists so authenticated media resolves against
    /// the Runtime Host; a `file:` one would instead hand the document the app
    /// container, and any other scheme would give it an origin nothing here
    /// reasons about. Both fall back to `about:blank`, which can read nothing —
    /// media stops resolving, which is visible.
    nonisolated static func documentBaseURL(_ serverURL: String?) -> URL? {
        guard
            let serverURL,
            let url = URL(string: serverURL),
            let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https"
        else { return nil }
        return url
    }

    static func dismantleUIView(_ webView: TranscriptWebView, coordinator: Coordinator) {
        // The content controller retains its handler strongly; leaving it
        // registered would keep this coordinator (and the session it closes
        // over) alive inside a pooled WebView.
        webView.configuration.userContentController.removeScriptMessageHandler(forName: bridgeName)
        coordinator.onOpenSubagent = nil
        let documentIsLoaded = coordinator.isLoaded
        coordinator.prepareForReuse()
        // The only navigation this coordinator permits is our transcript
        // document. Preserve an in-flight load too: dropping it during a SwiftUI
        // representable transition starts a second WebContent process and loses
        // the prewarm precisely on the cold-open path.
        WebTranscriptWebViewPool.recycle(webView, documentIsLoaded: documentIsLoaded)
    }

    private func preparedPayload() -> WebTranscriptPreparedPayload {
        Self.preparedPayload(
            serverURL: serverURL,
            timelineItems: items,
            subagents: subagents,
            submittedInputs: submittedInputs,
            errorMessage: errorMessage,
            contentRevision: contentRevision,
            transcriptReadThrough: transcriptReadThrough,
            retryRevision: retryRevision,
            sourceRevision: sourceRevision,
            sourceOperation: sourceOperation
        )
    }
}

enum WebTranscriptDiagnosticsFeature {
    static let environmentKey = "LONGHOUSE_WEBKIT_TRANSCRIPT_DIAGNOSTICS"
    static let userDefaultsKey = "longhouse.webkitTranscriptDiagnostics.enabled"

    static var isEnabled: Bool {
        if let raw = ProcessInfo.processInfo.environment[environmentKey] {
            let normalized = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            return ["1", "true", "yes", "on"].contains(normalized)
        }
#if DEBUG
        return true
#else
        return UserDefaults.standard.bool(forKey: userDefaultsKey)
#endif
    }
}

#if DEBUG
extension WebTranscriptView {
    /// Test-only accessor for the assembled transcript document: the bundled
    /// resource with the palette spliced in, exactly what the WebView loads.
    static var documentHTMLForTesting: String { documentHTML }
}
#endif

extension WebTranscriptView {
    /// Assembled document: the palette's CSS variable block (single source of
    /// truth, TranscriptPalette.swift) spliced into the bundled template at the
    /// `__LH_ROOT_BLOCK__` marker. Ends the Swift/CSS color double-definition.
    static var documentHTML: String {
        documentTemplate.replacingOccurrences(of: "/* __LH_ROOT_BLOCK__ */", with: TranscriptPalette.cssRootBlock)
    }

    /// The transcript document, read once from the app bundle. Its source is
    /// web/src/embeds/ios-transcript; `make generate-ios-transcript` rebuilds
    /// ios/Resources/Transcript/transcript.html and `make validate` fails when
    /// the checked-in file is stale.
    static let documentTemplate: String = {
        guard let url = Bundle.main.url(forResource: "transcript", withExtension: "html", subdirectory: "Transcript"),
              let html = try? String(contentsOf: url, encoding: .utf8) else {
            preconditionFailure("Transcript/transcript.html is missing from the app bundle")
        }
        return html
    }()
}
