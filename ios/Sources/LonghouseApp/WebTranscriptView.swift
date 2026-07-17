import Foundation
import SwiftUI
import WebKit
import OSLog

/// Renders the transcript body in WebKit while leaving the session chrome,
/// runtime controls, and composer native.
struct WebTranscriptView: UIViewRepresentable {
    let serverURL: String
    let items: [TimelineItem]
    let submittedInputs: [SubmittedInput]
    let errorMessage: String?
    /// Height (pt) of the floating native control surface below the transcript.
    /// Drives `#root` bottom padding so the last row clears the card. Defaults
    /// to the original 18px hardcoded inset.
    let bottomInset: CGFloat
    let onNearTop: (() -> Void)?
    let onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
    let onLifecycle: ((String) -> Void)?

    init(
        serverURL: String,
        items: [TimelineItem],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        bottomInset: CGFloat = 18,
        onNearTop: (() -> Void)? = nil,
        onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onLifecycle: ((String) -> Void)? = nil
    ) {
        self.serverURL = serverURL
        self.items = items
        self.submittedInputs = submittedInputs
        self.errorMessage = errorMessage
        self.bottomInset = bottomInset
        self.onNearTop = onNearTop
        self.onDiagnostics = onDiagnostics
        self.onLifecycle = onLifecycle
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> WKWebView {
        let pooled = WebTranscriptWebViewPool.takeOrCreate()
        let webView = pooled.webView
        webView.navigationDelegate = context.coordinator
        webView.scrollView.delegate = context.coordinator
        webView.scrollView.keyboardDismissMode = .interactive
        webView.scrollView.alwaysBounceVertical = true
        // Bottom clearance has exactly ONE owner: the `--native-bottom-inset`
        // DOM padding, fed the SwiftUI-computed bottom safe area (card + tab bar
        // + keyboard/home indicator). Disable the scroll view's automatic
        // safe-area inset so it can't double-count or fight the DOM padding — the
        // SwiftUI side takes the WebView full-bleed via
        // .ignoresSafeArea([.container, .keyboard], edges: .bottom).
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.backgroundColor = .clear
        context.coordinator.webView = webView
        context.coordinator.configureMediaAuth(serverURL: serverURL, on: webView)
        let lifecycleStage = pooled.reused ? "webview_reused" : "webview_make"
        Task { @MainActor in
            onLifecycle?(lifecycleStage)
        }
        if pooled.reused {
            // Adopt the warm spare's existing navigation, even if WebKit is
            // still finishing it. Restarting loadHTMLString() here discarded
            // launch prewarm work exactly when the user opened a session early.
            context.coordinator.adoptDocument(serverURL: serverURL, loaded: pooled.isLoaded)
            if pooled.isLoaded {
                Task { @MainActor in
                    onLifecycle?("webview_document_reused")
                }
            }
        } else {
            context.coordinator.loadDocument(serverURL: serverURL, on: webView)
        }
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.configureMediaAuth(serverURL: serverURL, on: webView)
        context.coordinator.ensureDocumentServerURL(serverURL, on: webView)
        context.coordinator.updateBottomInset(bottomInset, on: webView)
        context.coordinator.send(
            preparedPayload(),
            to: webView,
            diagnosticsEnabled: WebTranscriptDiagnosticsFeature.isEnabled,
            onNearTop: onNearTop,
            onDiagnostics: onDiagnostics,
            onLifecycle: onLifecycle
        )
    }

    static func dismantleUIView(_ webView: WKWebView, coordinator: Coordinator) {
        coordinator.prepareForReuse()
        WebTranscriptWebViewPool.recycle(webView)
    }

    private func preparedPayload() -> WebTranscriptPreparedPayload {
        Self.preparedPayload(
            serverURL: serverURL,
            timelineItems: items,
            submittedInputs: submittedInputs,
            errorMessage: errorMessage
        )
    }

    nonisolated static func preparedPayload(
        serverURL: String? = nil,
        timelineItems: [TimelineItem],
        submittedInputs: [SubmittedInput],
        errorMessage: String?
    ) -> WebTranscriptPreparedPayload {
        let startedAt = Date()
        let payload = WebTranscriptPayload(
            errorMessage: errorMessage,
            items: Self.payloadItems(
                serverURL: serverURL,
                timelineItems: timelineItems,
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
            prepareDurationMs: Int(Date().timeIntervalSince(startedAt) * 1000)
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
        submittedInputs: [SubmittedInput]
    ) -> [WebTranscriptPayloadItem] {
        let durableUserInputs = durableUserInputIdentities(timelineItems)
        let visibleSubmittedInputs = submittedInputs.filter { !durableUserInputs.contains($0) }
        guard !visibleSubmittedInputs.isEmpty else {
            return timelineItems.map { payloadItem($0, serverURL: serverURL) }
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
            rows.append(payloadItem(item, serverURL: serverURL))
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

    private nonisolated static func payloadItem(_ item: TimelineItem, serverURL: String?) -> WebTranscriptPayloadItem {
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
        case .action(let action, _):
            return actionPayload(id: item.id, action: action)
        case .tool(let call, let result, _):
            return toolPayload(id: item.id, call: call, result: result, serverURL: serverURL)
        case .orphanTool(let event):
            return toolPayload(id: item.id, call: event, result: event, orphan: true, serverURL: serverURL)
        case .passiveGroup(let calls):
            return passiveGroupPayload(id: item.id, calls: calls, serverURL: serverURL)
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
            media: nil
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
        serverURL: String?
    ) -> WebTranscriptPayloadItem {
        let toolName = call.toolName ?? "Tool"
        if toolName == "AskUserQuestion" {
            return askUserQuestionPayload(id: id, call: call, result: result)
        }
        let resolved = ToolTiers.resolve(toolName)
        let duration = result.flatMap { TimelineBuilder.durationSeconds(call: call, result: $0) }
            .map(TimelineBuilder.formatDuration)
        let status: String? = {
            if orphan { return "orphan" }
            switch call.toolCallState {
            case .running: return "running"
            case .dropped: return "dropped"
            case .completed: return "done"
            case .none: return nil
            }
        }()

        return WebTranscriptPayloadItem(
            id: id,
            kind: "tool",
            role: nil,
            title: resolved.label,
            subtitle: TimelineBuilder.inputSummary(for: call),
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: status,
            duration: duration,
            input: prettyJSON(call.toolInputJSON) ?? TimelineBuilder.inputSummary(for: call),
            output: truncatedOutput(result?.toolOutputText),
            calls: [],
            origin: nil,
            media: payloadMediaRefs(call.mediaRefs + (result?.mediaRefs ?? []), serverURL: serverURL)
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

    private nonisolated static func passiveGroupPayload(
        id: String,
        calls: [PassiveCall],
        serverURL: String?
    ) -> WebTranscriptPayloadItem {
        let summary = TimelineBuilder.explorationSummary(for: calls)

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
                title: ToolTiers.resolve(passive.call.toolName ?? "Tool").label,
                subtitle: TimelineBuilder.inputSummary(for: passive.call),
                status: status,
                input: prettyJSON(passive.call.toolInputJSON) ?? TimelineBuilder.inputSummary(for: passive.call),
                output: truncatedOutput(passive.result?.toolOutputText),
                media: payloadMediaRefs(passive.call.mediaRefs + (passive.result?.mediaRefs ?? []), serverURL: serverURL)
            )
        }

        return WebTranscriptPayloadItem(
            id: id,
            kind: "passiveGroup",
            role: nil,
            title: summary.isEmpty ? "Explored" : summary,
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
            media: nil
        )
    }

    private nonisolated static func payloadMediaRefs(
        _ refs: [SessionEventMediaRef],
        serverURL: String?
    ) -> [WebTranscriptMediaRef]? {
        var seen = Set<String>()
        let media = refs.compactMap { ref -> WebTranscriptMediaRef? in
            let dedupeURL = ref.thumbUrl ?? ref.blobUrl ?? ""
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
                mimeType: ref.mimeType
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

    private nonisolated static func truncatedOutput(_ text: String?) -> String? {
        guard let text, !text.isEmpty else { return nil }
        let maxCharacters = 12_000
        guard text.count > maxCharacters else { return text }
        return String(text.prefix(maxCharacters)) + "\n... truncated in iOS transcript ..."
    }

    private nonisolated static func submittedStatus(_ phase: SubmittedInputPhase, lastError: String?) -> String {
        switch phase {
        case .submitting: return "Sending..."
        case .working: return "Working..."
        case .sent: return "Sent"
        case .queued: return "Queued"
        case .couldNotConfirm: return "Could not confirm"
        case .failed: return lastError ?? "Could not send"
        case .needsUserDecision: return "Needs choice"
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, UIScrollViewDelegate {
        weak var webView: WKWebView?
        private let logger = Logger(subsystem: "ai.longhouse.ios", category: "WebTranscript")
        fileprivate var isLoaded = false
        private var shouldStickToBottom = true
        private var userScrollInProgress = false
        private var dragStartOffsetY: CGFloat?
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
        private var onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
        private var onLifecycle: ((String) -> Void)?
        private var lastNearTopRequestAt = Date.distantPast
        private var documentServerURL: String?
        private var mediaAuthSignature: String?
        /// Last bottom inset (pt) pushed to JS; re-applied on load and only
        /// re-sent when it changes by ≥0.5pt to avoid churn during streaming.
        private var lastBottomInset: CGFloat = 18

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            isLoaded = true
            Task { @MainActor in
                self.onLifecycle?("webview_html_loaded")
            }
            // A pooled/reused webview is already loaded when the inset was first
            // set; a fresh one needs the inset applied now that JS exists.
            applyBottomInset(lastBottomInset, on: webView)
            flushPendingPayload(
                to: webView,
                diagnosticsEnabled: diagnosticsEnabled,
                onDiagnostics: onDiagnostics
            )
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            Task { @MainActor in
                self.onLifecycle?("webview_content_process_terminated")
            }
            isLoaded = false
            jsFailureCount += 1
            pendingPayload = pendingPayload ?? inFlightPayload ?? lastRenderedPayload
            inFlightPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            loadDocument(serverURL: documentServerURL, on: webView)
        }

        func loadDocument(serverURL: String?, on webView: WKWebView) {
            documentServerURL = serverURL
            isLoaded = false
            webView.loadHTMLString(
                WebTranscriptView.documentHTML,
                baseURL: serverURL.flatMap { URL(string: $0) }
            )
        }

        func adoptDocument(serverURL: String, loaded: Bool) {
            documentServerURL = serverURL
            isLoaded = loaded
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
            let cookies = SharedAuthStore.managedCookies(for: serverURL)
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

        func updateBottomInset(_ inset: CGFloat, on webView: WKWebView) {
            guard abs(inset - lastBottomInset) >= 0.5 else { return }
            lastBottomInset = inset
            guard isLoaded else { return }   // applied in didFinish otherwise
            applyBottomInset(inset, on: webView)
            repinToBottomIfSticky(reason: "bottom_inset_changed", followUpDelay: 0.05)
        }

        private func applyBottomInset(_ inset: CGFloat, on webView: WKWebView) {
            let px = Int(inset.rounded())
            webView.evaluateJavaScript("window.setBottomInset && window.setBottomInset(\(px));")
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

        private func repinToBottomIfSticky(reason: String, followUpDelay: TimeInterval? = nil) {
            guard shouldStickToBottom, !userScrollInProgress else { return }
            repinToBottom(reason: reason)
            if let followUpDelay {
                let delay = max(0.05, min(0.5, followUpDelay))
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                    self?.repinToBottom(reason: "\(reason)_settled")
                }
            }
        }

        private func repinToBottom(reason: String) {
            guard let webView, isLoaded, !userScrollInProgress else { return }
            shouldStickToBottom = true
            suppressNearTopUntil = Date().addingTimeInterval(0.75)
            logger.debug("webkit transcript repin reason=\(reason, privacy: .public)")
            webView.evaluateJavaScript("window.scrollTranscriptToBottom && window.scrollTranscriptToBottom();")
        }

        private func emitNearTopIfNeeded(_ scrollView: UIScrollView) {
            guard inFlightPayload == nil else { return }
            guard userScrollInProgress || !shouldStickToBottom else { return }
            guard Date() >= suppressNearTopUntil else { return }
            guard scrollView.contentSize.height > scrollView.bounds.height + 240 else { return }
            guard scrollView.contentOffset.y < 180 else { return }
            let now = Date()
            guard now.timeIntervalSince(lastNearTopRequestAt) > 0.75 else { return }
            lastNearTopRequestAt = now
            onNearTop?()
        }

        func prepareForReuse() {
            webView?.navigationDelegate = nil
            webView?.scrollView.delegate = nil
            webView = nil
            onNearTop = nil
            onDiagnostics = nil
            onLifecycle = nil
            pendingPayload = nil
            inFlightPayload = nil
            lastRenderedPayload = nil
            lastPayload = nil
            lastDuplicatePayload = nil
            userScrollInProgress = false
            dragStartOffsetY = nil
            shouldStickToBottom = true
        }

        func send(
            _ payload: WebTranscriptPreparedPayload,
            to webView: WKWebView,
            diagnosticsEnabled: Bool,
            onNearTop: (() -> Void)?,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?,
            onLifecycle: ((String) -> Void)?
        ) {
            self.webView = webView
            self.diagnosticsEnabled = diagnosticsEnabled
            self.onNearTop = onNearTop
            self.onDiagnostics = onDiagnostics
            self.onLifecycle = onLifecycle
            if payload.base64 == lastPayload
                || payload.base64 == inFlightPayload?.base64
                || payload.base64 == pendingPayload?.base64 {
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

        private func flushPendingPayload(
            to webView: WKWebView,
            diagnosticsEnabled: Bool = WebTranscriptDiagnosticsFeature.isEnabled,
            onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil
        ) {
            guard inFlightPayload == nil else { return }
            guard let payload = pendingPayload else { return }
            pendingPayload = nil
            guard payload.base64 != lastPayload else {
                emitDuplicateDiagnosticsOnce(
                    payload: payload,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                return
            }

            renderSequence += 1
            let sequence = renderSequence
            let stick = shouldStickToBottom && !userScrollInProgress ? "true" : "false"
            inFlightPayload = payload
            let renderStartedAt = Date()
            if shouldStickToBottom && !userScrollInProgress {
                suppressNearTopUntil = renderStartedAt.addingTimeInterval(0.75)
            }
            webView.evaluateJavaScript("window.renderTranscript('\(payload.base64)', \(stick));") { [weak self] _, error in
                guard let self else { return }
                let renderDurationMs = Int(Date().timeIntervalSince(renderStartedAt) * 1000)
                if error == nil {
                    self.lastPayload = payload.base64
                    self.lastRenderedPayload = payload
                } else {
                    self.jsFailureCount += 1
                }
                if stick == "true", self.shouldStickToBottom, !self.userScrollInProgress {
                    self.suppressNearTopUntil = Date().addingTimeInterval(0.75)
                }
                self.inFlightPayload = nil
                self.emitDiagnostics(
                    stage: error == nil ? "rendered" : "failed",
                    payload: payload,
                    sequence: sequence,
                    error: error,
                    renderDurationMs: renderDurationMs,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
                self.flushPendingPayload(
                    to: webView,
                    diagnosticsEnabled: diagnosticsEnabled,
                    onDiagnostics: onDiagnostics
                )
            }
        }

        private func emitDiagnostics(
            stage: String,
            payload: WebTranscriptPreparedPayload,
            sequence: Int,
            error: Error?,
            renderDurationMs: Int? = nil,
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
                render_duration_ms: renderDurationMs,
                error_description: error.map { String(describing: $0) }
            )
            logger.debug(
                "webkit transcript stage=\(stage, privacy: .public) sequence=\(sequence) rows=\(payload.rowCount) bytes=\(payload.payloadByteSize) latest=\(payload.latestItemId ?? "none", privacy: .public) failures=\(self.jsFailureCount) stick=\(self.shouldStickToBottom) prepare_ms=\(payload.prepareDurationMs) render_ms=\(renderDurationMs ?? -1)"
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

struct WebTranscriptPreparedPayload {
    let base64: String
    let payloadByteSize: Int
    let rowCount: Int
    let latestItemId: String?
    let payloadFingerprint: String
    let prepareDurationMs: Int
}

@MainActor
enum WebTranscriptWebViewPool {
    struct PooledWebView {
        let webView: WKWebView
        let reused: Bool
        let isLoaded: Bool
    }

    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "WebTranscript")
    private static let processPool = WKProcessPool()
    private static var warmedWebView: WKWebView?
    private static var warmedWebViewLoaded = false
    private static var prewarmDelegate: WebTranscriptPrewarmDelegate?

    static func prewarm() {
        guard warmedWebView == nil else { return }
        let delegate = WebTranscriptPrewarmDelegate {
            Task { @MainActor in
                warmedWebViewLoaded = true
                logger.info("webkit prewarm loaded")
            }
        }
        let webView = configuredWebView()
        prewarmDelegate = delegate
        webView.navigationDelegate = delegate
        webView.loadHTMLString(WebTranscriptView.documentHTML, baseURL: nil)
        warmedWebView = webView
        warmedWebViewLoaded = false
        logger.info("webkit prewarm started")
    }

    static func takeOrCreate() -> PooledWebView {
        if let webView = warmedWebView {
            warmedWebView = nil
            prewarmDelegate = nil
            let loaded = warmedWebViewLoaded
            warmedWebViewLoaded = false
            logger.info("webkit prewarm reused loaded=\(loaded, privacy: .public)")
            return PooledWebView(webView: webView, reused: true, isLoaded: loaded)
        }
        logger.info("webkit prewarm miss")
        return PooledWebView(webView: configuredWebView(), reused: false, isLoaded: false)
    }

    static func recycle(_ webView: WKWebView) {
        // A just-popped transcript is a better warm spare than a new WebView
        // still starting its content process. Keep one globally bounded spare.
        prewarmDelegate = nil
        warmedWebView = webView
        warmedWebViewLoaded = true
        logger.info("webkit recycled")
    }

    private static func configuredWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.processPool = processPool
        return WKWebView(frame: .zero, configuration: configuration)
    }
}

private final class WebTranscriptPrewarmDelegate: NSObject, WKNavigationDelegate {
    private let onLoaded: () -> Void

    init(onLoaded: @escaping () -> Void) {
        self.onLoaded = onLoaded
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        onLoaded()
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
}

struct WebTranscriptToolCall: Encodable {
    let title: String
    let subtitle: String
    let status: String
    let input: String?
    let output: String?
    let media: [WebTranscriptMediaRef]?
}

struct WebTranscriptMediaRef: Encodable {
    let sha256: String
    let url: String?
    let blobUrl: String?
    let mediaState: String
    let mimeType: String?
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
    /// Test-only accessor for the static transcript document, so the CSS
    /// contract (no chat bubbles, monochrome tokens, demoted tool rows) can be
    /// asserted without a WebView.
    static var documentHTMLForTesting: String { documentHTML }
}
#endif

private extension WebTranscriptView {
    /// Assembled document: the palette's CSS variable block (single source of
    /// truth, TranscriptPalette.swift) spliced into the static template at the
    /// `__LH_ROOT_BLOCK__` marker. Ends the Swift/CSS color double-definition.
    static var documentHTML: String {
        documentTemplate.replacingOccurrences(of: "/* __LH_ROOT_BLOCK__ */", with: TranscriptPalette.cssRootBlock)
    }

    static let documentTemplate = #"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <style>
    /* __LH_ROOT_BLOCK__ */

    * {
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
    }

    html {
      background: var(--page);
    }

    body {
      margin: 0;
      background: var(--page);
      color: var(--text);
      font: -apple-system-body;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
      overflow-wrap: anywhere;
    }

    #root {
      min-height: 100vh;
      /* Bottom padding is driven from native via window.setBottomInset so the
         last transcript row clears the floating control surface. Defaults to
         the original 18px until Swift reports the measured chrome height. */
      padding: 12px 16px var(--native-bottom-inset, 18px);
    }

    .empty, .error {
      min-height: 48vh;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--secondary);
      padding: 24px;
    }

    .error {
      color: #b26b00;
    }

    .row {
      width: 100%;
      margin: 0 0 10px;
    }

    .message {
      max-width: 100%;
      user-select: text;
      -webkit-user-select: text;
    }

    .message.user {
      display: flex;
      justify-content: flex-end;
      padding-left: 48px;
    }

    /* Assistant prose: a plain document paragraph, no card, generous leading. */
    .message.assistant {
      display: block;
      padding: 0;
      background: transparent;
    }

    /* The human message: a quiet neutral capsule. Right-alignment + fill is the
       "this is you" signal — no outline needed, and no borrowed signal color. */
    .bubble {
      display: inline-block;
      max-width: 100%;
      padding: 9px 13px;
      border-radius: 17px;
      background: var(--user);
      white-space: pre-wrap;
    }

    .submitted .bubble {
      background: var(--user-pending);
      box-shadow: inset 0 0 0 1px var(--rule);
    }

    .submitted-status {
      margin-top: 6px;
      color: var(--secondary);
      font-size: 12px;
      font-weight: 600;
      text-align: right;
    }

    .submitted.working .submitted-status::before {
      content: "";
      display: inline-block;
      width: 6px;
      height: 6px;
      margin-right: 6px;
      border-radius: 50%;
      background: var(--accent);
      animation: working-pulse 1.1s ease-in-out infinite;
    }

    @keyframes working-pulse {
      0%, 100% { opacity: 0.35; transform: scale(0.8); }
      50% { opacity: 1; transform: scale(1.15); }
    }

    .action {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--secondary);
      font-size: 12px;
      user-select: text;
      -webkit-user-select: text;
    }

    .action-rule {
      width: 22px;
      height: 1px;
      background: var(--rule);
      flex-shrink: 0;
    }

    .action-title {
      color: var(--primary);
      font-weight: 650;
    }

    .origin {
      margin-top: 6px;
      color: var(--secondary);
      font-size: 11px;
      font-weight: 650;
      text-align: right;
      display: flex;
      gap: 4px;
      align-items: center;
      justify-content: flex-end;
    }

    .message-content {
      line-height: 1.45;
    }

    .media-strip {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(108px, 1fr));
      gap: 8px;
      margin-top: 8px;
      max-width: 100%;
    }

    .message.user .media-strip {
      justify-content: end;
    }

    .media-item {
      display: block;
      min-width: 0;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--rule);
      background: var(--code);
    }

    .media-item img {
      display: block;
      width: 100%;
      max-height: 240px;
      object-fit: contain;
      background: rgba(0, 0, 0, 0.16);
    }

    .media-placeholder {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px dashed var(--rule);
      color: var(--secondary);
      background: var(--code);
      font-size: 12px;
      font-weight: 600;
    }

    p {
      margin: 0 0 0.75em;
    }

    p:last-child {
      margin-bottom: 0;
    }

    h1, h2, h3 {
      margin: 0.35em 0 0.45em;
      line-height: 1.18;
    }

    h1 {
      font-size: 20px;
    }

    h2 {
      font-size: 17px;
    }

    h3 {
      font-size: 15px;
    }

    .table-wrap {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      margin: 0.5em 0 0.75em;
      border-radius: 7px;
      border: 1px solid var(--rule);
    }

    table {
      border-collapse: collapse;
      width: 100%;
      font-size: 0.9em;
    }

    th, td {
      padding: 6px 10px;
      text-align: left;
      border-bottom: 1px solid var(--rule);
      white-space: normal;
      vertical-align: top;
    }

    th {
      font-weight: 650;
      background: var(--code);
      white-space: nowrap;
    }

    tr:last-child td {
      border-bottom: none;
    }

    td[align="center"], th[align="center"] { text-align: center; }
    td[align="right"],  th[align="right"]  { text-align: right; }

    ul {
      margin: 0.25em 0 0.75em 1.25em;
      padding: 0;
    }

    li {
      margin: 0.22em 0;
    }

    code {
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 0.88em;
      background: var(--code);
      border-radius: 5px;
      padding: 1px 4px;
    }

    pre {
      margin: 8px 0 0;
      padding: 9px;
      border-radius: 7px;
      background: var(--code);
      overflow-x: auto;
      white-space: pre;
      -webkit-user-select: text;
      user-select: text;
    }

    pre code {
      background: transparent;
      padding: 0;
      white-space: pre;
    }

    a {
      color: var(--link);
    }

    .expand {
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--link);
      font: inherit;
      font-size: 13px;
      font-weight: 650;
      padding: 8px 0 0;
    }

    /* Tool rows demoted to footnotes: no box, no purple. A faint left rule
       groups them under the turn; they read as quiet metadata, not cards.
       Still fully expandable (preserve, never erase). */
    details.tool, details.passive {
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      margin-left: 2px;
      border-left: 1.5px solid var(--rule);
      overflow: hidden;
    }

    summary {
      min-height: 28px;
      display: flex;
      gap: 8px;
      align-items: center;
      padding: 4px 10px;
      list-style: none;
    }

    summary::-webkit-details-marker {
      display: none;
    }

    .tool-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--secondary);
      white-space: nowrap;
    }

    .tool-subtitle {
      min-width: 0;
      flex: 1;
      color: var(--tertiary);
      font: 13px ui-monospace, "SF Mono", Menlo, monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .tool-meta {
      color: var(--tertiary);
      font-size: 11px;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }

    .tool-meta.running {
      color: var(--secondary);
    }

    /* A dropped or orphaned result is the loud tool signal — attention color.
       Both mean "the result is missing/unexpected", the trust case we never
       want to silently hide. */
    .tool-meta.dropped,
    .tool-meta.orphan {
      color: var(--attention);
      font-style: normal;
    }

    .details-body {
      border-top: 1px solid var(--rule);
      margin: 4px 10px 0;
      padding: 8px 0 8px;
    }

    .question {
      border-left: 2px solid var(--attention);
      padding: 8px 0 8px 10px;
      user-select: text;
      -webkit-user-select: text;
    }

    .question-eyebrow {
      color: var(--attention);
      font-size: 11px;
      font-weight: 750;
      letter-spacing: 0;
      text-transform: uppercase;
    }

    .question-title {
      margin-top: 2px;
      color: var(--primary);
      font-size: 14px;
      font-weight: 700;
      line-height: 1.25;
    }

    .question-subtitle {
      margin-top: 2px;
      color: var(--secondary);
      font-size: 12px;
      font-weight: 600;
      line-height: 1.3;
    }

    .question-body {
      margin-top: 8px;
      color: var(--primary);
      font-size: 14px;
      line-height: 1.42;
      white-space: pre-wrap;
    }

    .question-options {
      display: grid;
      gap: 6px;
      margin-top: 8px;
    }

    .question-option {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 7px 8px;
      border: 1px solid var(--rule);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.025);
      color: var(--secondary);
    }

    .question-option-title {
      color: var(--primary);
      font-size: 13px;
      font-weight: 650;
      line-height: 1.25;
    }

    .question-option-subtitle {
      color: var(--secondary);
      font-size: 12px;
      line-height: 1.3;
    }

    .section-label {
      margin: 10px 0 4px;
      color: var(--secondary);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .section-label:first-child {
      margin-top: 0;
    }

    .passive-call {
      padding: 8px 0;
      border-top: 1px solid var(--rule);
    }

    .passive-call:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .passive-call-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--secondary);
    }

    .passive-call-subtitle {
      color: var(--tertiary);
      font: 13px ui-monospace, "SF Mono", Menlo, monospace;
      margin-top: 2px;
    }
  </style>
</head>
<body>
  <main id="root" aria-live="polite"></main>
  <script>
    let currentItems = [];

    function decodePayload(base64) {
      const binary = atob(base64);
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function inlineMarkdown(value) {
      let html = escapeHtml(value);
      html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
      html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
      return html;
    }

    function paragraphHtml(lines) {
      if (!lines.length) return '';
      return '<p>' + inlineMarkdown(lines.join('\n')).replace(/\n/g, '<br>') + '</p>';
    }

    // Returns true when line looks like a GFM table separator (|---|---|).
    // Uses * (not +) for the inner group so single-column |---| also matches.
    function isTableSeparator(line) {
      return /^\|?[\s\-:]+(\|[\s\-:]+)*\|?$/.test(line.trim());
    }

    // Returns true when line looks like a GFM table data row (starts/ends with |,
    // or contains at least one | surrounded by non-pipe content).
    function isTableRow(line) {
      const t = line.trim();
      return t.startsWith('|') || /\S\|\S/.test(t) || (t.endsWith('|') && t.includes('|'));
    }

    // Split a pipe-delimited row into trimmed cell strings.
    function splitCells(line) {
      const t = line.trim().replace(/^\|/, '').replace(/\|$/, '');
      return t.split('|').map(c => c.trim());
    }

    // Parse alignment hints from a separator row.
    function parseAligns(sepLine) {
      return splitCells(sepLine).map(cell => {
        if (cell.startsWith(':') && cell.endsWith(':')) return 'center';
        if (cell.endsWith(':')) return 'right';
        return '';
      });
    }

    // Render accumulated table rows (first is header, second is separator) to HTML.
    function tableToHtml(rows) {
      // Need at least header + separator; separator must actually look like one.
      if (rows.length < 2 || !isTableSeparator(rows[1])) {
        return rows.map(r => paragraphHtml([r])).join('');
      }
      const aligns = parseAligns(rows[1]);
      const alignAttr = (i) => aligns[i] ? ` align="${aligns[i]}"` : '';

      const header = splitCells(rows[0]);
      let h = '<thead><tr>' + header.map((c, i) =>
        `<th${alignAttr(i)}>${inlineMarkdown(c)}</th>`
      ).join('') + '</tr></thead>';

      let b = '<tbody>';
      for (let ri = 2; ri < rows.length; ri++) {
        const cells = splitCells(rows[ri]);
        b += '<tr>' + header.map((_, i) =>
          `<td${alignAttr(i)}>${inlineMarkdown(cells[i] ?? '')}</td>`
        ).join('') + '</tr>';
      }
      b += '</tbody>';

      return '<div class="table-wrap"><table>' + h + b + '</table></div>';
    }

    function markdownToHtml(value) {
      const lines = String(value ?? '').split(/\r?\n/);
      let html = '';
      let paragraph = [];
      let code = null;
      let tableRows = null;   // null = not in table; array = accumulating rows
      let pendingTableLine = null;  // one-line buffer: potential header row

      function flushParagraph() {
        html += paragraphHtml(paragraph);
        paragraph = [];
      }

      function flushCode() {
        if (code !== null) {
          html += '<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>';
          code = null;
        }
      }

      function flushTable() {
        if (tableRows !== null) {
          html += tableToHtml(tableRows);
          tableRows = null;
        }
      }

      for (const line of lines) {
        const trimmed = line.trim();

        // Code fence — highest priority, swallows everything inside.
        if (trimmed.startsWith('```') || trimmed.startsWith('~~~')) {
          if (code === null) {
            if (pendingTableLine !== null) {
              paragraph.push(pendingTableLine);
              pendingTableLine = null;
            }
            flushParagraph();
            flushTable();
            code = [];
          } else {
            flushCode();
          }
          continue;
        }

        if (code !== null) {
          code.push(line);
          continue;
        }

        // Table accumulation: require header+separator before committing
        // to table mode. Single pipe lines (shell commands, file paths, prose)
        // are buffered for one line and flushed as paragraph text unless a
        // separator immediately follows.
        if (isTableRow(line)) {
          if (tableRows !== null) {
            // Already inside a table — accumulate.
            tableRows.push(line);
            continue;
          }
          // Not yet in a table.
          if (pendingTableLine !== null) {
            // Second pipe line in a row.
            if (isTableSeparator(line)) {
              // Header (buffered) + separator = valid GFM table start.
              flushParagraph();
              tableRows = [pendingTableLine, line];
              pendingTableLine = null;
            } else {
              // Two data rows with no separator — not a table.
              // The buffered line was prose; push it and re-buffer.
              paragraph.push(pendingTableLine);
              pendingTableLine = line;
            }
            continue;
          }
          // First pipe line — buffer as potential header.
          // A separator-only first line is not a valid header; treat as prose.
          if (isTableSeparator(line)) {
            paragraph.push(line);
          } else {
            pendingTableLine = line;
          }
          continue;
        }

        // Non-pipe line: any buffered potential header is prose text.
        if (pendingTableLine !== null) {
          paragraph.push(pendingTableLine);
          pendingTableLine = null;
        }

        // Any non-pipe line breaks an in-progress table.
        if (tableRows !== null) {
          flushTable();
        }

        if (trimmed === '') {
          flushParagraph();
          continue;
        }

        if (trimmed.startsWith('### ')) {
          flushParagraph();
          html += '<h3>' + inlineMarkdown(trimmed.slice(4)) + '</h3>';
          continue;
        }

        if (trimmed.startsWith('## ')) {
          flushParagraph();
          html += '<h2>' + inlineMarkdown(trimmed.slice(3)) + '</h2>';
          continue;
        }

        if (trimmed.startsWith('# ')) {
          flushParagraph();
          html += '<h1>' + inlineMarkdown(trimmed.slice(2)) + '</h1>';
          continue;
        }

        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
          flushParagraph();
          html += '<ul><li>' + inlineMarkdown(trimmed.slice(2)) + '</li></ul>';
          continue;
        }

        paragraph.push(line);
      }

      // Flush any buffered pipe line that was never followed by a separator.
      if (pendingTableLine !== null) {
        paragraph.push(pendingTableLine);
        pendingTableLine = null;
      }

      flushParagraph();
      flushCode();
      flushTable();
      return html;
    }

    function isAtBottom() {
      const doc = document.documentElement;
      return window.innerHeight + window.scrollY >= doc.scrollHeight - 96;
    }

    // Native reports the floating control-surface height so #root padding keeps
    // the last row clear of it. Re-pin to bottom if the user was already there,
    // so growing the inset (attachments, turn-ended, keyboard) never hides the
    // newest content behind the card.
    window.setBottomInset = function(px) {
      const wasAtBottom = isAtBottom();
      const clamped = Math.max(0, Math.min(2000, Number(px) || 0));
      document.documentElement.style.setProperty('--native-bottom-inset', clamped + 'px');
      if (wasAtBottom) {
        requestAnimationFrame(scrollToBottom);
      }
    };

    function scrollToBottom() {
      window.scrollTo(0, document.documentElement.scrollHeight);
      requestAnimationFrame(() => {
        window.scrollTo(0, document.documentElement.scrollHeight);
      });
    }

    window.scrollTranscriptToBottom = scrollToBottom;

    function toolDetails(item) {
      const meta = item.status === 'running' ? 'running'
        : (item.status === 'dropped' ? 'dropped'
        : (item.status === 'orphan' ? 'orphan' : ''));
      const status = item.duration || item.status || '';
      const input = item.input ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(item.input) + '</code></pre>' : '';
      const media = mediaStrip(item.media || []);
      let output = '';
      if (item.output) {
        output = '<div class="section-label">Output</div><pre><code>' + escapeHtml(item.output) + '</code></pre>';
      } else if (item.status === 'running') {
        output = '<div class="section-label">Output</div><p>Running...</p>';
      } else if (item.status === 'dropped') {
        output = '<p>No result recorded, likely dropped during ingest.</p>';
      }
      return `
        <details class="tool row">
          <summary>
            <span class="tool-title">${escapeHtml(item.title || 'Tool')}</span>
            <span class="tool-subtitle">${escapeHtml(item.subtitle || '')}</span>
            <span class="tool-meta ${meta}">${escapeHtml(status)}</span>
          </summary>
          <div class="details-body">${input}${output}${media}</div>
        </details>
      `;
    }

    function passiveGroup(item) {
      const all = item.calls || [];
      const visibleLimit = 8;
      const earlierCount = Math.max(0, all.length - visibleLimit);
      const renderCall = (call) => {
        const input = call.input ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(call.input) + '</code></pre>' : '';
        const output = call.output ? '<div class="section-label">Output</div><pre><code>' + escapeHtml(call.output) + '</code></pre>' : '';
        const media = mediaStrip(call.media || []);
        return `
          <div class="passive-call">
            <div class="passive-call-title">${escapeHtml(call.title || 'Tool')}</div>
            <div class="passive-call-subtitle">${escapeHtml(call.subtitle || '')}</div>
            ${input}${output}${media}
          </div>
        `;
      };
      const earlierHtml = earlierCount > 0
        ? all.slice(0, earlierCount).map(renderCall).join('')
        : '';
      const latestHtml = all.slice(earlierCount).map(renderCall).join('');
      const earlierControl = earlierCount > 0
        ? `<button type="button" class="passive-earlier-btn" onclick="this.nextElementSibling.hidden=false;this.remove();">Show ${earlierCount} earlier</button><div class="passive-earlier" hidden>${earlierHtml}</div>`
        : '';
      return `
        <details class="passive row">
          <summary>
            <span class="tool-title">${escapeHtml(item.title || 'Explored')}</span>
            <span class="tool-subtitle">${escapeHtml(item.subtitle || '')}</span>
          </summary>
          <div class="details-body">${earlierControl}${latestHtml}</div>
        </details>
      `;
    }

    function mediaStrip(media) {
      const items = (media || []).map(ref => {
        if (!ref || !ref.url) {
          const label = ref && ref.mediaState === 'pending' ? 'Media pending' : 'Media unavailable';
          return `<span class="media-placeholder">${escapeHtml(label)}</span>`;
        }
        const href = ref.blobUrl || ref.url;
        const alt = 'Session media ' + String(ref.sha256 || '').slice(0, 12);
        return `
          <a class="media-item" href="${escapeHtml(href)}" target="_blank" rel="noreferrer noopener">
            <img src="${escapeHtml(ref.url)}" alt="${escapeHtml(alt)}" loading="lazy">
          </a>
        `;
      }).join('');
      return items ? `<div class="media-strip">${items}</div>` : '';
    }

    function question(item) {
      const options = (item.calls || []).map(call => `
        <div class="question-option" aria-disabled="true">
          <span class="question-option-title">${escapeHtml(call.title || '')}</span>
          ${call.subtitle ? `<span class="question-option-subtitle">${escapeHtml(call.subtitle)}</span>` : ''}
        </div>
      `).join('');
      return `
        <article class="row question">
          <div class="question-eyebrow">Needs answer</div>
          <div class="question-title">${escapeHtml(item.title || 'Question')}</div>
          <div class="question-subtitle">${escapeHtml(item.subtitle || 'Answer in terminal')}</div>
          <div class="question-body">${escapeHtml(item.body || 'Claude is waiting for your answer.')}</div>
          ${options ? `<div class="question-options" aria-label="Answer options">${options}</div>` : ''}
        </article>
      `;
    }

    function message(item, index) {
      const body = item.role === 'assistant'
        ? markdownToHtml(item.body || '')
        : escapeHtml(item.body || '');
      const expand = item.collapsed
        ? `<button class="expand" data-expand-index="${index}">Show full message</button>`
        : '';
      const media = mediaStrip(item.media || []);
      if (item.role === 'user') {
        const origin = item.origin === 'longhouse'
          ? '<div id="session-chat-input-origin-longhouse" class="origin" aria-label="Sent via Longhouse">Longhouse</div>'
          : '';
        return `
          <div class="row message user">
            <div>
              <div class="bubble">${body}</div>
              ${media}
              ${expand}
              ${origin}
            </div>
          </div>
        `;
      }
      return `
        <article class="row message assistant" data-message-index="${index}">
          <div class="message-content">${body}</div>
          ${media}
          ${expand}
        </article>
      `;
    }

    function submitted(item) {
      return `
        <div class="row message user submitted ${escapeHtml(item.status || '')}">
          <div>
            <div class="bubble">${escapeHtml(item.body || '')}</div>
            <div class="submitted-status">${escapeHtml(item.subtitle || '')}</div>
          </div>
        </div>
      `;
    }

    function action(item) {
      const subtitle = item.subtitle ? `<span>${escapeHtml(item.subtitle)}</span>` : '';
      return `
        <div class="row action">
          <span class="action-rule"></span>
          <span class="action-title">${escapeHtml(item.title || 'Session action')}</span>
          ${subtitle}
        </div>
      `;
    }

    function renderItem(item, index) {
      if (item.kind === 'message') return message(item, index);
      if (item.kind === 'submitted') return submitted(item);
      if (item.kind === 'action') return action(item);
      if (item.kind === 'question') return question(item);
      if (item.kind === 'tool') return toolDetails(item);
      if (item.kind === 'passiveGroup') return passiveGroup(item);
      return '';
    }

    function attachExpandHandlers() {
      for (const button of document.querySelectorAll('[data-expand-index]')) {
        button.addEventListener('click', () => {
          const index = Number(button.getAttribute('data-expand-index'));
          const item = currentItems[index];
          if (!item || !item.fullBody) return;
          const article = button.closest('[data-message-index]');
          if (article) {
            const content = article.querySelector('.message-content');
            content.innerHTML = markdownToHtml(item.fullBody);
          } else {
            const bubble = button.parentElement.querySelector('.bubble');
            bubble.textContent = item.fullBody;
          }
          button.remove();
        });
      }
    }

    window.renderTranscript = function(base64, shouldStickToBottom) {
      const wasAtBottom = shouldStickToBottom || isAtBottom();
      const previousItems = currentItems;
      const previousFirstId = previousItems.length > 0 ? previousItems[0].id : null;
      const previousScrollHeight = document.documentElement.scrollHeight;
      const previousScrollY = window.scrollY;
      const payload = decodePayload(base64);
      currentItems = payload.items || [];
      const newFirstId = currentItems.length > 0 ? currentItems[0].id : null;
      const prepended = previousFirstId && newFirstId && previousFirstId !== newFirstId
        && currentItems.some(item => item.id === previousFirstId);
      const root = document.getElementById('root');
      if (payload.errorMessage && currentItems.length === 0) {
        root.innerHTML = `<div class="error">${escapeHtml(payload.errorMessage)}</div>`;
      } else if (currentItems.length === 0) {
        root.innerHTML = '<div class="empty">No messages yet</div>';
      } else {
        const error = payload.errorMessage
          ? `<div class="error row">${escapeHtml(payload.errorMessage)}</div>`
          : '';
        root.innerHTML = error + currentItems.map(renderItem).join('');
        attachExpandHandlers();
      }
      if (wasAtBottom) scrollToBottom();
      else if (prepended) {
        const delta = document.documentElement.scrollHeight - previousScrollHeight;
        window.scrollTo(0, previousScrollY + delta);
      }
    };
  </script>
</body>
</html>
"""#
}
