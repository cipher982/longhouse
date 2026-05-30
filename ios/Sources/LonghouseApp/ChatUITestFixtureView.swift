#if DEBUG
import SwiftUI

@MainActor
struct ChatUITestFixtureView: View {
    @EnvironmentObject private var appState: AppState
    private let fixtureName: String
    private let client: ChatUITestWorkspaceClient
    @StateObject private var viewModel: SessionViewModel
    @StateObject private var probe: ChatUITestProbe
    @State private var invalidationTick = 0

    init(fixtureName: String) {
        let fixture = ChatUITestFixture(name: fixtureName)
        let client = ChatUITestWorkspaceClient(fixture: fixture)
        self.fixtureName = fixtureName
        self.client = client
        _probe = StateObject(wrappedValue: ChatUITestProbe(path: UITestHooks.chatFixtureProbePath))
        _viewModel = StateObject(
            wrappedValue: SessionViewModel(
                apiFactory: { _ in client },
                streamFactory: { _, _, _ in client.streamSource() },
                enableRealtime: fixture.usesRealtimeStream
            )
        )
    }

    var body: some View {
        NavigationStack {
            SessionView(
                sessionId: client.sessionID,
                fallbackTitle: "Chat UI Fixture",
                viewModel: viewModel,
                onTranscriptDiagnostics: { diagnostics in
                    Task { @MainActor in
                        probe.record(diagnostics)
                    }
                }
            )
        }
        .task(id: fixtureName) {
            if fixtureName == "render-storm" || fixtureName == "replay-file" {
                await waitForInitialWorkspaceLoad()
                for tick in 1...40 {
                    invalidationTick = tick
                    probe.recordTick(tick)
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                await waitForStressTrigger()
                await client.appendAssistantMessage("Assistant fixture stress update after user scroll.")
                await viewModel.reload(sessionId: client.sessionID, appState: appState)
                return
            }
            guard fixtureName.hasPrefix("assistant-update") || fixtureName.hasPrefix("assistant-stream") else { return }
            await waitForInitialWorkspaceLoad()

            if fixtureName.hasPrefix("assistant-stream") {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await client.streamAssistantMessage(
                    chunks: [
                        "Assistant fixture streaming",
                        "Assistant fixture streaming update",
                        "Assistant fixture streaming update at bottom.",
                    ],
                    intervalNanoseconds: 250_000_000
                )
                return
            }

            let delay: UInt64 = fixtureName == "assistant-update-keyboard"
                ? 2_500_000_000
                : 900_000_000
            let message = fixtureName == "assistant-update-keyboard"
                ? "Assistant fixture keyboard update at bottom."
                : "Assistant fixture live update at bottom."
            try? await Task.sleep(nanoseconds: delay)
            await client.appendAssistantMessage(message)
            await viewModel.reload(sessionId: client.sessionID, appState: appState)
        }
    }

    private func waitForInitialWorkspaceLoad() async {
        while !Task.isCancelled && viewModel.detail == nil {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }

    private func waitForStressTrigger() async {
        guard let triggerPath = UITestHooks.chatFixtureTriggerPath else { return }
        while !Task.isCancelled && !FileManager.default.fileExists(atPath: triggerPath) {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }
}

@MainActor
struct TimelineOpenUITestFixtureView: View {
    @StateObject private var probe = ChatUITestProbe(path: UITestHooks.chatFixtureProbePath)

    private let sessions: [TimelineOpenFixtureSession]

    init() {
        sessions = (1...3).map { index in
            TimelineOpenFixtureSession(
                id: "ui-test-timeline-session-\(index)",
                title: "Timeline open fixture \(index)",
                fixture: ChatUITestFixture(name: index == 1 ? "stress" : "basic")
            )
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(sessions) { session in
                        NavigationLink {
                            destination(for: session)
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(session.title)
                                    .font(.headline.weight(.semibold))
                                Text("Fixture transcript")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(14)
                            .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("timeline-open-session-\(session.index)")
                    }
                }
                .padding(16)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Timeline")
            .task {
                WebTranscriptWebViewPool.prewarm()
            }
            .onAppear {
                WebTranscriptWebViewPool.prewarm()
            }
        }
    }

    private func destination(for session: TimelineOpenFixtureSession) -> some View {
        let client = ChatUITestWorkspaceClient(fixture: session.fixture, sessionID: session.id)
        let viewModel = SessionViewModel(
            apiFactory: { _ in client },
            streamFactory: { _, _, _ in client.streamSource() },
            enableRealtime: false
        )
        return SessionView(
            sessionId: session.id,
            fallbackTitle: session.title,
            viewModel: viewModel,
            onTranscriptDiagnostics: { diagnostics in
                Task { @MainActor in
                    probe.record(diagnostics)
                }
            }
        )
    }
}

private struct TimelineOpenFixtureSession: Identifiable {
    let id: String
    let title: String
    let fixture: ChatUITestFixture

    var index: Int {
        Int(id.split(separator: "-").last ?? "0") ?? 0
    }
}

@MainActor
private final class ChatUITestProbe: ObservableObject {
    private(set) var statusLine = "renders=0 duplicates=0 repeats=0 rows=0 bytes=0 latest=none stage=none stick=0 render_ms=0 max_render_ms=0 tick=0"

    private let path: String?
    private var renderCount = 0
    private var duplicateCount = 0
    private var repeatRenderCount = 0
    private var lastRenderedKey: String?
    private var maxRenderDurationMs = 0
    private var tick = 0

    init(path: String?) {
        self.path = path
        persist()
    }

    func record(_ diagnostics: RenderBeaconReporter.WebKitDiagnostics) {
        let latest = diagnostics.latest_item_id ?? "none"
        let key = "\(diagnostics.row_count)|\(diagnostics.payload_byte_size)|\(latest)"
        if diagnostics.stage == "rendered" {
            if key == lastRenderedKey {
                repeatRenderCount += 1
            }
            lastRenderedKey = key
            renderCount += 1
            maxRenderDurationMs = max(maxRenderDurationMs, diagnostics.render_duration_ms ?? 0)
        } else if diagnostics.stage == "duplicate" {
            duplicateCount += 1
        }

        statusLine = [
            "renders=\(renderCount)",
            "duplicates=\(duplicateCount)",
            "repeats=\(repeatRenderCount)",
            "rows=\(diagnostics.row_count)",
            "bytes=\(diagnostics.payload_byte_size)",
            "latest=\(latest)",
            "stage=\(diagnostics.stage)",
            "stick=\(diagnostics.should_stick_to_bottom ? 1 : 0)",
            "render_ms=\(diagnostics.render_duration_ms ?? 0)",
            "max_render_ms=\(maxRenderDurationMs)",
            "tick=\(tick)",
        ].joined(separator: " ")
        persist()
    }

    func recordTick(_ tick: Int) {
        self.tick = tick
        statusLine = statusLine
            .split(separator: " ")
            .map { token in token.hasPrefix("tick=") ? "tick=\(tick)" : String(token) }
            .joined(separator: " ")
        persist()
    }

    private func persist() {
        guard let path else { return }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? statusLine.write(to: url, atomically: true, encoding: .utf8)
    }
}

private struct ChatUITestFixture: Sendable {
    let name: String
    let eventCount: Int
    let replayPath: String?

    init(name: String) {
        self.name = name
        replayPath = name == "replay-file" ? UITestHooks.chatFixtureReplayPath : nil
        eventCount = max(0, UITestHooks.chatFixtureEventCount ?? (name == "stress" ? 500 : 80))
    }

    var usesRealtimeStream: Bool {
        name.hasPrefix("assistant-stream")
    }
}

private actor ChatUITestWorkspaceClient: SessionWorkspaceClient {
    let sessionID: String
    private var nextEventID = 1
    private var events: [SessionEvent]
    private var realtimeContinuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    private var streamingAssistantEventID: Int?

    init(fixture: ChatUITestFixture, sessionID: String = "ui-test-chat-session") {
        self.sessionID = sessionID
        var seedEvents: [SessionEvent] = []
        if let replayPath = fixture.replayPath,
           let replayEvents = Self.loadReplayEvents(path: replayPath) {
            seedEvents = replayEvents
        } else if fixture.name == "tools" {
            seedEvents = Self.toolFixtureEvents()
        } else {
            for index in 0..<fixture.eventCount {
                let role = index.isMultiple(of: 2) ? "user" : "assistant"
                seedEvents.append(Self.makeEvent(
                    id: index + 1,
                    role: role,
                    content: Self.messageText(index: index, role: role, fixtureName: fixture.name),
                    timestamp: Self.fixedTimestamp(offset: index)
                ))
            }
        }
        events = seedEvents
        nextEventID = (seedEvents.map(\.id).max() ?? 0) + 1
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        Self.makeWorkspace(sessionID: sessionID, events: events)
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse {
        if let delayMs = UITestHooks.mobileTailDelayMs, delayMs > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
        }
        let page = Self.tailPage(events: events, limit: limit, offset: offset)
        return Self.makeMobileTail(
            sessionID: sessionID,
            events: page.events,
            total: events.count,
            pageOffset: page.pageOffset,
            snapshotEventId: events.map(\.id).max()
        )
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse {
        try await Task.sleep(nanoseconds: 650_000_000)
        let inputID = nextEventID
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "user",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date()),
            inputOrigin: SessionInputOrigin(
                authoredVia: .longhouse,
                sessionInputId: inputID,
                clientRequestId: clientRequestId
            )
        ))
        nextEventID += 1
        return SessionInputResponse(
            outcome: .sent,
            inputId: inputID,
            clientRequestId: clientRequestId,
            intent: SessionInputIntent(rawValue: intent) ?? .auto,
            queued: []
        )
    }

    func sendInputMultipart(id: String, text: String, attachments: [ComposerAttachment], clientRequestId: String?) async throws -> SessionInputResponse {
        try await sendInput(id: id, text: text, intent: "auto", clientRequestId: clientRequestId)
    }

    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse {
        DraftReplyResponse(
            draftText: "Drafted fixture reply",
            model: "ui-test",
            generatedAt: ISO8601DateFormatter().string(from: Date()),
            basedOnEventIds: []
        )
    }

    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse {
        LoopModeResponse(sessionId: id, loopMode: loopMode)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}

    nonisolated func streamSource() -> SessionWorkspaceStreamSource {
        SessionWorkspaceStreamSource(
            start: { self.startRealtimeStream() },
            stop: { await self.stopRealtimeStream() },
            clockSkewMs: { 0 }
        )
    }

    nonisolated func startRealtimeStream() -> AsyncStream<SessionWorkspaceStream.Event> {
        AsyncStream { continuation in
            Task {
                await self.attachRealtimeContinuation(continuation)
            }
        }
    }

    nonisolated func stopRealtimeStream() async {
        await finishRealtimeStream()
    }

    private func finishRealtimeStream() {
        realtimeContinuation?.finish()
        realtimeContinuation = nil
    }

    func appendAssistantMessage(_ text: String) {
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "assistant",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
    }

    func streamAssistantMessage(chunks: [String], intervalNanoseconds: UInt64) async {
        for chunk in chunks {
            upsertStreamingAssistantMessage(chunk)
            emitWorkspaceChanged()
            try? await Task.sleep(nanoseconds: intervalNanoseconds)
        }
    }

    private func attachRealtimeContinuation(
        _ continuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation
    ) {
        realtimeContinuation?.finish()
        realtimeContinuation = continuation
        continuation.yield(.connected(SessionWorkspaceStream.Connected(
            session_id: sessionID,
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000)
        )))
        continuation.onTermination = { [weak self] _ in
            Task { await self?.clearRealtimeContinuation() }
        }
    }

    private func clearRealtimeContinuation() {
        realtimeContinuation = nil
    }

    private func upsertStreamingAssistantMessage(_ text: String) {
        if let eventID = streamingAssistantEventID,
           let index = events.firstIndex(where: { $0.id == eventID }) {
            events[index] = Self.makeEvent(
                id: eventID,
                role: "assistant",
                content: text,
                timestamp: ISO8601DateFormatter().string(from: Date())
            )
            return
        }

        let eventID = nextEventID
        streamingAssistantEventID = eventID
        events.append(Self.makeEvent(
            id: eventID,
            role: "assistant",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
    }

    private func emitWorkspaceChanged() {
        let latestID = events.last?.id ?? 0
        realtimeContinuation?.yield(.changed(SessionWorkspaceStream.WorkspaceChanged(
            session_id: sessionID,
            latest_event_id: latestID,
            thread_session_count: 1,
            latest_event_emitted_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_fanout_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000),
            pubsub_seq: latestID,
            transcript_preview: nil
        )))
    }

    private static func makeWorkspace(sessionID: String, events: [SessionEvent]) -> SessionWorkspaceResponse {
        let detail = makeDetail(sessionID: sessionID, events: events)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
                timestamp: event.timestamp,
                event: event,
                continuedFromSessionId: nil,
                continuationKind: nil,
                originLabel: nil,
                parentOriginLabel: nil,
                parentContinuationKind: nil,
                branchedFromEventId: nil
            )
        }
        return SessionWorkspaceResponse(
            session: detail,
            thread: SessionThreadResponse(
                rootSessionId: sessionID,
                headSessionId: sessionID,
                sessions: [detail]
            ),
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: projectionItems.count,
                pageOffset: 0,
                branchMode: "head",
                abandonedEvents: 0
            )
        )
    }

    private static func makeMobileTail(
        sessionID: String,
        events: [SessionEvent],
        total: Int,
        pageOffset: Int,
        snapshotEventId: Int?
    ) -> SessionMobileTailResponse {
        let detail = makeDetail(sessionID: sessionID, events: events)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
                timestamp: event.timestamp,
                event: event,
                continuedFromSessionId: nil,
                continuationKind: nil,
                originLabel: nil,
                parentOriginLabel: nil,
                parentContinuationKind: nil,
                branchedFromEventId: nil
            )
        }
        return SessionMobileTailResponse(
            session: detail,
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: total,
                pageOffset: pageOffset,
                branchMode: "head",
                abandonedEvents: 0
            ),
            snapshotEventId: snapshotEventId
        )
    }

    private static func tailPage(events: [SessionEvent], limit: Int, offset: Int) -> (events: [SessionEvent], pageOffset: Int) {
        let total = events.count
        let pageOffset = max(0, total - limit - offset)
        let end = max(0, total - offset)
        guard pageOffset < end else {
            return ([], pageOffset)
        }
        return (Array(events[pageOffset..<end]), pageOffset)
    }

    private static func makeDetail(sessionID: String, events: [SessionEvent]) -> SessionDetail {
        let detail = SessionDetail(
            id: sessionID,
            provider: "codex",
            project: "longhouse",
            cwd: "/Users/davidrose/git/zerg/longhouse",
            gitBranch: "main",
            summary: "Chat UI fixture",
            summaryTitle: "Chat UI Fixture",
            presenceState: "idle",
            presenceTool: nil,
            userState: "active",
            status: "idle",
            lastActivityAt: events.last?.timestamp,
            displayPhase: "Idle",
            activeTool: nil,
            homeLabel: "MacBook",
            originLabel: "UI test",
            capabilities: SessionCapabilities(
                liveControlAvailable: true,
                hostReattachAvailable: false,
                replyToLiveSessionAvailable: true,
                canQueueNextInput: true,
                canSteerActiveTurn: false,
                displayLabel: "Send",
                displayDetail: nil,
                displayTone: "success",
                inputMode: "live",
                defaultInputIntent: "auto",
                composerEnabled: true,
                composerPlaceholder: "Send a message to the live Codex session...",
                composerDisabledReason: nil,
                sendDisabledReason: nil,
                attachImages: false
            ),
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "live",
                signalTier: "none",
                state: "idle",
                tone: "idle",
                headline: "Idle",
                detail: "Ready for UI test input",
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: true,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isStalled: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            ),
            loopMode: .assist
        )
        return detail
    }

    private static func makeEvent(
        id: Int,
        role: String,
        content: String,
        timestamp: String,
        inputOrigin: SessionInputOrigin? = nil
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: inputOrigin
        )
    }

    /// A realistic mixed transcript that exercises the redesign's demoted
    /// tool-row CSS + TimelineBuilder pairing: assistant prose, a paired
    /// tool call+result, a passive group, and a dropped (orphaned) tool call.
    private static func toolFixtureEvents() -> [SessionEvent] {
        var events: [SessionEvent] = []
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }

        events.append(SessionEvent(
            id: next(), role: "user",
            contentText: "Find who renamed the ticket after the meeting and retry the MR state.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Now I can see exactly what Oleg did. Two new blocker tickets appeared: **PAASSE-22459** and **PAASSE-22460**. Let me pull those.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // Paired tool call + result.
        let callId = "call-jira-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil, toolOutputText: nil, toolCallId: callId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil,
            toolOutputText: "PAASSE-22459: blocked on MR rename by Oleg at 18:42.",
            toolCallId: callId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A Bash call with a large-ish output (the "work is not noise" case).
        let bashId = "call-bash-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Bash", toolInputJSON: nil, toolOutputText: nil, toolCallId: bashId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Bash", toolInputJSON: nil,
            toolOutputText: String(repeating: "git log line for changelog parsing\n", count: 12),
            toolCallId: bashId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A dropped/orphaned tool call — no matching result (the trust case).
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "mcp__atlassian__getJiraIssue", toolInputJSON: nil, toolOutputText: nil,
            toolCallId: "call-dropped-1", toolCallState: .dropped, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "The MR was renamed by Oleg at 18:42, then moved back to In Review.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        return events
    }

    private static func loadReplayEvents(path: String) -> [SessionEvent]? {
        let url = URL(fileURLWithPath: path)
        guard let data = try? Data(contentsOf: url) else { return nil }
        do {
            let fixture = try JSONDecoder().decode(ChatUITestReplayFile.self, from: data)
            return fixture.events.enumerated().map { index, event in
                SessionEvent(
                    id: event.id ?? index + 1,
                    role: event.role,
                    contentText: event.contentText,
                    toolName: event.toolName,
                    toolInputJSON: nil,
                    toolOutputText: event.toolOutputText,
                    toolCallId: event.toolCallId,
                    toolCallState: nil,
                    timestamp: event.timestamp,
                    inActiveContext: true,
                    isHeadBranch: true,
                    inputOrigin: nil
                )
            }
        } catch {
            return nil
        }
    }

    private static func messageText(index: Int, role: String, fixtureName: String) -> String {
        if role == "assistant" {
            if fixtureName == "render-storm" {
                return """
                Assistant fixture message \(index): realistic long response with markdown, code, and enough text to stress WebKit rendering.

                - Session event id: \(index)
                - Tool summary: read, search, patch, validate
                - Runtime state: streaming

                ```swift
                struct FixtureRow\(index) {
                    let id = \(index)
                    let text = "This is a realistic transcript payload with code blocks and wrapping text."
                }
                ```

                The transcript renderer should handle this without repeatedly re-rendering identical payloads, without blocking touch scrolling, and without snapping back to the bottom when the user has intentionally scrolled upward.

                \(String(repeating: "Detailed fixture paragraph for mobile rendering, scroll anchoring, markdown layout, and text wrapping. ", count: 8))
                """
            }
            return "Assistant fixture message \(index): streaming-style response with enough body to exercise row layout."
        }
        if fixtureName == "render-storm" {
            return "User fixture message \(index): realistic request text for mobile chat stress testing, scroll anchoring, and duplicate render detection."
        }
        return "User fixture message \(index): request text for chat scroll anchoring."
    }

    private static func fixedTimestamp(offset: Int) -> String {
        let date = Date(timeIntervalSince1970: 1_777_737_600 + TimeInterval(offset))
        return ISO8601DateFormatter().string(from: date)
    }
}

private struct ChatUITestReplayFile: Decodable {
    let events: [ChatUITestReplayEvent]
}

private struct ChatUITestReplayEvent: Decodable {
    let id: Int?
    let role: String
    let contentText: String?
    let toolName: String?
    let toolOutputText: String?
    let toolCallId: String?
    let timestamp: String
}
#endif
