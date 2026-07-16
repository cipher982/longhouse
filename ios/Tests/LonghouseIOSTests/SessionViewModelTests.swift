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

        #expect(model.items.map(\.id) == ["user:10", "prose:synthetic:preview:99"])
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

        // Re-entering an already-open session reconciles in the BACKGROUND so
        // the resume never blanks the transcript while a refresh is in flight.
        // The refresh still happens; it just isn't awaited by start().
        await waitForWorkspaceRequestCount(api, atLeast: 2)
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
        #expect(olderRequest?.snapshotEventId == "51")
    }

    @Test
    func unchangedTailRefreshKeepsOlderPrefetch() async throws {
        let tail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let older = try makeWorkspace(eventId: 1, content: "Older page", total: 100, pageOffset: 0)
        let sameTail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let api = FakeSessionWorkspaceClient(workspaces: [tail, older, sameTail])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, _, _ in Self.neverConnectingStreamSource() },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        await waitForTailRequestCount(api, atLeast: 2)
        await model.reload(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)

        let offsets = await api.tailRequestOffsets()
        #expect(offsets == [0, 50, 0])
        model.stop()
    }

    @Test
    func memoryWarningDropsSpeculativePrefetchWithoutClearingTranscriptCache() async throws {
        let tail = try makeWorkspace(eventId: 51, content: "Recent tail", total: 100, pageOffset: 50)
        let prefetchedOlder = try makeWorkspace(eventId: 1, content: "Prefetched older page", total: 100, pageOffset: 0)
        let fetchedAfterWarning = try makeWorkspace(eventId: 2, content: "Fetched after warning", total: 100, pageOffset: 0)
        let otherSession = try makeWorkspace(eventId: 80, content: "Other cached session")
        let cache = SessionTranscriptCache()
        cache.store(
            serverURL: "https://example.longhouse.ai",
            sessionId: "other-session",
            detail: otherSession.session,
            events: otherSession.events,
            loadedProjectionItemCount: otherSession.events.count,
            totalProjectionItemCount: otherSession.projection.total,
            tailSnapshotEventId: otherSession.events.compactMap(\.legacyNumericId).max().map(String.init)
        )
        let api = FakeSessionWorkspaceClient(workspaces: [tail, prefetchedOlder, fetchedAfterWarning])
        await api.pauseNextTailResponse(offset: 50)
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, _, _ in Self.neverConnectingStreamSource() },
            enableRealtime: true,
            transcriptCache: cache,
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        await waitForTailRequestCount(api, atLeast: 2)
        model.handleMemoryWarning()
        await api.resumePausedTailResponses()
        await waitForTailResponseCount(api, atLeast: 2)

        #expect(cache.snapshot(serverURL: "https://example.longhouse.ai", sessionId: "session-1") != nil)
        #expect(cache.snapshot(serverURL: "https://example.longhouse.ai", sessionId: "other-session") != nil)

        await model.loadOlder(sessionId: "session-1", appState: appState)

        let offsets = await api.tailRequestOffsets()
        #expect(offsets.count == 3)
        #expect(offsets.contains(0))
        #expect(offsets.filter { $0 == 50 }.count == 2)
        #expect(await api.tailRequest(at: 2)?.offset == 50)
        #expect(await api.tailRequest(at: 2)?.snapshotEventId == "51")
        #expect(model.items.map { $0.id } == ["user:2", "user:51"])
        model.stop()
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
    func startRestoresCachedTailThenRefreshesInBackground() async throws {
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
            tailSnapshotEventId: cached.events.compactMap(\.legacyNumericId).max().map(String.init)
        )
        let api = FakeSessionWorkspaceClient(workspaces: [fresh])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false, transcriptCache: cache)

        await model.start(sessionId: "session-1", appState: appState)

        #expect(model.isInitialLoading == false)
        #expect(model.detail?.id == "session-1")
        await waitForWorkspaceRequestCount(api, atLeast: 1)
        #expect(model.items.map(\.id) == ["user:21"])
        #expect(await api.workspaceRequestCount() == 1)
    }

    @Test
    func startRestoresCachedProjectionActionBeforeRefresh() async throws {
        let cached = try makeWorkspace(eventId: 20, content: "Message after interrupt")
        let fresh = try makeWorkspace(eventId: 21, content: "Network tail")
        let projectionItems = [makeActionItem()] + cached.projection.items
        let cache = SessionTranscriptCache()
        cache.store(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: cached.session,
            events: cached.events,
            projectionItems: projectionItems,
            loadedProjectionItemCount: projectionItems.count,
            totalProjectionItemCount: projectionItems.count,
            tailSnapshotEventId: cached.events.compactMap(\.legacyNumericId).max().map(String.init)
        )
        let api = FakeSessionWorkspaceClient(workspaces: [fresh])
        await api.pauseNextTailResponse(offset: 0)
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false, transcriptCache: cache)

        await model.start(sessionId: "session-1", appState: appState)
        await waitForTailRequestCount(api, atLeast: 1)

        #expect(model.items.map(\.id) == ["action:interrupt-1", "user:20"])

        await api.resumePausedTailResponses()
        await waitForTailResponseCount(api, atLeast: 1)
        model.stop()
    }

    @Test
    func cachedHookPlaceholderIsReplacedByStructuredPauseRequestOnRefresh() async throws {
        let cachedPauseRequestJSON = """
        {
          "id": "pause-hook-placeholder",
          "session_id": "session-1",
          "runtime_key": "claude:session-1",
          "kind": "structured_question",
          "status": "pending",
          "provider": "claude",
          "can_respond": false,
          "title": "Claude needs an answer",
          "summary": "Answer this in the original terminal.",
          "tool_name": "AskUserQuestion",
          "questions": [
            {
              "id": "terminal_answer",
              "header": null,
              "question": "Claude is waiting for an interactive answer in the terminal.",
              "multi_select": false,
              "options": []
            }
          ],
          "occurred_at": "2026-06-09T19:14:30Z",
          "last_seen_at": "2026-06-09T19:14:30Z",
          "resolved_at": null,
          "expires_at": null
        }
        """
        let structuredPauseRequestJSON = """
        {
          "id": "pause-scope",
          "session_id": "session-1",
          "runtime_key": "claude:session-1",
          "kind": "structured_question",
          "status": "pending",
          "provider": "claude",
          "can_respond": true,
          "title": "Scope",
          "summary": "Waiting for your answer.",
          "tool_name": "AskUserQuestion",
          "questions": [
            {
              "id": "scope",
              "header": "Scope",
              "question": "What's the scope of the system to build now?",
              "multi_select": false,
              "options": [
                {
                  "label": "Full auth-broker login orchestrator",
                  "description": "Build reusable iMessage-code retrieval and challenge orchestration.",
                  "value": "full_orchestrator"
                },
                {
                  "label": "iMessage code reader first",
                  "description": "Build the SMS-code retrieval primitive before wiring orchestration.",
                  "value": "code_reader"
                }
              ]
            }
          ],
          "occurred_at": "2026-06-09T19:14:31Z",
          "last_seen_at": "2026-06-09T19:14:31Z",
          "resolved_at": null,
          "expires_at": null
        }
        """
        let cached = try makeWorkspace(eventId: 20, content: "Cached placeholder", pauseRequestJSON: cachedPauseRequestJSON)
        let fresh = try makeWorkspace(eventId: 21, content: "Network structured question", pauseRequestJSON: structuredPauseRequestJSON)
        let cache = SessionTranscriptCache()
        cache.store(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: cached.session,
            events: cached.events,
            loadedProjectionItemCount: cached.events.count,
            totalProjectionItemCount: cached.projection.total,
            tailSnapshotEventId: cached.events.compactMap(\.legacyNumericId).max().map(String.init)
        )
        let api = FakeSessionWorkspaceClient(workspaces: [fresh])
        await api.pauseNextTailResponse(offset: 0)
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false, transcriptCache: cache)

        await model.start(sessionId: "session-1", appState: appState)
        await waitForTailRequestCount(api, atLeast: 1)
        #expect(model.detail?.activePauseRequest?.id == "pause-hook-placeholder")
        #expect(model.detail?.activePauseRequest?.questions.first?.id == "terminal_answer")

        await api.resumePausedTailResponses()
        await waitForTailResponseCount(api, atLeast: 1)

        #expect(model.items.map(\.id) == ["user:21"])
        let request = try #require(model.detail?.activePauseRequest)
        #expect(request.id == "pause-scope")
        #expect(request.canRespond)
        #expect(request.questions.first?.id == "scope")
        #expect(request.questions.first?.options.map(\.label) == [
            "Full auth-broker login orchestrator",
            "iMessage code reader first"
        ])
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

        await waitForWorkspaceRequestCount(secondAPI, atLeast: 1)
        #expect(secondModel.items.map(\.id) == ["user:1", "user:51", "user:52"])
        #expect(await secondAPI.workspaceRequestCount() == 1)
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
        #expect(model.refreshErrorMessage == nil)
        #expect(model.items.map(\.id) == ["user:10"])
        #expect(model.submittedInputs.first?.phase == .sent)
        #expect(await api.workspaceRequestCount() >= 1)
    }

    @Test
    func ambiguousSendKeepsSubmittedTextWithoutTerminalFailure() async throws {
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
        #expect(model.submittedInputs.first?.phase == .couldNotConfirm)
        #expect(model.errorMessage == nil)
        #expect(model.refreshErrorMessage?.contains("confirm delivery") == true)
        #expect(model.refreshErrorMessage?.contains("Send failed") != true)
    }

    @Test
    func structuredSendRejectionIsTerminalFailure() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(workspaces: [before])
        await api.failFutureSends(LonghouseAPIError.structured(
            status: 409,
            errorCode: "input_conflict",
            message: "That client request id already belongs to a different message."
        ))
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "conflict", sessionId: "session-1", appState: appState)

        #expect(!sent)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .failed)
        #expect(model.errorMessage == "Could not send: That client request id already belongs to a different message.")
        #expect(model.refreshErrorMessage == nil)
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
    func activeConsoleReceiptKeepsOptimisticBubbleWorking() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
            sendResponse: SessionInputResponse(
                outcome: .sent,
                inputId: nil,
                clientRequestId: "request-console",
                turn: ConsoleTurnReceipt(
                    turnId: "turn-1",
                    runId: "run-1",
                    state: "active"
                ),
                intent: .auto,
                queued: []
            )
        )
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        #expect(await model.send(text: "work on this", sessionId: "session-1", appState: appState))
        #expect(model.submittedInputs.first?.phase == .working)
        #expect(model.submittedInputs.first?.turnId == "turn-1")
        #expect(model.submittedInputs.first?.runId == "run-1")
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
    func respondToPauseRequestPostsStructuredAnswersAndRefreshesTail() async throws {
        let pauseRequestJSON = """
        {
          "id": "pause-1",
          "session_id": "session-1",
          "runtime_key": "codex:session-1",
          "kind": "structured_question",
          "status": "pending",
          "provider": "codex",
          "can_respond": true,
          "title": "Choose storage",
          "summary": "Codex needs a storage decision.",
          "tool_name": "requestUserInput",
          "questions": [
            {
              "id": "storage",
              "header": "Storage",
              "question": "Which storage backend?",
              "multi_select": false,
              "options": [
                {"label": "SQLite", "description": "Keep it local.", "value": "sqlite"},
                {"label": "Postgres", "description": "Use managed DB.", "value": "postgres"}
              ]
            }
          ],
          "occurred_at": "2026-05-02T20:00:00Z",
          "last_seen_at": "2026-05-02T20:00:00Z",
          "resolved_at": null,
          "expires_at": null
        }
        """
        let before = try makeWorkspace(eventId: 10, content: "Before answer", pauseRequestJSON: pauseRequestJSON)
        let after = try makeWorkspace(eventId: 11, content: "After answer")
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let request = try #require(model.detail?.activePauseRequest)
        let answered = await model.respondToPauseRequest(
            sessionId: "session-1",
            appState: appState,
            pauseRequest: request,
            decision: "answer",
            answers: ["storage": ["sqlite"]],
            content: nil,
            message: "Storage: sqlite"
        )

        #expect(answered)
        #expect(model.isRespondingToPauseRequest == false)
        #expect(model.pauseResponseErrorMessage == nil)
        #expect(model.items.map(\.id) == ["user:11"])
        let responses = await api.pauseResponses()
        #expect(responses.count == 1)
        #expect(responses.first?.sessionId == "session-1")
        #expect(responses.first?.pauseRequestId == "pause-1")
        #expect(responses.first?.decision == "answer")
        #expect(responses.first?.answers?["storage"] == ["sqlite"])
        #expect(responses.first?.content == nil)
        #expect(responses.first?.message == "Storage: sqlite")
        #expect(await api.workspaceRequestCount() == 2)
    }

    @Test
    func failedPauseResponseRefreshesStalePauseState() async throws {
        let pauseRequestJSON = """
        {
          "id": "pause-stale",
          "session_id": "session-1",
          "runtime_key": "codex:session-1",
          "kind": "structured_question",
          "status": "pending",
          "provider": "codex",
          "can_respond": true,
          "title": "Choose storage",
          "summary": "Codex needs a storage decision.",
          "tool_name": "requestUserInput",
          "questions": [
            {
              "id": "storage",
              "header": "Storage",
              "question": "Which storage backend?",
              "multi_select": false,
              "options": [
                {"label": "SQLite", "description": "Keep it local.", "value": "sqlite"}
              ]
            }
          ],
          "occurred_at": "2026-05-02T20:00:00Z",
          "last_seen_at": "2026-05-02T20:00:00Z",
          "resolved_at": null,
          "expires_at": null
        }
        """
        let before = try makeWorkspace(eventId: 10, content: "Before stale answer", pauseRequestJSON: pauseRequestJSON)
        let after = try makeWorkspace(eventId: 11, content: "Already resolved")
        let api = FakeSessionWorkspaceClient(workspaces: [before, after])
        await api.failFuturePauseResponses(
            LonghouseAPIError.structured(
                status: 409,
                errorCode: "pause_request_not_pending",
                message: "This provider question has already resolved."
            )
        )
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let request = try #require(model.detail?.activePauseRequest)
        let answered = await model.respondToPauseRequest(
            sessionId: "session-1",
            appState: appState,
            pauseRequest: request,
            decision: "answer",
            answers: ["storage": ["sqlite"]],
            content: nil,
            message: "Storage: sqlite"
        )

        #expect(!answered)
        #expect(model.pauseResponseErrorMessage == "This provider question has already resolved.")
        #expect(model.detail?.activePauseRequest == nil)
        #expect(model.items.map(\.id) == ["user:11"])
        #expect(await api.workspaceRequestCount() == 2)
    }

    @Test
    func successfulRetryClearsPriorUnconfirmedBubbleForSameText() async throws {
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
        #expect(model.submittedInputs.first?.phase == .couldNotConfirm)

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
    func submittedInputReconcilesByExactRecentTextWhenOriginIsMissing() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
            sendResponse: SessionInputResponse(
                outcome: .sent,
                inputId: nil,
                clientRequestId: nil,
                turn: ConsoleTurnReceipt(turnId: "turn-1", runId: "run-1", state: "active"),
                intent: .auto,
                queued: []
            ),
            afterSendWorkspace: { _ in
                try makeWorkspace(
                    eventId: 11,
                    content: "continue",
                    timestamp: ISO8601DateFormatter().string(from: Date()),
                    inputOriginJSON: nil
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
    func unconfirmedSubmittedInputReconcilesByClientRequestIdAfterTailRefresh() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
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
        await api.setSendSteps([.requestFailed])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)
        await waitForSubmittedInputsToClear(model)

        #expect(!sent)
        #expect(model.submittedInputs.isEmpty)
        #expect(model.items.map(\.id) == ["user:11"])
        #expect(model.errorMessage == nil)
    }

    @Test
    func submittedInputDoesNotReconcileByMatchingStaleTextWithoutIdentity() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let after = try makeWorkspace(
            eventId: 11,
            content: "continue",
            timestamp: ISO8601DateFormatter().string(from: Date().addingTimeInterval(-700))
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
    func unconfirmedSubmittedInputDoesNotReconcileByMatchingTextWithoutIdentity() async throws {
        let before = try makeWorkspace(eventId: 10, content: "Before send")
        let api = FakeSessionWorkspaceClient(
            workspaces: [before],
            afterSendWorkspace: { _ in
                try makeWorkspace(
                    eventId: 11,
                    content: "continue",
                    timestamp: ISO8601DateFormatter().string(from: Date())
                )
            }
        )
        await api.setSendSteps([.requestFailed])
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(apiFactory: { _ in api }, enableRealtime: false)

        await model.start(sessionId: "session-1", appState: appState)
        let sent = await model.send(text: "continue", sessionId: "session-1", appState: appState)
        await waitForWorkspaceRequestCount(api, atLeast: 2)

        #expect(!sent)
        #expect(model.submittedInputs.count == 1)
        #expect(model.submittedInputs.first?.phase == .couldNotConfirm)
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
        pauseRequestJSON: String? = nil,
        total: Int = 1,
        pageOffset: Int = 0
    ) throws -> SessionWorkspaceResponse {
        let encodedContent = try jsonString(content)
        let encodedTimestamp = try jsonString(timestamp)
        let inputOriginField = inputOriginJSON.map { ",\n                  \"input_origin\": \($0)" } ?? ""
        let transcriptPreviewField = transcriptPreviewJSON.map { ",\n            \"transcript_preview\": \($0)" } ?? ""
        let pauseRequestField = pauseRequestJSON.map { ",\n            \"pause_request\": \($0)" } ?? ""
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
            "terminal_reason": null\(pauseRequestField)
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
        return try JSONDecoder.snakeCase.decodeSessionFixture(SessionWorkspaceResponse.self, from: json)
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

    private func waitForTailRequestCount(_ api: FakeSessionWorkspaceClient, atLeast count: Int) async {
        for _ in 0..<50 {
            if await api.tailRequestCount() >= count { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func waitForTailResponseCount(_ api: FakeSessionWorkspaceClient, atLeast count: Int) async {
        for _ in 0..<50 {
            if await api.tailResponseCount() >= count { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private static func neverConnectingStreamSource() -> SessionWorkspaceStreamSource {
        SessionWorkspaceStreamSource(
            start: { AsyncStream { _ in } },
            stop: {},
            clockSkewMs: { 0 }
        )
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

    private func makeActionItem() -> SessionProjectionItem {
        SessionProjectionItem(
            kind: "action",
            sessionId: "session-1",
            timestamp: "2026-05-02T19:59:59Z",
            event: nil,
            action: SessionAction(
                id: "action:interrupt-1",
                kind: "turn_interrupted",
                provider: "codex",
                source: "user",
                providerReason: "interrupted",
                eventId: 19
            ),
            continuedFromSessionId: nil,
            continuationKind: nil,
            originLabel: nil,
            parentOriginLabel: nil,
            parentContinuationKind: nil,
            branchedFromEventId: nil
        )
    }
}

private enum FakeSendStep: Sendable {
    case response(SessionInputResponse)
    case requestFailed
    case turnEnded(String)
}

private actor FakeSessionWorkspaceClient: SessionWorkspaceClient {
    struct PauseResponseRecord: Sendable {
        let sessionId: String
        let pauseRequestId: String
        let decision: String
        let answers: [String: [String]]?
        let content: String?
        let message: String?
    }

    private var workspaces: [SessionWorkspaceResponse]
    private let sendResponse: SessionInputResponse
    private let afterSendWorkspace: (@Sendable (String?) throws -> SessionWorkspaceResponse)?
    private var shouldFailWorkspaceLoads = false
    private var sendError: Error?
    private var sendSteps: [FakeSendStep] = []
    private var pauseResponseError: Error?
    private var pauseResponse: PauseRequestResponse?
    private var workspaceRequests: [(id: String, limit: Int, branchMode: String)] = []
    private var tailRequests: [(id: String, limit: Int, offset: Int, branchMode: String, snapshotEventId: String?, cursor: String?)] = []
    private var tailResponses = 0
    private var pausedTailResponseCounts: [Int: Int] = [:]
    private var pausedTailContinuations: [CheckedContinuation<Void, Never>] = []
    private var sentInputs: [String] = []
    private var pauseResponseRequests: [PauseResponseRecord] = []
    private var postedRenderBeacons: [RenderBeaconReporter.Payload] = []
    private var lastClientRequestId: String?

    init(
        workspaces: [SessionWorkspaceResponse],
        sendResponse: SessionInputResponse = SessionInputResponse(outcome: .sent, inputId: 1, clientRequestId: nil, intent: .auto, queued: []),
        afterSendWorkspace: (@Sendable (String?) throws -> SessionWorkspaceResponse)? = nil,
        pauseResponse: PauseRequestResponse? = nil
    ) {
        self.workspaces = workspaces
        self.sendResponse = sendResponse
        self.afterSendWorkspace = afterSendWorkspace
        self.pauseResponse = pauseResponse
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
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse {
        workspaceRequests.append((id: id, limit: limit, branchMode: branchMode))
        tailRequests.append((id: id, limit: limit, offset: offset, branchMode: branchMode, snapshotEventId: snapshotEventId, cursor: cursor))
        if let pausedCount = pausedTailResponseCounts[offset], pausedCount > 0 {
            if pausedCount == 1 {
                pausedTailResponseCounts[offset] = nil
            } else {
                pausedTailResponseCounts[offset] = pausedCount - 1
            }
            await withCheckedContinuation { continuation in
                pausedTailContinuations.append(continuation)
            }
        }
        let workspace = try nextWorkspace()
        tailResponses += 1
        return SessionMobileTailResponse(
            session: workspace.session,
            projection: workspace.projection,
            snapshotEventId: workspace.events.compactMap(\.legacyNumericId).max().map(String.init)
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

    func respondToPauseRequest(
        sessionId: String,
        pauseRequestId: String,
        decision: String,
        answers: [String: [String]]?,
        content: String?,
        message: String?
    ) async throws -> PauseRequestResponse {
        pauseResponseRequests.append(PauseResponseRecord(
            sessionId: sessionId,
            pauseRequestId: pauseRequestId,
            decision: decision,
            answers: answers,
            content: content,
            message: message
        ))
        if let pauseResponseError {
            throw pauseResponseError
        }
        if let pauseResponse {
            return pauseResponse
        }
        return PauseRequestResponse(
            status: "resolved",
            pauseRequest: SessionPauseRequest(
                id: pauseRequestId,
                sessionId: sessionId,
                runtimeKey: "codex:\(sessionId)",
                kind: "structured_question",
                status: "resolved",
                provider: "codex",
                canRespond: false,
                title: nil,
                summary: nil,
                toolName: nil,
                questions: [],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: "2026-05-02T20:00:01Z",
                expiresAt: nil
            )
        )
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

    func failFuturePauseResponses(_ error: Error) {
        pauseResponseError = error
    }

    func pauseNextTailResponse(offset: Int) {
        pausedTailResponseCounts[offset, default: 0] += 1
    }

    func resumePausedTailResponses() {
        let continuations = pausedTailContinuations
        pausedTailContinuations.removeAll()
        for continuation in continuations {
            continuation.resume()
        }
    }

    func workspaceRequestCount() -> Int {
        workspaceRequests.count
    }

    func workspaceRequest(at index: Int) -> (id: String, limit: Int, branchMode: String)? {
        guard workspaceRequests.indices.contains(index) else { return nil }
        return workspaceRequests[index]
    }

    func tailRequest(at index: Int) -> (id: String, limit: Int, offset: Int, branchMode: String, snapshotEventId: String?, cursor: String?)? {
        guard tailRequests.indices.contains(index) else { return nil }
        return tailRequests[index]
    }

    func tailRequestCount() -> Int {
        tailRequests.count
    }

    func tailRequestOffsets() -> [Int] {
        tailRequests.map(\.offset)
    }

    func tailResponseCount() -> Int {
        tailResponses
    }

    func sendRequests() -> [String] {
        sentInputs
    }

    func pauseResponses() -> [PauseResponseRecord] {
        pauseResponseRequests
    }

    func renderBeacons() -> [RenderBeaconReporter.Payload] {
        postedRenderBeacons
    }
}
