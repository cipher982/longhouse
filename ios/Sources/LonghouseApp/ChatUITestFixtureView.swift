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
                streamFactory: { _, _ in client.streamSource() },
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
            if fixtureName == "render-storm" {
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
private final class ChatUITestProbe: ObservableObject {
    private(set) var statusLine = "renders=0 duplicates=0 repeats=0 rows=0 bytes=0 latest=none stage=none stick=0 tick=0"

    private let path: String?
    private var renderCount = 0
    private var duplicateCount = 0
    private var repeatRenderCount = 0
    private var lastRenderedKey: String?
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
            "tick=\(tick)",
        ].joined(separator: " ")
        persist()
    }

    func recordTick(_ tick: Int) {
        self.tick = tick
        statusLine = statusLine.replacingOccurrences(
            of: #"tick=\d+"#,
            with: "tick=\(tick)",
            options: .regularExpression
        )
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

    init(name: String) {
        self.name = name
        self.eventCount = max(0, UITestHooks.chatFixtureEventCount ?? (name == "stress" ? 500 : 80))
    }

    var usesRealtimeStream: Bool {
        name.hasPrefix("assistant-stream")
    }
}

private actor ChatUITestWorkspaceClient: SessionWorkspaceClient {
    let sessionID = "ui-test-chat-session"
    private var nextEventID = 1
    private var events: [SessionEvent]
    private var realtimeContinuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    private var streamingAssistantEventID: Int?

    init(fixture: ChatUITestFixture) {
        var seedEvents: [SessionEvent] = []
        for index in 0..<fixture.eventCount {
            let role = index.isMultiple(of: 2) ? "user" : "assistant"
            seedEvents.append(Self.makeEvent(
                id: index + 1,
                role: role,
                content: Self.messageText(index: index, role: role, fixtureName: fixture.name),
                timestamp: Self.fixedTimestamp(offset: index)
            ))
        }
        events = seedEvents
        nextEventID = fixture.eventCount + 1
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        Self.makeWorkspace(sessionID: sessionID, events: events)
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
        return SessionInputResponse(outcome: .sent, inputId: inputID, clientRequestId: clientRequestId, intent: intent, queued: [])
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
            stop: { await self.stopRealtimeStream() }
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
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000),
            pubsub_seq: latestID
        )))
    }

    private static func makeWorkspace(sessionID: String, events: [SessionEvent]) -> SessionWorkspaceResponse {
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
                composerDisabledReason: nil
            ),
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "live",
                signalTier: nil,
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
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: nil,
                hostState: "online",
                terminalReason: nil
            ),
            runtimeFacts: nil,
            loopMode: .assist
        )
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
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: inputOrigin
        )
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
#endif
