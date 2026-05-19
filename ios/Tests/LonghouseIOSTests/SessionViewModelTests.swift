import Foundation
import Testing

@testable import Longhouse

@MainActor
struct SessionViewModelTests {
    @Test
    func startLoadsSessionWorkspace() async throws {
        let workspace = try makeWorkspace(eventId: 10, content: "Load the workspace")
        let api = FakeSessionWorkspaceClient(workspaces: [workspace])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)

        #expect(model.errorMessage == nil)
        #expect(model.isInitialLoading == false)
        #expect(model.detail?.id == "session-1")
        #expect(model.items.map(\.id) == ["user:10"])
        let firstRequest = await api.workspaceRequest(at: 0)
        #expect(firstRequest?.id == "session-1")
        #expect(firstRequest?.limit == 200)
        #expect(firstRequest?.branchMode == "head")
    }

    @Test
    func sendReturnsBeforeWorkspaceRefreshCompletes() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(eventId: 11, content: "After send")
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await api.failFutureWorkspaceLoads()
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)

        #expect(sent)
        #expect(model.lastSendOutcome == .sent)
        #expect(model.items.map(\.id) == ["user:10"])
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .sent)
        #expect(await api.sendRequests() == ["continue:auto"])
    }

    @Test
    func sendSynchronouslyBumpsTranscriptScrollToken() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let beforeSendToken = model.transcriptScrollToken
        let beforeRevealCounter = model.submittedRevealCounter
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)

        #expect(sent)
        #expect(model.transcriptScrollToken != beforeSendToken)
        #expect(model.submittedRevealCounter == beforeRevealCounter + 1)
        #expect(model.submittedInputs.first?.text == "continue")
        #expect(model.submittedInputs.first?.phase == .sent)
    }

    @Test
    func transcriptScrollTokenChangesWhenLastItemContentGrows() async throws {
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in nil }, enableRealtime: false)

        model.items = TimelineBuilder.build(events: [
            makeEvent(id: 20, role: "assistant", content: "Thinking")
        ])
        let before = model.transcriptScrollToken
        model.items = TimelineBuilder.build(events: [
            makeEvent(id: 20, role: "assistant", content: "Thinking\n\nHere is the full answer.")
        ])

        #expect(model.transcriptScrollToken != before)
    }

    @Test
    func sendDoesNotBlankTranscriptWhenBestEffortRefreshFails() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await api.failFutureWorkspaceLoads()
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)

        #expect(sent)
        #expect(model.errorMessage == nil)
        #expect(model.items.map(\.id) == ["user:10"])
        #expect(await api.workspaceRequestCount() >= 1)
    }

    @Test
    func sendFailureKeepsSubmittedTextOutOfComposerState() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        await api.failFutureSends(URLError(.notConnectedToInternet))
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "do not lose this", sessionId: "session-1", appState: appState)

        #expect(!sent)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.text == "do not lose this")
        #expect(model.submittedInputs.first?.phase == .failed)
        #expect(model.errorMessage?.contains("Send failed") == true)
    }

    @Test
    func queuedSendUpdatesSubmittedStateWithoutTranscriptRefresh() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
            sendResponse: SessionInputResponse(
                outcome: .queued,
                inputId: 7,
                intent: "queue",
                queued: [
                    QueuedInputSummary(
                        id: 7,
                        text: "next",
                        intent: "queue",
                        status: "queued",
                        lastError: nil,
                        createdAt: "2026-05-02T20:00:00Z"
                    )
                ]
            )
        )
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "next", sessionId: "session-1", appState: appState, intent: "queue")

        #expect(sent)
        #expect(model.lastSendOutcome == .queued)
        #expect(model.queuedInputCount == 1)
        #expect(model.submittedInputs.first?.phase == .queued)
        #expect(model.submittedInputs.first?.serverInputId == 7)
    }

    @Test
    func queueInsteadOfSteerRemovesOriginalDecisionBubble() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let queuedResponse = SessionInputResponse(
            outcome: .queued,
            inputId: 9,
            intent: "queue",
            queued: [
                QueuedInputSummary(
                    id: 9,
                    text: "keep going",
                    intent: "queue",
                    status: "queued",
                    lastError: nil,
                    createdAt: "2026-05-02T20:00:00Z"
                )
            ]
        )
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        await api.setSendSteps([
            .turnEnded("Active turn ended."),
            .response(queuedResponse),
        ])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let steered = await model.send(text: "keep going", sessionId: "session-1", appState: appState, intent: "steer")
        #expect(!steered)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .needsUserDecision)

        let queued = await model.queueInsteadOfSteer(sessionId: "session-1", appState: appState)

        #expect(queued)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .queued)
        #expect(model.submittedInputs.first?.serverInputId == 9)
    }

    @Test
    func successfulRetryClearsPriorFailedBubbleForSameText() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        await api.setSendSteps([
            .requestFailed,
            .response(SessionInputResponse(outcome: .sent, inputId: 11, intent: "auto", queued: [])),
        ])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let failed = await model.send(text: "retry me", sessionId: "session-1", appState: appState)
        #expect(!failed)
        #expect(model.submittedInputs.first?.phase == .failed)

        let retried = await model.send(text: "retry me", sessionId: "session-1", appState: appState)

        #expect(retried)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .sent)
        #expect(model.submittedInputs.first?.serverInputId == 11)
    }

    @Test
    func claudeChannelWrapperIsStrippedForDisplayText() {
        #expect(
            ClaudeChannelText.stripWrapper("<channel name=\"commentary\">\ncontinue\n</channel>")
                == "continue"
        )
        #expect(ClaudeChannelText.stripWrapper("continue") == "continue")
        #expect(ClaudeChannelText.stripWrapper("<channel>\ncontinue") == "<channel>\ncontinue")
    }

    @Test
    func submittedInputReconcilesAgainstClaudeChannelWrappedTranscriptEvent() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(
            eventId: 11,
            content: "<channel name=\"commentary\">\ncontinue\n</channel>",
            timestamp: ISO8601DateFormatter().string(from: Date())
        )
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)
        await waitForSubmittedInputsToClear(model)

        #expect(sent)
        #expect(model.submittedInputs.isEmpty)
        #expect(model.items.map(\.id) == ["user:11"])
    }

    private func makeWorkspace(
        eventId: Int,
        content: String,
        timestamp: String = "2026-05-02T20:00:00Z"
    ) throws -> SessionWorkspaceResponse {
        let encodedContent = try jsonString(content)
        let encodedTimestamp = try jsonString(timestamp)
        let json = """
        {
          "session": {
            "id": "session-1",
            "provider": "codex",
            "project": "zerg",
            "summary_title": "Workspace Session",
            "user_state": "active",
            "capabilities": {
              "live_control_available": true,
              "host_reattach_available": true,
              "reply_to_live_session_available": true
            },
            "loop_mode": "assist"
          },
          "thread": {
            "root_session_id": "session-1",
            "head_session_id": "session-1",
            "sessions": []
          },
          "projection": {
            "root_session_id": "session-1",
            "focus_session_id": "session-1",
            "head_session_id": "session-1",
            "path_session_ids": ["session-1"],
            "items": [
              {
                "kind": "event",
                "session_id": "session-1",
                "timestamp": "2026-05-02T20:00:00Z",
                "event": {
                  "id": \(eventId),
                  "role": "user",
                  "content_text": \(encodedContent),
                  "timestamp": \(encodedTimestamp),
                  "in_active_context": true,
                  "is_head_branch": true
                }
              }
            ],
            "total": 1,
            "page_offset": 0,
            "branch_mode": "head",
            "abandoned_events": 0
          }
        }
        """.data(using: .utf8)!
        return try JSONDecoder.snakeCase.decode(SessionWorkspaceResponse.self, from: json)
    }

    private func jsonString(_ value: String) throws -> String {
        let data = try JSONEncoder().encode(value)
        return String(data: data, encoding: .utf8)!
    }

    private func waitForSubmittedInputsToClear(_ model: SessionViewModel) async {
        for _ in 0..<50 {
            if model.submittedInputs.isEmpty { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func makeEvent(id: Int, role: String, content: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: true
        )
    }
}

private enum FakeSendStep: Sendable {
    case response(SessionInputResponse)
    case requestFailed
    case turnEnded(String)
}

private actor FakeSessionWorkspaceClient: SessionWorkspaceClient {
    private var workspaces: [SessionWorkspaceResponse]
    private let sendResponse: SessionInputResponse
    private var shouldFailWorkspaceLoads = false
    private var sendError: Error?
    private var sendSteps: [FakeSendStep] = []
    private var workspaceRequests: [(id: String, limit: Int, branchMode: String)] = []
    private var sentInputs: [String] = []

    init(
        workspaces: [SessionWorkspaceResponse],
        sendResponse: SessionInputResponse = SessionInputResponse(outcome: .sent, inputId: 1, intent: "auto", queued: [])
    ) {
        self.workspaces = workspaces
        self.sendResponse = sendResponse
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        workspaceRequests.append((id: id, limit: limit, branchMode: branchMode))
        if shouldFailWorkspaceLoads {
            throw URLError(.cannotConnectToHost)
        }
        if workspaces.count > 1 {
            return workspaces.removeFirst()
        }
        return workspaces[0]
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse {
        sentInputs.append("\(text):\(intent)")
        if !sendSteps.isEmpty {
            let step = sendSteps.removeFirst()
            switch step {
            case .response(let response):
                return response
            case .requestFailed:
                throw LonghouseAPIError.requestFailed
            case .turnEnded(let message):
                throw LonghouseAPIError.structured(status: 409, errorCode: "turn_ended", message: message)
            }
        }
        if let sendError {
            throw sendError
        }
        return sendResponse
    }

    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse {
        DraftReplyResponse(draftText: "Draft", model: "test", generatedAt: "2026-05-02T20:00:00Z", basedOnEventIds: [])
    }

    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse {
        LoopModeResponse(sessionId: id, loopMode: loopMode)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}

    func failFutureWorkspaceLoads() {
        shouldFailWorkspaceLoads = true
    }

    func failFutureSends(_ error: Error) {
        sendError = error
    }

    func setSendSteps(_ steps: [FakeSendStep]) {
        sendSteps = steps
    }

    func workspaceRequestCount() -> Int {
        workspaceRequests.count
    }

    func workspaceRequest(at index: Int) -> (id: String, limit: Int, branchMode: String)? {
        guard workspaceRequests.indices.contains(index) else { return nil }
        return workspaceRequests[index]
    }

    func sendRequests() -> [String] {
        sentInputs
    }
}
