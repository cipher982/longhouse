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
        #expect(firstRequest?.limit == 50)
        #expect(firstRequest?.branchMode == "head")
        let firstTailRequest = await api.tailRequest(at: 0)
        #expect(firstTailRequest?.offset == 0)
        #expect(firstTailRequest?.snapshotEventId == nil)
    }

    @Test
    func startRendersFreshTranscriptPreviewAfterDurableTail() async throws {
        let previewJSON = """
        {
          "event_id": 99,
          "text": "Fresh live bridge text",
          "event_origin": "live_provisional",
          "timestamp": "2026-05-02T20:00:05Z",
          "is_provisional": true,
          "is_complete": false,
          "content_cursor": "cursor-99",
          "is_stale": false
        }
        """
        let workspace = try makeWorkspace(
            eventId: 10,
            content: "Durable tail",
            timestamp: "2026-05-02T20:00:00Z",
            transcriptPreviewJSON: previewJSON
        )
        let api = FakeSessionWorkspaceClient(workspaces: [workspace])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)

        #expect(model.items.map(\.id) == ["user:10", "prose:-99"])
    }

    @Test
    func startRefreshesAlreadyOpenSession() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before reentry")
        let after = try makeWorkspace(eventId: 11, content: "After reentry")
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await model.start(sessionId: "session-1", appState: appState)

        #expect(model.items.map(\.id) == ["user:11"])
        #expect(await api.workspaceRequestCount() == 2)
    }

    @Test
    func loadOlderPrependsPreviousTailPage() async throws {
        let tail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let older = try makeWorkspace(eventId: 1, content: "Older page", total: 100, pageOffset: 0)
        let api = FakeSessionWorkspaceClient(workspaces: [tail, older])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await model.loadOlder(sessionId: "session-1", appState: appState)

        #expect(model.items.map(\.id) == ["user:1", "user:51"])
        let olderRequest = await api.tailRequest(at: 1)
        #expect(olderRequest?.offset == 50)
        #expect(olderRequest?.snapshotEventId == 51)
    }

    @Test
    func refreshTailPreservesLoadedOlderPage() async throws {
        let tail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let older = try makeWorkspace(eventId: 1, content: "Older page", total: 100, pageOffset: 0)
        let refreshedTail = try makeWorkspace(eventId: 52, content: "Fresh tail", total: 101, pageOffset: 51)
        let api = FakeSessionWorkspaceClient(workspaces: [tail, older, refreshedTail])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await model.loadOlder(sessionId: "session-1", appState: appState)
        await model.reload(sessionId: "session-1", appState: appState)

        #expect(model.items.map(\.id) == ["user:1", "user:51", "user:52"])
        let refreshRequest = await api.tailRequest(at: 2)
        #expect(refreshRequest?.offset == 0)
        #expect(refreshRequest?.snapshotEventId == nil)
    }

    @Test
    func startRestoresCachedTailWithoutBlockingOnNetwork() async throws {
        let cached = try makeWorkspace(eventId: 20, content: "Cached tail")
        let fresh = try makeWorkspace(eventId: 21, content: "Network tail")
        let cache = SessionTranscriptCache()
        cache.store(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: cached.session,
            events: cached.events,
            loadedProjectionItemCount: cached.events.count,
            totalProjectionItemCount: cached.projection.total,
            tailSnapshotEventId: cached.events.map(\.id).max()
        )
        let api = FakeSessionWorkspaceClient(workspaces: [fresh])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false, transcriptCache: cache)

        await model.start(sessionId: "session-1", appState: appState)

        #expect(model.isInitialLoading == false)
        #expect(model.detail?.id == "session-1")
        #expect(model.items.map(\.id) == ["user:20"])
        #expect(await api.workspaceRequestCount() == 0)
    }

    @Test
    func cachePreservesLoadedOlderPagesAcrossViewModels() async throws {
        let tail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let older = try makeWorkspace(eventId: 1, content: "Older page", total: 100, pageOffset: 0)
        let fresh = try makeWorkspace(eventId: 52, content: "Network tail", total: 101, pageOffset: 51)
        let cache = SessionTranscriptCache()
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"

        let firstAPI = FakeSessionWorkspaceClient(workspaces: [tail, older])
        let firstModel = SessionViewModel(apiFactory: { _ in firstAPI }, enableRealtime: false, transcriptCache: cache)
        await firstModel.start(sessionId: "session-1", appState: appState)
        await firstModel.loadOlder(sessionId: "session-1", appState: appState)

        let secondAPI = FakeSessionWorkspaceClient(workspaces: [fresh])
        let secondModel = SessionViewModel(apiFactory: { _ in secondAPI }, enableRealtime: false, transcriptCache: cache)
        await secondModel.start(sessionId: "session-1", appState: appState)

        #expect(secondModel.items.map(\.id) == ["user:1", "user:51"])
        #expect(await secondAPI.workspaceRequestCount() == 0)
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
    func sendSynchronouslyAddsSubmittedInputForTranscriptPayload() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)

        #expect(sent)
        #expect(model.submittedInputs.first?.text == "continue")
        #expect(model.submittedInputs.first?.phase == .sent)
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
                clientRequestId: nil,
                intent: .queue,
                queued: [
                    QueuedInputSummary(
                        id: 7,
                        text: "next",
                        intent: .queue,
                        status: .queued,
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
            clientRequestId: nil,
            intent: .queue,
            queued: [
                QueuedInputSummary(
                    id: 9,
                    text: "keep going",
                    intent: .queue,
                    status: .queued,
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
            .response(SessionInputResponse(outcome: .sent, inputId: 11, clientRequestId: nil, intent: .auto, queued: [])),
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
    func submittedInputReconcilesBySessionInputId() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(
            eventId: 11,
            content: "server projected text",
            timestamp: ISO8601DateFormatter().string(from: Date()),
            inputOriginJSON: """
            {
              "authored_via": "longhouse",
              "session_input_id": 7,
              "client_request_id": null
            }
            """
        )
        let api = FakeSessionWorkspaceClient(
            workspaces: [before, after],
            sendResponse: SessionInputResponse(outcome: .sent, inputId: 7, clientRequestId: nil, intent: .auto, queued: [])
        )
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

    @Test
    func submittedInputReconcilesByClientRequestId() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
            sendResponse: SessionInputResponse(outcome: .sent, inputId: 7, clientRequestId: nil, intent: .auto, queued: []),
            afterSendWorkspace: { clientRequestId in
                try makeWorkspace(
                    eventId: 11,
                    content: "server projected text",
                    timestamp: ISO8601DateFormatter().string(from: Date()),
                    inputOriginJSON: """
                    {
                      "authored_via": "longhouse",
                      "session_input_id": null,
                      "client_request_id": "\(clientRequestId ?? "")"
                    }
                    """
                )
            }
        )
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

    @Test
    func submittedInputDoesNotReconcileByMatchingTextWithoutIdentity() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(
            eventId: 11,
            content: "continue",
            timestamp: ISO8601DateFormatter().string(from: Date())
        )
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)
        await waitForWorkspaceRequestCount(api, atLeast: 2)

        #expect(sent)
        #expect(model.submittedInputs.count == 1)
        #expect(model.items.map(\.id) == ["user:11"])
    }

    @Test
    func submittedInputDoesNotReconcileAgainstOffHeadIdentity() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(
            eventId: 11,
            content: "server projected text",
            timestamp: ISO8601DateFormatter().string(from: Date()),
            isHeadBranch: false,
            inputOriginJSON: """
            {
              "authored_via": "longhouse",
              "session_input_id": 7,
              "client_request_id": "ios-off-head-1"
            }
            """
        )
        let api = FakeSessionWorkspaceClient(
            workspaces: [before, after],
            sendResponse: SessionInputResponse(outcome: .sent, inputId: 7, clientRequestId: nil, intent: .auto, queued: [])
        )
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)
        await waitForWorkspaceRequestCount(api, atLeast: 2)

        #expect(sent)
        #expect(model.submittedInputs.count == 1)
        #expect(model.items.map(\.id) == ["user:11"])
    }

    @Test
    func transcriptDiagnosticsPostsRenderBeaconAfterWebKitRender() async throws {
        let workspace = try makeWorkspace(eventId: 10, content: "Rendered in WebKit")
        let api = FakeSessionWorkspaceClient(workspaces: [workspace])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)
        let diagnostics = RenderBeaconReporter.WebKitDiagnostics(
            stage: "rendered",
            payload_byte_size: 2048,
            row_count: 1,
            latest_item_id: "user:10",
            render_sequence: 1,
            js_failure_count: 0,
            should_stick_to_bottom: true,
            web_view_loaded: true,
            error_description: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        await model.recordTranscriptDiagnostics(diagnostics, sessionId: "session-1", appState: appState)

        let beacons = await api.renderBeacons()
        #expect(beacons.count == 1)
        #expect(beacons.first?.event_id == "10")
        #expect(beacons.first?.webkit == diagnostics)
    }

    @Test
    func transcriptDiagnosticsIgnoresNonRenderStagesForBeacons() async throws {
        let workspace = try makeWorkspace(eventId: 10, content: "Rendered in WebKit")
        let api = FakeSessionWorkspaceClient(workspaces: [workspace])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        await model.recordTranscriptDiagnostics(
            RenderBeaconReporter.WebKitDiagnostics(
                stage: "queued",
                payload_byte_size: 2048,
                row_count: 1,
                latest_item_id: "user:10",
                render_sequence: 1,
                js_failure_count: 0,
                should_stick_to_bottom: true,
                web_view_loaded: false,
                error_description: nil
            ),
            sessionId: "session-1",
            appState: appState
        )
        await model.recordTranscriptDiagnostics(
            RenderBeaconReporter.WebKitDiagnostics(
                stage: "duplicate",
                payload_byte_size: 2048,
                row_count: 1,
                latest_item_id: "user:10",
                render_sequence: 1,
                js_failure_count: 0,
                should_stick_to_bottom: true,
                web_view_loaded: true,
                error_description: nil
            ),
            sessionId: "session-1",
            appState: appState
        )

        let beacons = await api.renderBeacons()
        #expect(beacons.isEmpty)
    }

    nonisolated private func makeWorkspace(
        eventId: Int,
        content: String,
        timestamp: String = "2026-05-02T20:00:00Z",
        isHeadBranch: Bool = true,
        inputOriginJSON: String? = nil,
        transcriptPreviewJSON: String? = nil,
        total: Int = 1,
        pageOffset: Int = 0
    ) throws -> SessionWorkspaceResponse {
        let encodedContent = try jsonString(content)
        let encodedTimestamp = try jsonString(timestamp)
        let inputOriginField = inputOriginJSON.map { ",\n                  \"input_origin\": \($0)" } ?? ""
        let transcriptPreviewField = transcriptPreviewJSON.map { ",\n            \"transcript_preview\": \($0)" } ?? ""
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
            "runtime_display": {
            "truth_tier": "fresh",
            "signal_tier": "none",
            "state": null,
            "tone": "inactive",
            "headline": "Inactive",
            "detail": null,
            "phase_label": "Inactive",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
            "is_stalled": false,
            "is_managed_local_truth": false,
            "has_signal": false,
            "control_path": "unmanaged",
            "activity_recency": "none",
            "lifecycle": "open",
            "host_state": "unknown",
            "terminal_reason": null
          },
          "loop_mode": "assist"\(transcriptPreviewField)
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
                  "is_head_branch": \(isHeadBranch)\(inputOriginField)
                }
              }
            ],
            "total": \(total),
            "page_offset": \(pageOffset),
            "branch_mode": "head",
            "abandoned_events": 0
          }
        }
        """.data(using: .utf8)!
        return try JSONDecoder.snakeCase.decode(SessionWorkspaceResponse.self, from: json)
    }

    nonisolated private func jsonString(_ value: String) throws -> String {
        let data = try JSONEncoder().encode(value)
        return String(data: data, encoding: .utf8)!
    }

    private func waitForSubmittedInputsToClear(_ model: SessionViewModel) async {
        for _ in 0..<50 {
            if model.submittedInputs.isEmpty { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func waitForWorkspaceRequestCount(_ api: FakeSessionWorkspaceClient, atLeast count: Int) async {
        for _ in 0..<50 {
            if await api.workspaceRequestCount() >= count { return }
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
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
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
    private let afterSendWorkspace: (@Sendable (String?) throws -> SessionWorkspaceResponse)?
    private var shouldFailWorkspaceLoads = false
    private var sendError: Error?
    private var sendSteps: [FakeSendStep] = []
    private var workspaceRequests: [(id: String, limit: Int, branchMode: String)] = []
    private var tailRequests: [(id: String, limit: Int, offset: Int, branchMode: String, snapshotEventId: Int?)] = []
    private var sentInputs: [String] = []
    private var postedRenderBeacons: [RenderBeaconReporter.Payload] = []
    private var lastClientRequestId: String?

    init(
        workspaces: [SessionWorkspaceResponse],
        sendResponse: SessionInputResponse = SessionInputResponse(outcome: .sent, inputId: 1, clientRequestId: nil, intent: .auto, queued: []),
        afterSendWorkspace: (@Sendable (String?) throws -> SessionWorkspaceResponse)? = nil
    ) {
        self.workspaces = workspaces
        self.sendResponse = sendResponse
        self.afterSendWorkspace = afterSendWorkspace
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        workspaceRequests.append((id: id, limit: limit, branchMode: branchMode))
        return try nextWorkspace()
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse {
        workspaceRequests.append((id: id, limit: limit, branchMode: branchMode))
        tailRequests.append((id: id, limit: limit, offset: offset, branchMode: branchMode, snapshotEventId: snapshotEventId))
        let workspace = try nextWorkspace()
        return SessionMobileTailResponse(
            session: workspace.session,
            projection: workspace.projection,
            snapshotEventId: workspace.events.map(\.id).max()
        )
    }

    private func nextWorkspace() throws -> SessionWorkspaceResponse {
        if shouldFailWorkspaceLoads {
            throw URLError(.cannotConnectToHost)
        }
        if let afterSendWorkspace, lastClientRequestId != nil {
            return try afterSendWorkspace(lastClientRequestId)
        }
        if workspaces.count > 1 {
            return workspaces.removeFirst()
        }
        return workspaces[0]
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse {
        sentInputs.append("\(text):\(intent)")
        lastClientRequestId = clientRequestId
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

    func sendInputMultipart(id: String, text: String, attachments: [ComposerAttachment], clientRequestId: String?) async throws -> SessionInputResponse {
        try await sendInput(id: id, text: text, intent: "auto", clientRequestId: clientRequestId)
    }

    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse {
        DraftReplyResponse(draftText: "Draft", model: "test", generatedAt: "2026-05-02T20:00:00Z", basedOnEventIds: [])
    }

    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse {
        LoopModeResponse(sessionId: id, loopMode: loopMode)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {
        postedRenderBeacons.append(payload)
    }

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

    func tailRequest(at index: Int) -> (id: String, limit: Int, offset: Int, branchMode: String, snapshotEventId: Int?)? {
        guard tailRequests.indices.contains(index) else { return nil }
        return tailRequests[index]
    }

    func sendRequests() -> [String] {
        sentInputs
    }

    func renderBeacons() -> [RenderBeaconReporter.Payload] {
        postedRenderBeacons
    }
}
