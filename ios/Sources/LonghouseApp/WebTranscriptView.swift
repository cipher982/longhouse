import SwiftUI
import WebKit
import OSLog

/// Renders the transcript body in WebKit while leaving the session chrome,
/// runtime controls, and composer native.
struct WebTranscriptView: UIViewRepresentable {
    let items: [TimelineItem]
    let submittedInputs: [SubmittedInput]
    let sessionEnded: Bool
    let errorMessage: String?
    let onNearTop: (() -> Void)?
    let onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
    let onLifecycle: ((String) -> Void)?

    init(
        items: [TimelineItem],
        submittedInputs: [SubmittedInput],
        sessionEnded: Bool,
        errorMessage: String?,
        onNearTop: (() -> Void)? = nil,
        onDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onLifecycle: ((String) -> Void)? = nil
    ) {
        self.items = items
        self.submittedInputs = submittedInputs
        self.sessionEnded = sessionEnded
        self.errorMessage = errorMessage
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
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.backgroundColor = .clear
        context.coordinator.webView = webView
        context.coordinator.isLoaded = pooled.isLoaded
        let lifecycleStage = pooled.reused ? "webview_reused" : "webview_make"
        Task { @MainActor in
            onLifecycle?(lifecycleStage)
            if pooled.reused && pooled.isLoaded {
                onLifecycle?("webview_html_loaded")
            }
        }
        if !pooled.isLoaded {
            webView.loadHTMLString(Self.documentHTML, baseURL: nil)
        }
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.send(
            preparedPayload(),
            to: webView,
            diagnosticsEnabled: WebTranscriptDiagnosticsFeature.isEnabled,
            onNearTop: onNearTop,
            onDiagnostics: onDiagnostics,
            onLifecycle: onLifecycle
        )
    }

    private func preparedPayload() -> WebTranscriptPreparedPayload {
        Self.preparedPayload(
            timelineItems: items,
            submittedInputs: submittedInputs,
            sessionEnded: sessionEnded,
            errorMessage: errorMessage
        )
    }

    nonisolated static func preparedPayload(
        timelineItems: [TimelineItem],
        submittedInputs: [SubmittedInput],
        sessionEnded: Bool,
        errorMessage: String?
    ) -> WebTranscriptPreparedPayload {
        let payload = WebTranscriptPayload(
            errorMessage: errorMessage,
            items: Self.payloadItems(
                timelineItems: timelineItems,
                submittedInputs: submittedInputs,
                sessionEnded: sessionEnded
            )
        )
        let encoder = JSONEncoder()
        let data = (try? encoder.encode(payload)) ?? Data()
        return WebTranscriptPreparedPayload(
            base64: data.base64EncodedString(),
            payloadByteSize: data.count,
            rowCount: payload.items.count,
            latestItemId: payload.items.last?.id
        )
    }

    nonisolated static func payloadItems(
        timelineItems: [TimelineItem],
        submittedInputs: [SubmittedInput],
        sessionEnded: Bool
    ) -> [WebTranscriptPayloadItem] {
        var rows = timelineItems.map { item in
            payloadItem(item, sessionEnded: sessionEnded)
        }
        let durableUserInputs = durableUserInputIdentities(timelineItems)
        rows.append(contentsOf: submittedInputs
            .filter { !durableUserInputs.contains($0) }
            .map(payloadSubmittedInput)
        )
        return rows
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

    private nonisolated static func payloadItem(_ item: TimelineItem, sessionEnded: Bool) -> WebTranscriptPayloadItem {
        switch item {
        case .user(let event):
            return messagePayload(id: item.id, role: "user", text: event.contentText ?? "", origin: event.inputOrigin?.authoredVia)
        case .assistant(let event):
            return messagePayload(id: item.id, role: "assistant", text: event.contentText ?? "", origin: nil)
        case .tool(let call, let result, _):
            return toolPayload(id: item.id, call: call, result: result, sessionEnded: sessionEnded)
        case .orphanTool(let event):
            return toolPayload(id: item.id, call: event, result: event, sessionEnded: sessionEnded, orphan: true)
        case .passiveGroup(let calls):
            return passiveGroupPayload(id: item.id, calls: calls, sessionEnded: sessionEnded)
        }
    }

    private nonisolated static func messagePayload(
        id: String,
        role: String,
        text: String,
        origin: SessionInputAuthoredVia?
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
            origin: origin?.payloadValue
        )
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
            origin: nil
        )
    }

    private nonisolated static func toolPayload(
        id: String,
        call: SessionEvent,
        result: SessionEvent?,
        sessionEnded: Bool,
        orphan: Bool = false
    ) -> WebTranscriptPayloadItem {
        let toolName = call.toolName ?? "Tool"
        let resolved = ToolTiers.resolve(toolName)
        let pending = result == nil && !orphan && !TimelineBuilder.isDropped(call: call, sessionEnded: sessionEnded)
        let dropped = result == nil && !orphan && TimelineBuilder.isDropped(call: call, sessionEnded: sessionEnded)
        let duration = result.flatMap { TimelineBuilder.durationSeconds(call: call, result: $0) }
            .map(TimelineBuilder.formatDuration)
        let status: String? = {
            if orphan { return "orphan" }
            if pending { return "running" }
            if dropped { return "dropped" }
            return "done"
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
            origin: nil
        )
    }

    private nonisolated static func passiveGroupPayload(
        id: String,
        calls: [PassiveCall],
        sessionEnded: Bool
    ) -> WebTranscriptPayloadItem {
        var counts: [(String, Int)] = []
        for passive in calls {
            let name = passive.call.toolName ?? "Tool"
            if let index = counts.firstIndex(where: { $0.0 == name }) {
                counts[index].1 += 1
            } else {
                counts.append((name, 1))
            }
        }

        let childCalls = calls.map { passive in
            WebTranscriptToolCall(
                title: ToolTiers.resolve(passive.call.toolName ?? "Tool").label,
                subtitle: TimelineBuilder.inputSummary(for: passive.call),
                status: passive.result == nil && TimelineBuilder.isDropped(call: passive.call, sessionEnded: sessionEnded)
                    ? "dropped"
                    : (passive.result == nil ? "running" : "done"),
                input: prettyJSON(passive.call.toolInputJSON) ?? TimelineBuilder.inputSummary(for: passive.call),
                output: truncatedOutput(passive.result?.toolOutputText)
            )
        }

        return WebTranscriptPayloadItem(
            id: id,
            kind: "passiveGroup",
            role: nil,
            title: "Explored",
            subtitle: counts.map { "\($0.0) × \($0.1)" }.joined(separator: ", "),
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: childCalls,
            origin: nil
        )
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
        case .sent: return "Sent"
        case .queued: return "Queued"
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

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            isLoaded = true
            Task { @MainActor in
                self.onLifecycle?("webview_html_loaded")
            }
            flushPendingPayload(
                to: webView,
                diagnosticsEnabled: diagnosticsEnabled,
                onDiagnostics: onDiagnostics
            )
        }

        func scrollViewDidScroll(_ scrollView: UIScrollView) {
            emitNearTopIfNeeded(scrollView)
            guard !userScrollInProgress else { return }
            updateStickiness(scrollView)
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
            guard scrollView.contentSize.height > scrollView.bounds.height + 240 else { return }
            guard scrollView.contentOffset.y < 180 else { return }
            let now = Date()
            guard now.timeIntervalSince(lastNearTopRequestAt) > 0.75 else { return }
            lastNearTopRequestAt = now
            onNearTop?()
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
                render_sequence: sequence,
                js_failure_count: jsFailureCount,
                should_stick_to_bottom: shouldStickToBottom,
                web_view_loaded: isLoaded,
                render_duration_ms: renderDurationMs,
                error_description: error.map { String(describing: $0) }
            )
            logger.debug(
                "webkit transcript stage=\(stage, privacy: .public) sequence=\(sequence) rows=\(payload.rowCount) bytes=\(payload.payloadByteSize) latest=\(payload.latestItemId ?? "none", privacy: .public) failures=\(self.jsFailureCount) stick=\(self.shouldStickToBottom) render_ms=\(renderDurationMs ?? -1)"
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

struct WebTranscriptPreparedPayload: Equatable {
    let base64: String
    let payloadByteSize: Int
    let rowCount: Int
    let latestItemId: String?
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
        // Single-shot warm spare: active transcript web views are not returned
        // to this pool, so a reused view should contain only the empty document.
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
}

struct WebTranscriptToolCall: Encodable {
    let title: String
    let subtitle: String
    let status: String
    let input: String?
    let output: String?
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

private extension WebTranscriptView {
    static let documentHTML = #"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <style>
    :root {
      color-scheme: light dark;
      --page: #f2f2f7;
      --text: #111114;
      --secondary: rgba(60, 60, 67, 0.68);
      --tertiary: rgba(60, 60, 67, 0.38);
      --assistant: rgba(255, 255, 255, 0.86);
      --tool: rgba(120, 82, 180, 0.08);
      --tool-border: rgba(120, 82, 180, 0.16);
      --user: rgba(0, 122, 255, 0.15);
      --user-pending: rgba(0, 122, 255, 0.10);
      --code: rgba(118, 118, 128, 0.12);
      --link: #006edb;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --page: #000000;
        --text: #f5f5f7;
        --secondary: rgba(235, 235, 245, 0.62);
        --tertiary: rgba(235, 235, 245, 0.34);
        --assistant: rgba(28, 28, 30, 0.92);
        --tool: rgba(167, 139, 250, 0.13);
        --tool-border: rgba(167, 139, 250, 0.24);
        --user: rgba(10, 132, 255, 0.28);
        --user-pending: rgba(10, 132, 255, 0.18);
        --code: rgba(118, 118, 128, 0.24);
        --link: #65a7ff;
      }
    }

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
      padding: 12px 16px 18px;
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
      padding-left: 40px;
    }

    .message.assistant {
      display: block;
      padding: 10px;
      border-radius: 10px;
      background: var(--assistant);
    }

    .bubble {
      display: inline-block;
      max-width: 100%;
      padding: 10px;
      border-radius: 12px;
      background: var(--user);
      white-space: pre-wrap;
    }

    .submitted .bubble {
      background: var(--user-pending);
      box-shadow: inset 0 0 0 1px rgba(0, 122, 255, 0.20);
    }

    .submitted-status {
      margin-top: 6px;
      color: var(--secondary);
      font-size: 12px;
      font-weight: 600;
      text-align: right;
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
      line-height: 1.36;
    }

    p {
      margin: 0 0 0.75em;
    }

    p:last-child {
      margin-bottom: 0;
    }

    h1, h2 {
      margin: 0.35em 0 0.45em;
      line-height: 1.18;
    }

    h1 {
      font-size: 20px;
    }

    h2 {
      font-size: 17px;
    }

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

    details.tool, details.passive {
      border-radius: 8px;
      background: var(--tool);
      box-shadow: inset 0 0 0 1px var(--tool-border);
      overflow: hidden;
    }

    summary {
      min-height: 36px;
      display: flex;
      gap: 8px;
      align-items: center;
      padding: 8px 10px;
      list-style: none;
    }

    summary::-webkit-details-marker {
      display: none;
    }

    .tool-title {
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }

    .tool-subtitle {
      min-width: 0;
      flex: 1;
      color: var(--secondary);
      font: 12px ui-monospace, "SF Mono", Menlo, monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .tool-meta {
      color: var(--tertiary);
      font-size: 11px;
      white-space: nowrap;
    }

    .tool-meta.running {
      color: var(--secondary);
    }

    .tool-meta.dropped {
      font-style: italic;
    }

    .details-body {
      border-top: 1px solid var(--tool-border);
      padding: 9px 10px 10px;
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
      border-top: 1px solid var(--tool-border);
    }

    .passive-call:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .passive-call-title {
      font-size: 12px;
      font-weight: 700;
    }

    .passive-call-subtitle {
      color: var(--secondary);
      font: 12px ui-monospace, "SF Mono", Menlo, monospace;
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

    function markdownToHtml(value) {
      const lines = String(value ?? '').split(/\r?\n/);
      let html = '';
      let paragraph = [];
      let code = null;

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

      for (const line of lines) {
        const trimmed = line.trim();

        if (trimmed.startsWith('```') || trimmed.startsWith('~~~')) {
          if (code === null) {
            flushParagraph();
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

        if (trimmed === '') {
          flushParagraph();
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

      flushParagraph();
      flushCode();
      return html;
    }

    function isAtBottom() {
      const doc = document.documentElement;
      return window.innerHeight + window.scrollY >= doc.scrollHeight - 96;
    }

    function scrollToBottom() {
      window.scrollTo(0, document.documentElement.scrollHeight);
      requestAnimationFrame(() => {
        window.scrollTo(0, document.documentElement.scrollHeight);
      });
    }

    function toolDetails(item) {
      const meta = item.status === 'running' ? 'running' : (item.status === 'dropped' ? 'dropped' : '');
      const status = item.duration || item.status || '';
      const input = item.input ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(item.input) + '</code></pre>' : '';
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
          <div class="details-body">${input}${output}</div>
        </details>
      `;
    }

    function passiveGroup(item) {
      const calls = (item.calls || []).map(call => {
        const input = call.input ? '<div class="section-label">Input</div><pre><code>' + escapeHtml(call.input) + '</code></pre>' : '';
        const output = call.output ? '<div class="section-label">Output</div><pre><code>' + escapeHtml(call.output) + '</code></pre>' : '';
        return `
          <div class="passive-call">
            <div class="passive-call-title">${escapeHtml(call.title || 'Tool')}</div>
            <div class="passive-call-subtitle">${escapeHtml(call.subtitle || '')}</div>
            ${input}${output}
          </div>
        `;
      }).join('');
      return `
        <details class="passive row">
          <summary>
            <span class="tool-title">${escapeHtml(item.title || 'Explored')}</span>
            <span class="tool-subtitle">${escapeHtml(item.subtitle || '')}</span>
          </summary>
          <div class="details-body">${calls}</div>
        </details>
      `;
    }

    function message(item, index) {
      const body = item.role === 'assistant'
        ? markdownToHtml(item.body || '')
        : escapeHtml(item.body || '');
      const expand = item.collapsed
        ? `<button class="expand" data-expand-index="${index}">Show full message</button>`
        : '';
      if (item.role === 'user') {
        const origin = item.origin === 'longhouse'
          ? '<div id="session-chat-input-origin-longhouse" class="origin" aria-label="Sent via Longhouse">Longhouse</div>'
          : '';
        return `
          <div class="row message user">
            <div>
              <div class="bubble">${body}</div>
              ${expand}
              ${origin}
            </div>
          </div>
        `;
      }
      return `
        <article class="row message assistant" data-message-index="${index}">
          <div class="message-content">${body}</div>
          ${expand}
        </article>
      `;
    }

    function submitted(item) {
      return `
        <div class="row message user submitted">
          <div>
            <div class="bubble">${escapeHtml(item.body || '')}</div>
            <div class="submitted-status">${escapeHtml(item.subtitle || '')}</div>
          </div>
        </div>
      `;
    }

    function renderItem(item, index) {
      if (item.kind === 'message') return message(item, index);
      if (item.kind === 'submitted') return submitted(item);
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
