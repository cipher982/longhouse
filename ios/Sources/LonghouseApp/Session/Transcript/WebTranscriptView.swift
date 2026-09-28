import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

/// The transcript WebView's height is not constant: the floating control card,
/// the keyboard, and the safe area all resize it through SwiftUI. UIScrollView
/// does not re-clamp `contentOffset` when its bounds change, so a pinned
/// transcript ends up short of (or past) its last row — the reported symptom
/// was a black band the exact height of a dismissed keyboard, which is the
/// viewport delta, cleared by a drag because a drag reconciles the offset.
///
/// The DOM's `resize` listener already tried to cover this, but only if WebKit
/// delivers the event; its regression test dispatches the event by hand, so the
/// trigger was never under test. `layoutSubviews` is the one trigger UIKit
/// guarantees on a frame change.
final class TranscriptWebView: WKWebView {
    let transcriptInstanceID = UUID().uuidString.prefix(8)
    /// (previousHeight, newHeight). Only fires on a real height change.
    var onViewportHeightChange: ((CGFloat, CGFloat) -> Void)?
    /// Seeded to zero rather than "unset": treating the first layout as a
    /// baseline to record silently swallows a change when the first layout pass
    /// IS the change, which is exactly what happens to a pooled WebView adopted
    /// into a session whose chrome is already a different height.
    private var observedHeight: CGFloat = 0

    override func layoutSubviews() {
        super.layoutSubviews()
        let height = bounds.height
        guard height > 0, abs(height - observedHeight) > 0.5 else { return }
        let previous = observedHeight
        observedHeight = height
        onViewportHeightChange?(previous, height)
    }

    func prepareForTranscriptReuse() {
        onViewportHeightChange = nil
        observedHeight = 0
    }
}

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
    let onNearTop: (() -> Void)?
    /// A render left too little scroll range for the near-top callback; the
    /// owner can load older history so the transcript reaches the composer.
    let onNeedsMoreHistory: (() -> Void)?
    let onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
    let onLifecycle: ((String) -> Void)?
    /// Tapping a worker row opens that child's transcript.
    let onOpenSubagent: ((String) -> Void)?
    /// Input retry/edit/discard actions stay native; the WebView never receives retained bytes.
    let onEditSubmittedInput: ((String) -> Void)?
    let onDiscardSubmittedInput: ((String) -> Void)?
    let onRetrySubmittedInput: ((String) -> Void)?
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
        onNearTop: (() -> Void)? = nil,
        onNeedsMoreHistory: (() -> Void)? = nil,
        onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onLifecycle: ((String) -> Void)? = nil,
        onOpenSubagent: ((String) -> Void)? = nil,
        onEditSubmittedInput: ((String) -> Void)? = nil,
        onDiscardSubmittedInput: ((String) -> Void)? = nil,
        onRetrySubmittedInput: ((String) -> Void)? = nil,
        onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)? = nil,
        onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)? = nil
    ) {
        self.serverURL = serverURL
        self.items = items
        self.subagents = subagents
        self.onOpenSubagent = onOpenSubagent
        self.onEditSubmittedInput = onEditSubmittedInput
        self.onDiscardSubmittedInput = onDiscardSubmittedInput
        self.onRetrySubmittedInput = onRetrySubmittedInput
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
        context.coordinator.onFrameFailed = onFrameFailed
        context.coordinator.onFrameRendered = onFrameRendered
        let controller = webView.configuration.userContentController
        controller.removeScriptMessageHandler(forName: WebTranscriptView.bridgeName)
        controller.add(context.coordinator, name: WebTranscriptView.bridgeName)
        webView.scrollView.delegate = context.coordinator
        webView.scrollView.keyboardDismissMode = .interactive
        webView.scrollView.alwaysBounceVertical = true
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
        context.coordinator.onEditSubmittedInput = onEditSubmittedInput
        context.coordinator.onDiscardSubmittedInput = onDiscardSubmittedInput
        context.coordinator.onRetrySubmittedInput = onRetrySubmittedInput
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
            sourceOperation: sourceOperation
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

    struct WebTranscriptPayloadInput: Sendable {
        let serverURL: String?
        let timelineItems: [TimelineItem]
        let subagents: [SessionSubagent]
        let submittedInputs: [SubmittedInput]
        let errorMessage: String?
        let contentRevision: UInt64
        let transcriptReadThrough: String?
        let retryRevision: UInt64
        let sourceRevision: Int?
        let sourceOperation: String?
    }

    nonisolated static func preparedPayload(
        serverURL: String? = nil,
        timelineItems: [TimelineItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        contentRevision: UInt64 = 0,
        transcriptReadThrough: String? = nil,
        retryRevision: UInt64 = 0,
        sourceRevision: Int? = nil,
        sourceOperation: String? = nil
    ) -> WebTranscriptPreparedPayload {
        let startedAt = Date()
        let payload = WebTranscriptPayload(
            errorMessage: errorMessage,
            items: Self.payloadItems(
                serverURL: serverURL,
                timelineItems: timelineItems,
                subagents: subagents,
                submittedInputs: submittedInputs
            )
        )
        let encoder = JSONEncoder()
        let data = (try? encoder.encode(payload)) ?? Data()
        return WebTranscriptPreparedPayload(
            base64: data.base64EncodedString(),
            payloadByteSize: data.count,
            rowCount: payload.items.count,
            latestItemId: payload.items.last?.id,
            payloadFingerprint: Self.payloadFingerprint(data),
            prepareDurationMs: Int(Date().timeIntervalSince(startedAt) * 1000),
            contentRevision: contentRevision,
            transcriptReadThrough: transcriptReadThrough,
            retryRevision: retryRevision,
            sourceRevision: sourceRevision,
            sourceOperation: sourceOperation
        )
    }

    nonisolated static func preparedPayload(
        input: WebTranscriptPayloadInput
    ) -> WebTranscriptPreparedPayload {
        preparedPayload(
            serverURL: input.serverURL,
            timelineItems: input.timelineItems,
            subagents: input.subagents,
            submittedInputs: input.submittedInputs,
            errorMessage: input.errorMessage,
            contentRevision: input.contentRevision,
            transcriptReadThrough: input.transcriptReadThrough,
            retryRevision: input.retryRevision,
            sourceRevision: input.sourceRevision,
            sourceOperation: input.sourceOperation
        )
    }

    private nonisolated static func payloadFingerprint(_ data: Data) -> String {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in data {
            hash ^= UInt64(byte)
            hash &*= 0x100000001b3
        }
        return String(format: "%016llx", hash)
    }

    nonisolated static func payloadItems(
        serverURL: String? = nil,
        timelineItems: [TimelineItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput]
    ) -> [WebTranscriptPayloadItem] {
        let durableUserInputs = durableUserInputIdentities(timelineItems)
        let visibleSubmittedInputs = submittedInputs.filter { !durableUserInputs.contains($0) }
        guard !visibleSubmittedInputs.isEmpty else {
            return timelineItems.map { payloadItem($0, serverURL: serverURL, subagents: subagents) }
        }

        var rows: [WebTranscriptPayloadItem] = []
        var remainingSubmittedInputs = visibleSubmittedInputs
        for item in timelineItems {
            if let previewDate = liveProvisionalAssistantDate(item) {
                let insertion = submittedInputsToPlaceBeforeLivePreview(
                    remainingSubmittedInputs,
                    previewDate: previewDate
                )
                if !insertion.isEmpty {
                    let insertionIds = Set(insertion.map(\.id))
                    rows.append(contentsOf: insertion.map(payloadSubmittedInput))
                    remainingSubmittedInputs.removeAll { insertionIds.contains($0.id) }
                }
            }
            rows.append(payloadItem(item, serverURL: serverURL, subagents: subagents))
        }

        rows.append(contentsOf: remainingSubmittedInputs.map(payloadSubmittedInput))
        return rows
    }

    private nonisolated static func liveProvisionalAssistantDate(_ item: TimelineItem) -> Date? {
        guard case .assistant(let event) = item else { return nil }
        guard event.eventOrigin == "live_provisional" || event.isSynthetic else { return nil }
        return LonghouseDateParser.parse(event.timestamp) ?? .distantPast
    }

    private nonisolated static func submittedInputsToPlaceBeforeLivePreview(
        _ inputs: [SubmittedInput],
        previewDate: Date
    ) -> [SubmittedInput] {
        inputs.filter { input in
            switch input.phase {
            case .submitting, .working, .sent:
                return true
            case .queued:
                return input.createdAt <= previewDate
            case .couldNotConfirm, .failed, .needsUserDecision:
                return false
            }
        }
    }

    private nonisolated static func durableUserInputIdentities(_ timelineItems: [TimelineItem]) -> DurableUserInputIdentities {
        var sessionInputIds = Set<Int>()
        var clientRequestIds = Set<String>()

        for item in timelineItems {
            guard case .user(let event) = item,
                  event.isHeadBranch,
                  let origin = event.inputOrigin else { continue }
            if let sessionInputId = origin.sessionInputId {
                sessionInputIds.insert(sessionInputId)
            }
            if let clientRequestId = origin.clientRequestId,
               !clientRequestId.isEmpty {
                clientRequestIds.insert(clientRequestId)
            }
        }

        return DurableUserInputIdentities(
            sessionInputIds: sessionInputIds,
            clientRequestIds: clientRequestIds
        )
    }

    private nonisolated static func payloadItem(
        _ item: TimelineItem,
        serverURL: String?,
        subagents: [SessionSubagent] = []
    ) -> WebTranscriptPayloadItem {
        var payload = payloadItemBody(item, serverURL: serverURL, subagents: subagents)
        payload.turnEnd = turnEndPayload(for: item)
        return payload
    }

    /// The provider stamps its turn accounting on one durable event; the
    /// footer belongs under whichever row carries that event.
    nonisolated static func turnEndPayload(for item: TimelineItem, now: Date = Date()) -> WebTranscriptTurnEnd? {
        let turnEnd: SessionTurnEnd?
        switch item {
        case .user(let event), .assistant(let event), .orphanTool(let event):
            turnEnd = event.turnEnd
        case .providerNotification:
            turnEnd = nil
        case .tool(let call, let result, _):
            turnEnd = result?.turnEnd ?? call.turnEnd
        case .activityGroup(let calls):
            turnEnd = calls.reversed().lazy.compactMap { $0.result?.turnEnd ?? $0.call.turnEnd }.first
        case .action:
            turnEnd = nil
        }
        guard let turnEnd else { return nil }
        let stopped = turnEnd.outcome == "aborted"
        return WebTranscriptTurnEnd(
            label: "\(stopped ? "Interrupted after" : "Worked for") \(TurnEndCopy.duration(milliseconds: turnEnd.durationMs))",
            doneAt: TurnEndCopy.doneAt(turnEnd.endedAt, now: now, verb: stopped ? "stopped" : "Turn finished")
        )
    }

    private nonisolated static func payloadItemBody(
        _ item: TimelineItem,
        serverURL: String?,
        subagents: [SessionSubagent] = []
    ) -> WebTranscriptPayloadItem {
        switch item {
        case .user(let event):
            return messagePayload(
                id: item.id,
                role: "user",
                text: event.contentText ?? "",
                origin: event.inputOrigin?.authoredVia,
                mediaRefs: payloadMediaRefs(event.mediaRefs, serverURL: serverURL)
            )
        case .assistant(let event):
            return messagePayload(
                id: item.id,
                role: "assistant",
                text: event.contentText ?? "",
                origin: nil,
                mediaRefs: payloadMediaRefs(event.mediaRefs, serverURL: serverURL)
            )
        case .providerNotification(let event):
            return providerNotificationPayload(id: item.id, text: event.contentText ?? "")
        case .action(let action, _):
            return actionPayload(id: item.id, action: action)
        case .tool(let call, let result, _):
            return toolPayload(id: item.id, call: call, result: result, serverURL: serverURL, subagents: subagents)
        case .orphanTool(let event):
            return toolPayload(
                id: item.id,
                call: event,
                result: event,
                orphan: true,
                serverURL: serverURL,
                subagents: subagents
            )
        case .activityGroup(let calls):
            return activityGroupPayload(
                id: item.id,
                calls: calls,
                serverURL: serverURL,
                subagents: subagents
            )
        }
    }

    private nonisolated static func messagePayload(
        id: String,
        role: String,
        text: String,
        origin: SessionInputAuthoredVia?,
        mediaRefs: [WebTranscriptMediaRef]? = nil
    ) -> WebTranscriptPayloadItem {
        let displayText = text
        let collapsed = TranscriptTextPolicy.shouldCollapseMessage(displayText)
        return WebTranscriptPayloadItem(
            id: id,
            kind: "message",
            role: role,
            title: nil,
            subtitle: nil,
            body: TranscriptTextPolicy.visibleMessage(displayText, expanded: false),
            fullBody: collapsed ? displayText : nil,
            collapsed: collapsed,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: origin?.payloadValue,
            media: mediaRefs
        )
    }

    private nonisolated static func actionPayload(id: String, action: SessionAction) -> WebTranscriptPayloadItem {
        WebTranscriptPayloadItem(
            id: id,
            kind: "action",
            role: nil,
            title: actionLabel(action.kind),
            subtitle: action.provider,
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: action.kind,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil
        )
    }

    private nonisolated static func providerNotificationPayload(
        id: String,
        text: String
    ) -> WebTranscriptPayloadItem {
        WebTranscriptPayloadItem(
            id: id,
            kind: "providerNotification",
            role: nil,
            title: nil,
            subtitle: nil,
            body: text,
            fullBody: nil,
            collapsed: false,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil
        )
    }

    private nonisolated static func actionLabel(_ kind: String) -> String {
        if kind == "turn_interrupted" { return "User interrupted the turn" }
        return "Session action"
    }

    private nonisolated static func payloadSubmittedInput(_ input: SubmittedInput) -> WebTranscriptPayloadItem {
        WebTranscriptPayloadItem(
            id: input.id,
            kind: "submitted",
            role: "user",
            title: nil,
            subtitle: submittedStatus(input.phase, lastError: input.lastError),
            body: input.text,
            fullBody: nil,
            collapsed: false,
            status: input.phase.rawValue,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil,
            attachments: input.attachmentSummaries
        )
    }

    private struct TranscriptQuestion: Sendable {
        let id: String
        let header: String?
        let question: String
        let options: [TranscriptQuestionOption]
    }

    private struct TranscriptQuestionOption: Sendable {
        let label: String
        let description: String?
    }

    private nonisolated static func transcriptQuestions(from input: [String: JSONValue]?) -> [TranscriptQuestion] {
        guard let input else { return [] }
        let rawQuestions: [JSONValue]
        if case .array(let questions) = input["questions"] {
            rawQuestions = questions
        } else if input["question"] != nil || input["prompt"] != nil {
            rawQuestions = [.object(input)]
        } else {
            rawQuestions = []
        }

        return rawQuestions.enumerated().compactMap { index, raw in
            guard case .object(let item) = raw else { return nil }
            let rawOptions: [JSONValue]
            if case .array(let options) = item["options"] {
                rawOptions = options
            } else if case .array(let choices) = item["choices"] {
                rawOptions = choices
            } else {
                rawOptions = []
            }
            let options = rawOptions.compactMap(transcriptQuestionOption)
            return TranscriptQuestion(
                id: jsonText(item["id"]) ?? jsonText(item["name"]) ?? jsonText(item["key"]) ?? "question-\(index + 1)",
                header: jsonText(item["header"]) ?? jsonText(item["title"]),
                question: jsonText(item["question"]) ?? jsonText(item["prompt"]) ?? jsonText(item["label"]) ?? "Answer required",
                options: options
            )
        }
    }

    private nonisolated static func transcriptQuestionOption(_ raw: JSONValue) -> TranscriptQuestionOption? {
        if case .object(let item) = raw {
            guard let label = jsonText(item["label"]) ?? jsonText(item["value"]) ?? jsonText(item["text"]) else {
                return nil
            }
            return TranscriptQuestionOption(
                label: label,
                description: jsonText(item["description"]) ?? jsonText(item["detail"])
            )
        }
        guard let label = jsonText(raw) else { return nil }
        return TranscriptQuestionOption(label: label, description: nil)
    }

    private nonisolated static func jsonText(_ value: JSONValue?) -> String? {
        let raw: String?
        switch value {
        case .string(let s): raw = s
        case .int(let n): raw = String(n)
        case .double(let n): raw = String(n)
        case .bool(let b): raw = String(b)
        case .array, .object, .null, .none: raw = nil
        }
        let cleaned = raw?.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned?.isEmpty == false ? cleaned : nil
    }

    private nonisolated static func toolPayload(
        id: String,
        call: SessionEvent,
        result: SessionEvent?,
        orphan: Bool = false,
        serverURL: String?,
        subagents: [SessionSubagent] = []
    ) -> WebTranscriptPayloadItem {
        let toolName = call.toolName ?? "Tool"
        if toolName == "AskUserQuestion" {
            return askUserQuestionPayload(id: id, call: call, result: result)
        }
        let presentedToolName = TimelineBuilder.presentedToolName(call)
        let resolved = ToolTiers.resolve(presentedToolName)
        let presentation = call.toolPresentation
        let duration = result.flatMap { TimelineBuilder.durationSeconds(call: call, result: $0) }
            .map(TimelineBuilder.formatDuration)
        // iOS previously had no failure surface at all: a non-zero exit or a
        // structured failure rendered as "done". One predicate now drives the
        // chip and the preview, as on web (R4).
        let failed = TimelineBuilder.isFailed(call: call, result: result)
        let exitCode = ShellSalienceClassifier.parseExitCode(result?.toolOutputText)
        let status: String? = {
            if orphan { return "orphan" }
            if call.toolCallState == .running { return "running" }
            if call.toolCallState == .dropped { return "dropped" }
            if failed { return exitCode.map { "exit \($0)" } ?? "failed" }
            if call.toolCallState == .completed { return "done" }
            return nil
        }()
        // Only edits get a stat or a diff. Computing this for every tool would
        // let any input carrying a `text`/`content` key masquerade as a file
        // creation and replace its Input block with a bogus diff.
        let editStat = TimelineBuilder.isEditInteraction(call) ? EditSummary.stat(for: call) : nil
        let editLabel = editStat.flatMap { EditSummary.format($0) }
        var presentationCalls = presentation?.children.map { child in
            WebTranscriptToolCall(
                title: child.label,
                subtitle: child.toolName,
                status: "parsed",
                input: prettyJSONValue(child.toolInputValue),
                rawInput: nil,
                output: presentation?.wrapperRecedes == true ? truncatedOutput(result?.toolOutputText) : nil,
                media: nil
            )
        } ?? []
        if presentation?.wrapperRecedes == true {
            presentationCalls.append(
                WebTranscriptToolCall(
                    title: "Raw enclosing \(presentation?.sourceToolName ?? toolName)",
                    subtitle: "Provider evidence",
                    status: "raw",
                    input: nil,
                    rawInput: prettyJSONValue(call.toolInputValue),
                    output: nil,
                    media: nil
                )
            )
        }

        let spawned = Subagents.children(
            from: subagents,
            toolCallId: call.toolCallId,
            toolOutputText: result?.toolOutputText
        )

        return WebTranscriptPayloadItem(
            id: id,
            kind: "tool",
            role: nil,
            title: presentation?.label ?? resolved.label,
            subtitle: editLabel ?? TimelineBuilder.inputSummary(for: call),
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: status,
            duration: duration,
            input: prettyJSONValue(
                presentation?.wrapperRecedes == true
                    ? presentation?.toolInputValue
                    : call.toolInputValue
            ) ?? TimelineBuilder.inputSummary(for: call),
            output: truncatedOutput(result?.toolOutputText),
            calls: presentationCalls,
            origin: presentation?.disposition == "parsed"
                ? "Parsed via \(presentation?.executionMethod ?? presentation?.sourceToolName ?? toolName)"
                : nil,
            media: payloadMediaRefs(call.mediaRefs + (result?.mediaRefs ?? []), serverURL: serverURL),
            failurePreview: TimelineBuilder.failurePreview(call: call, result: result),
            diff: editStat.flatMap { EditSummary.diffLines(for: $0) }.map { lines in
                lines.map { WebTranscriptDiffLine(kind: $0.kind.rawValue, text: $0.text) }
            },
            subagents: spawned.isEmpty
                ? nil
                : spawned.map {
                    WebTranscriptSubagent(
                        sessionId: $0.sessionId,
                        label: Subagents.label(for: $0),
                        toolCalls: $0.toolCalls
                    )
                },
            subagentSummary: spawned.isEmpty ? nil : Subagents.summary(spawned)
        )
    }

    private nonisolated static func askUserQuestionPayload(
        id: String,
        call: SessionEvent,
        result: SessionEvent?
    ) -> WebTranscriptPayloadItem {
        let questions = transcriptQuestions(from: call.toolInputJSON)
        let title = questions.first?.header ?? "Question"
        let body = questions.map(\.question).joined(separator: "\n\n")
        let options = questions.flatMap(\.options).map { option in
            WebTranscriptToolCall(
                title: option.label,
                subtitle: option.description ?? "",
                status: "option",
                input: nil,
                rawInput: nil,
                output: nil,
                media: nil
            )
        }
        return WebTranscriptPayloadItem(
            id: id,
            kind: "question",
            role: nil,
            title: title,
            subtitle: result == nil ? "Answer in terminal" : "Answered in terminal",
            body: body.isEmpty ? "Claude is waiting for your answer." : body,
            fullBody: nil,
            collapsed: false,
            status: result == nil ? "waiting" : "answered",
            duration: nil,
            input: nil,
            output: nil,
            calls: options,
            origin: nil,
            media: nil
        )
    }
    private nonisolated static func activityGroupPayload(
        id: String,
        calls: [ActivityCall],
        serverURL: String?,
        subagents: [SessionSubagent]
    ) -> WebTranscriptPayloadItem {
        let summary = TimelineBuilder.activitySummary(for: calls)
        var seenSubagentIds = Set<String>()
        let spawned = calls
            .flatMap { call in
                Subagents.children(
                    from: subagents,
                    toolCallId: call.call.toolCallId,
                    toolOutputText: call.result?.toolOutputText
                )
            }
            .filter { seenSubagentIds.insert($0.sessionId).inserted }
        let spawnedPayload = spawned.isEmpty
            ? nil
            : spawned.map {
                WebTranscriptSubagent(
                    sessionId: $0.sessionId,
                    label: Subagents.label(for: $0),
                    toolCalls: $0.toolCalls
                )
            }

        // Pass every call; WebKit renderer collapses to latest-N with an
        // interactive "Show N earlier" control (never permanent hide).
        let childCalls = calls.map { passive in
            let status: String = {
                switch passive.call.toolCallState {
                case .running: return "running"
                case .dropped: return "dropped"
                case .completed: return "done"
                case .none: return "done"
                }
            }()
            return WebTranscriptToolCall(
                title: passive.call.toolPresentation?.label
                    ?? ToolTiers.resolve(TimelineBuilder.presentedToolName(passive.call)).label,
                subtitle: TimelineBuilder.inputSummary(for: passive.call),
                status: status,
                input: prettyJSONValue(
                    passive.call.toolPresentation?.wrapperRecedes == true
                        ? passive.call.toolPresentation?.toolInputValue
                        : passive.call.toolInputValue
                ) ?? TimelineBuilder.inputSummary(for: passive.call),
                rawInput: passive.call.toolPresentation?.wrapperRecedes == true
                    ? prettyJSONValue(passive.call.toolInputValue)
                    : nil,
                output: truncatedOutput(passive.result?.toolOutputText),
                media: payloadMediaRefs(passive.call.mediaRefs + (passive.result?.mediaRefs ?? []), serverURL: serverURL)
            )
        }

        return WebTranscriptPayloadItem(
            id: id,
            kind: "activityGroup",
            role: nil,
            title: summary.isEmpty ? "Activity" : summary,
            subtitle: "\(calls.count)",
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: childCalls,
            origin: nil,
            media: nil,
            subagents: spawnedPayload,
            subagentSummary: spawned.isEmpty ? nil : Subagents.summary(spawned)
        )
    }

    private nonisolated static func payloadMediaRefs(
        _ refs: [SessionEventMediaRef],
        serverURL: String?
    ) -> [WebTranscriptMediaRef]? {
        var seen = Set<String>()
        let media = refs.compactMap { ref -> WebTranscriptMediaRef? in
            let dedupeURL = ref.thumbUrl ?? ref.blobUrl
            let dedupeKey = "\(ref.sha256):\(dedupeURL)"
            guard !seen.contains(dedupeKey) else {
                return nil
            }
            seen.insert(dedupeKey)
            let imageLike = ref.mimeType?.hasPrefix("image/") ?? true
            let visibleURL = ref.mediaState == "present" && imageLike
                ? absoluteMediaURL(ref.thumbUrl ?? ref.blobUrl, serverURL: serverURL)
                : nil
            if visibleURL == nil && ref.mediaState == "present" {
                return nil
            }
            return WebTranscriptMediaRef(
                sha256: ref.sha256,
                url: visibleURL,
                blobUrl: absoluteMediaURL(ref.blobUrl, serverURL: serverURL),
                mediaState: ref.mediaState,
                mimeType: ref.mimeType,
                width: ref.width,
                height: ref.height
            )
        }
        return media.isEmpty ? nil : media
    }

    private nonisolated static func absoluteMediaURL(_ rawURL: String?, serverURL: String?) -> String? {
        guard let rawURL = rawURL?.trimmingCharacters(in: .whitespacesAndNewlines),
              !rawURL.isEmpty else {
            return nil
        }
        if URL(string: rawURL)?.scheme != nil {
            return rawURL
        }
        guard let serverURL,
              let base = URL(string: serverURL),
              let resolved = URL(string: rawURL, relativeTo: base) else {
            return rawURL
        }
        return resolved.absoluteURL.absoluteString
    }

    private nonisolated static func prettyJSON(_ value: [String: JSONValue]?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(value) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private nonisolated static func prettyJSONValue(_ value: JSONValue?) -> String? {
        guard let value else { return nil }
        if case .object(let object) = value {
            return prettyJSON(object)
        }
        if case .string(let string) = value {
            return string
        }
        guard let data = try? JSONEncoder().encode(value),
              let rendered = String(data: data, encoding: .utf8) else { return nil }
        return rendered
    }

    private nonisolated static func truncatedOutput(_ text: String?) -> String? {
        guard let text, !text.isEmpty else { return nil }
        let maxCharacters = 12_000
        guard text.count > maxCharacters else { return text }
        return String(text.prefix(maxCharacters)) + "\n... truncated in iOS transcript ..."
    }

    private nonisolated static func submittedStatus(_ phase: SubmittedInputPhase, lastError: String?) -> String {
        switch phase {
        // One vocabulary with web: the durable echo replacing this row is the
        // confirmation, and turn progress belongs to the composer, so every
        // in-flight phase reads the same.
        case .submitting, .working: return "Sending…"
        case .sent: return "Sent"
        case .queued: return "Queued · sends after this turn"
        case .couldNotConfirm: return "Not confirmed"
        case .failed: return lastError.map { "Not delivered — \($0)" } ?? "Not delivered"
        case .needsUserDecision:
            return lastError.map { "Needs choice — \($0)" } ?? "Needs choice"
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, UIScrollViewDelegate, WKScriptMessageHandler {
        var onOpenSubagent: ((String) -> Void)?
        var onEditSubmittedInput: ((String) -> Void)?
        var onDiscardSubmittedInput: ((String) -> Void)?
        var onRetrySubmittedInput: ((String) -> Void)?
        var onFrameFailed: ((WebTranscriptRenderReceipt) -> Void)?
        var onFrameRendered: ((WebTranscriptRenderReceipt) -> Void)?

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
        fileprivate var isLoaded = false
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

struct WebTranscriptPreparedPayload: Sendable {
    let base64: String
    let payloadByteSize: Int
    let rowCount: Int
    let latestItemId: String?
    let payloadFingerprint: String
    let prepareDurationMs: Int
    let contentRevision: UInt64
    let transcriptReadThrough: String?
    let retryRevision: UInt64
    let sourceRevision: Int?
    let sourceOperation: String?

    init(
        base64: String,
        payloadByteSize: Int,
        rowCount: Int,
        latestItemId: String?,
        payloadFingerprint: String,
        prepareDurationMs: Int,
        contentRevision: UInt64,
        transcriptReadThrough: String?,
        retryRevision: UInt64 = 0,
        sourceRevision: Int?,
        sourceOperation: String?
    ) {
        self.base64 = base64
        self.payloadByteSize = payloadByteSize
        self.rowCount = rowCount
        self.latestItemId = latestItemId
        self.payloadFingerprint = payloadFingerprint
        self.prepareDurationMs = prepareDurationMs
        self.contentRevision = contentRevision
        self.transcriptReadThrough = transcriptReadThrough
        self.retryRevision = retryRevision
        self.sourceRevision = sourceRevision
        self.sourceOperation = sourceOperation
    }
}

struct WebTranscriptRenderReceipt: Equatable, Sendable {
    let contentRevision: UInt64
    let transcriptReadThrough: String?
    let retryRevision: UInt64
    let payloadFingerprint: String
    let latestItemId: String?

    init(
        contentRevision: UInt64,
        transcriptReadThrough: String?,
        retryRevision: UInt64 = 0,
        payloadFingerprint: String,
        latestItemId: String?
    ) {
        self.contentRevision = contentRevision
        self.transcriptReadThrough = transcriptReadThrough
        self.retryRevision = retryRevision
        self.payloadFingerprint = payloadFingerprint
        self.latestItemId = latestItemId
    }
}

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

struct DurableUserInputIdentities {
    let sessionInputIds: Set<Int>
    let clientRequestIds: Set<String>

    func contains(_ input: SubmittedInput) -> Bool {
        if let serverInputId = input.serverInputId,
           sessionInputIds.contains(serverInputId) {
            return true
        }
        return clientRequestIds.contains(input.clientRequestId)
    }
}

struct WebTranscriptPayload: Encodable {
    let errorMessage: String?
    let items: [WebTranscriptPayloadItem]
}

struct WebTranscriptPayloadItem: Encodable {
    let id: String
    let kind: String
    let role: String?
    let title: String?
    let subtitle: String?
    let body: String?
    let fullBody: String?
    let collapsed: Bool
    let status: String?
    let duration: String?
    let input: String?
    let output: String?
    let calls: [WebTranscriptToolCall]
    let origin: String?
    let media: [WebTranscriptMediaRef]?
    /// Bounded attachment metadata for optimistic submitted rows. Full bytes
    /// never cross the WebView boundary.
    var attachments: [SubmittedInputAttachmentSummary]? = nil
    /// Bounded preview shown without a tap on a failed row (R4).
    var failurePreview: String? = nil
    /// Rendered diff for edit rows (R3).
    var diff: [WebTranscriptDiffLine]? = nil
    /// Workers this call spawned, opened in place rather than spliced in.
    var subagents: [WebTranscriptSubagent]? = nil
    /// "22 agents · 4m12s" — the shape of the work while still collapsed.
    var subagentSummary: String? = nil
    /// "Worked for 2m 9s · Turn finished 9:15 AM" under the item a turn ended on.
    var turnEnd: WebTranscriptTurnEnd? = nil
}

struct WebTranscriptTurnEnd: Encodable, Equatable {
    let label: String
    let doneAt: String
}

struct WebTranscriptSubagent: Encodable {
    let sessionId: String
    let label: String
    let toolCalls: Int
}

struct WebTranscriptDiffLine: Encodable {
    let kind: String
    let text: String
}

struct WebTranscriptToolCall: Encodable {
    let title: String
    let subtitle: String
    let status: String
    let input: String?
    let rawInput: String?
    let output: String?
    let media: [WebTranscriptMediaRef]?
}

struct WebTranscriptMediaRef: Encodable {
    let sha256: String
    let url: String?
    let blobUrl: String?
    let mediaState: String
    let mimeType: String?
    let width: Int?
    let height: Int?
}

private extension SessionInputAuthoredVia {
    var payloadValue: String {
        switch self {
        case .longhouse:
            return "longhouse"
        case .terminal:
            return "terminal"
        case .unknown(let value):
            return value
        }
    }
}

#if DEBUG
extension WebTranscriptView {
    /// Test-only accessor for the assembled transcript document: the bundled
    /// resource with the palette spliced in, exactly what the WebView loads.
    static var documentHTMLForTesting: String { documentHTML }
}
#endif

private extension WebTranscriptView {
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

/// Copy for the provider's turn accounting. Pure so row anchoring and duration
/// behavior stay deterministic.
enum TurnEndCopy {
    /// "2m 9s", "58s", "1h 2m": the terminal's own compaction of a duration.
    nonisolated static func duration(milliseconds: Int) -> String {
        let total = max(0, milliseconds) / 1000
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        if hours > 0 { return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h" }
        if minutes > 0 { return seconds > 0 ? "\(minutes)m \(seconds)s" : "\(minutes)m" }
        return "\(seconds)s"
    }

    /// "Turn finished 9:15 AM" today, "Turn finished Tue 9:15 AM" within a
    /// week, else with the date. A stopped turn says "stopped" instead.
    nonisolated static func doneAt(
        _ endedAt: String,
        now: Date = Date(),
        calendar: Calendar = .current,
        verb: String = "Turn finished"
    ) -> String {
        guard let date = LonghouseDateParser.parse(endedAt) else { return verb }
        let time = DateFormatter()
        time.calendar = calendar
        time.dateStyle = .none
        time.timeStyle = .short
        if calendar.isDate(date, inSameDayAs: now) {
            return "\(verb) \(time.string(from: date))"
        }
        let day = DateFormatter()
        day.calendar = calendar
        let withinWeek = now.timeIntervalSince(date) < 7 * 24 * 3600
        day.setLocalizedDateFormatFromTemplate(withinWeek ? "EEE" : "MMM d")
        return "\(verb) \(day.string(from: date)) \(time.string(from: date))"
    }
}
