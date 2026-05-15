#if DEBUG
import SwiftUI

@MainActor
struct ChatUITestFixtureView: View {
    private let client: ChatUITestWorkspaceClient
    @StateObject private var viewModel: SessionViewModel

    init(fixtureName: String) {
        let fixture = ChatUITestFixture(name: fixtureName)
        let client = ChatUITestWorkspaceClient(fixture: fixture)
        self.client = client
        _viewModel = StateObject(
            wrappedValue: SessionViewModel(
                apiFactory: { _ in client },
                enableRealtime: false
            )
        )
    }

    var body: some View {
        NavigationStack {
            SessionView(
                sessionId: client.sessionID,
                fallbackTitle: "Chat UI Fixture",
                viewModel: viewModel
            )
        }
    }
}

private struct ChatUITestFixture: Sendable {
    let name: String
    let eventCount: Int

    init(name: String) {
        self.name = name
        self.eventCount = max(0, UITestHooks.chatFixtureEventCount ?? (name == "stress" ? 500 : 80))
    }
}

private actor ChatUITestWorkspaceClient: SessionWorkspaceClient {
    let sessionID = "ui-test-chat-session"
    private var nextEventID = 1
    private var events: [SessionEvent]

    init(fixture: ChatUITestFixture) {
        var seedEvents: [SessionEvent] = []
        for index in 0..<fixture.eventCount {
            let role = index.isMultiple(of: 2) ? "user" : "assistant"
            seedEvents.append(Self.makeEvent(
                id: index + 1,
                role: role,
                content: Self.messageText(index: index, role: role),
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
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "user",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
        return SessionInputResponse(outcome: .sent, inputId: nextEventID, intent: intent, queued: [])
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
                displayTone: "success"
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

    private static func makeEvent(id: Int, role: String, content: String, timestamp: String) -> SessionEvent {
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
            isHeadBranch: true
        )
    }

    private static func messageText(index: Int, role: String) -> String {
        if role == "assistant" {
            return "Assistant fixture message \(index): streaming-style response with enough body to exercise row layout."
        }
        return "User fixture message \(index): request text for chat scroll anchoring."
    }

    private static func fixedTimestamp(offset: Int) -> String {
        let date = Date(timeIntervalSince1970: 1_777_737_600 + TimeInterval(offset))
        return ISO8601DateFormatter().string(from: date)
    }
}
#endif
