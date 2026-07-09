import Foundation
import os
import Testing

@testable import Longhouse

/// M2 "Resume correctness": the workspace stream seeds its reconnect cursor
/// from the persisted pubsub_seq, and a 401 triggers a single auth-refresh +
/// stream restart rather than a silent reconnect loop.
@MainActor
struct SessionStreamResumeTests {
    private let serverURL = "https://example.longhouse.ai"

    @Test
    func streamSeedsReconnectCursorFromPersistedPubsubSeq() async throws {
        let workspace = try TestWorkspaceFactory.make(eventId: 30, content: "Tail")
        let api = FakeStreamResumeClient(workspaces: [workspace])
        // Disk snapshot carries a prior pubsub_seq so a cold start can replay.
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-stream-tests-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = TranscriptSnapshotStore(directory: dir)
        store.save(
            serverURL: serverURL,
            sessionId: "session-1",
            detail: workspace.session,
            events: workspace.events,
            loadedProjectionItemCount: workspace.events.count,
            totalProjectionItemCount: workspace.projection.total,
            tailSnapshotEventId: 30,
            lastPubsubSeq: 777,
            workspaceRevisionFingerprint: "sha256:cached"
        )
        store.waitForPendingWrites()

        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq, fingerprint in
                recorder.make(sinceSeq: sinceSeq, knownWorkspaceFingerprint: fingerprint)
            },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)
        // Let the stream task spin up.
        try? await Task.sleep(nanoseconds: 50_000_000)

        #expect(recorder.lastSinceSeq == 777)
        #expect(recorder.lastKnownWorkspaceFingerprint == "sha256:cached")
        model.stop()
    }

    @Test
    func unauthorizedTriggersSingleAuthRefreshAndRestart() async throws {
        let workspace = try TestWorkspaceFactory.make(eventId: 30, content: "Tail")
        let api = FakeStreamResumeClient(workspaces: [workspace])
        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq, fingerprint in
                recorder.make(sinceSeq: sinceSeq, knownWorkspaceFingerprint: fingerprint)
            },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        await waitForStartCount(recorder, atLeast: 1)
        let startsBefore = recorder.startCount

        // The active stream reports a 401.
        recorder.emitUnauthorized()
        await waitForStartCount(recorder, atLeast: startsBefore + 1)

        // Exactly one refresh REST call and one stream restart.
        #expect(await api.tailRequestCount() >= 1)
        #expect(recorder.startCount == startsBefore + 1)

        // After a successful connect, the guard resets so a later 401 (e.g. a
        // cookie that expires mid-session) can refresh again.
        let startsAfterFirst = recorder.startCount
        let tailRequestsAfterFirst = await api.tailRequestCount()
        recorder.emitConnected()
        try? await Task.sleep(nanoseconds: 30_000_000)
        recorder.emitUnauthorized()
        await waitForStartCount(recorder, atLeast: startsAfterFirst + 1)
        #expect(await api.tailRequestCount() >= tailRequestsAfterFirst + 1)
        #expect(recorder.startCount == startsAfterFirst + 1, "connect should re-arm refresh")

        model.stop()
    }

    @Test
    func suppressedSecondUnauthorizedLeavesStreamReattachable() async throws {
        let workspace = try TestWorkspaceFactory.make(eventId: 30, content: "Tail")
        let api = FakeStreamResumeClient(workspaces: [workspace])
        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq, fingerprint in
                recorder.make(sinceSeq: sinceSeq, knownWorkspaceFingerprint: fingerprint)
            },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        await waitForStartCount(recorder, atLeast: 1)

        // First 401 → one refresh + restart. Second 401 (no connect) is
        // suppressed and must drop the dead stream handle.
        let startsBeforeUnauthorized = recorder.startCount
        recorder.emitUnauthorized()
        await waitForStartCount(recorder, atLeast: startsBeforeUnauthorized + 1)
        recorder.emitUnauthorized()
        #expect(await waitForStreamDetached(model), "suppressed 401 should clear the dead stream")
        let startsAfterSuppressed = recorder.startCount

        // A foreground resume of the same session must reattach the stream,
        // proving the dead handle was cleared (start() gates on streamTask==nil).
        await model.start(sessionId: "session-1", appState: appState)
        await waitForStartCount(recorder, atLeast: startsAfterSuppressed + 1)
        #expect(recorder.startCount == startsAfterSuppressed + 1, "resume should reattach a dead stream")

        model.stop()
    }

    @Test
    func replayGapRefreshesTailAndClearsStaleCursor() async throws {
        let workspace = try TestWorkspaceFactory.make(eventId: 30, content: "Tail")
        let api = FakeStreamResumeClient(workspaces: [workspace])
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-stream-gap-tests-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = TranscriptSnapshotStore(directory: dir)
        store.save(
            serverURL: serverURL,
            sessionId: "session-1",
            detail: workspace.session,
            events: workspace.events,
            loadedProjectionItemCount: workspace.events.count,
            totalProjectionItemCount: workspace.projection.total,
            tailSnapshotEventId: 30,
            lastPubsubSeq: 777,
            workspaceRevisionFingerprint: "sha256:cached-gap"
        )
        store.waitForPendingWrites()

        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq, fingerprint in
                recorder.make(sinceSeq: sinceSeq, knownWorkspaceFingerprint: fingerprint)
            },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)
        #expect(recorder.lastSinceSeq == 777)
        #expect(recorder.lastKnownWorkspaceFingerprint == "sha256:cached-gap")

        recorder.emitReplayGap(latestSeq: 0)
        try? await Task.sleep(nanoseconds: 100_000_000)
        #expect(await api.tailRequestCount() >= 1)

        model.pauseRealtime()
        await model.start(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)
        #expect(recorder.lastSinceSeq == nil, "replay gap should clear stale persisted cursor")
        #expect(recorder.lastKnownWorkspaceFingerprint == nil, "tail refresh should replace a stale cached fingerprint")

        model.stop()
    }

    @Test
    func streamChangedRetriesTailRefreshAfterTransientFailure() async throws {
        let before = try TestWorkspaceFactory.make(eventId: 10, content: "Before stream wake")
        let after = try TestWorkspaceFactory.make(eventId: 11, content: "Final durable message")
        let api = FakeStreamResumeClient(workspaces: [before, after])
        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq, fingerprint in
                recorder.make(sinceSeq: sinceSeq, knownWorkspaceFingerprint: fingerprint)
            },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil,
            realtimeRefreshRetryDelaysNanoseconds: [20_000_000]
        )

        await model.start(sessionId: "session-1", appState: appState)
        await waitForItemIds(model, ["user:10"])
        #expect(model.items.map(\.id) == ["user:10"])
        await api.failNextTailRequests(1)

        recorder.emitChanged(latestEventId: 11, pubsubSeq: 778)
        await waitForItemIds(model, ["user:11"])

        #expect(model.items.map(\.id) == ["user:11"])
        #expect(await api.tailRequestCount() >= 3)
        #expect(model.refreshErrorMessage == nil)

        model.stop()
    }

    @Test
    func visiblePollPolicyCoversDisconnectedRunningToolAndManagedBackstop() {
        #expect(SessionViewModel.visiblePollDelayNanoseconds(completedTicks: 0) == 750_000_000)
        #expect(SessionViewModel.visiblePollDelayNanoseconds(completedTicks: 3) == 5_000_000_000)
        #expect(SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: false, ticks: 1))
        #expect(SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: false, ticks: 3))
        #expect(SessionViewModel.shouldPollVisibleSession(connected: false, hasRunningTool: false, managed: false, ticks: 1))
        #expect(SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: false, setupPending: true, ticks: 30))
        #expect(SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: true, managed: false, ticks: 12))
        #expect(SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: true, ticks: 6))
        #expect(!SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: false, ticks: 30))
        #expect(!SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: true, managed: false, ticks: 11))
        #expect(!SessionViewModel.shouldPollVisibleSession(connected: true, hasRunningTool: false, managed: true, ticks: 5))
    }

    private func waitForItemIds(_ model: SessionViewModel, _ expected: [String]) async {
        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline {
            if model.items.map(\.id) == expected {
                return
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func waitForStartCount(_ recorder: StreamFactoryRecorder, atLeast count: Int) async {
        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline {
            if recorder.startCount >= count {
                return
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func waitForStreamDetached(_ model: SessionViewModel) async -> Bool {
        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline {
            if !model.hasRealtimeStreamTaskForTesting {
                return true
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return false
    }
}

/// Records stream factory invocations and lets a test drive the live stream.
/// `OSAllocatedUnfairLock.withLock` is safe to call from async contexts (unlike
/// `NSLock.lock()`/`unlock()` under Swift 6 strict concurrency).
private final class StreamFactoryRecorder: Sendable {
    private struct State {
        var lastSinceSeq: Int?
        var lastKnownWorkspaceFingerprint: String?
        var startCount = 0
        var continuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    }
    private let state = OSAllocatedUnfairLock(initialState: State())

    var lastSinceSeq: Int? { state.withLock { $0.lastSinceSeq } }
    var lastKnownWorkspaceFingerprint: String? { state.withLock { $0.lastKnownWorkspaceFingerprint } }
    var startCount: Int { state.withLock { $0.startCount } }

    func make(sinceSeq: Int?, knownWorkspaceFingerprint: String?) -> SessionWorkspaceStreamSource {
        state.withLock {
            $0.lastSinceSeq = sinceSeq
            $0.lastKnownWorkspaceFingerprint = knownWorkspaceFingerprint
        }
        return SessionWorkspaceStreamSource(
            start: { [state] in
                // Do NOT auto-emit .connected: a real 401 stream never reaches
                // the connected event (the 401 is the HTTP response). The test
                // drives events explicitly to model the auth-failure path.
                AsyncStream { continuation in
                    state.withLock {
                        $0.startCount += 1
                        $0.continuation = continuation
                    }
                }
            },
            stop: { [state] in
                state.withLock {
                    $0.continuation?.finish()
                    $0.continuation = nil
                }
            },
            clockSkewMs: { 0 }
        )
    }

    func emitConnected() {
        let c = state.withLock { $0.continuation }
        c?.yield(.connected(SessionWorkspaceStream.Connected(session_id: "session-1", server_now_ms: nil)))
    }

    func emitUnauthorized() {
        let c = state.withLock { $0.continuation }
        c?.yield(.unauthorized)
    }

    func emitReplayGap(latestSeq: Int) {
        let c = state.withLock { $0.continuation }
        c?.yield(.replayGap(SessionWorkspaceStream.ReplayGap(
            session_id: "session-1",
            requested_seq: 777,
            earliest_seq: nil,
            latest_seq: latestSeq,
            reason: "buffer_unavailable"
        )))
    }

    func emitChanged(latestEventId: Int, pubsubSeq: Int?) {
        let c = state.withLock { $0.continuation }
        c?.yield(.changed(SessionWorkspaceStream.WorkspaceChanged(
            session_id: "session-1",
            latest_event_id: latestEventId,
            thread_session_count: 1,
            latest_event_emitted_at_ms: nil,
            server_fanout_at_ms: nil,
            server_now_ms: nil,
            pubsub_seq: pubsubSeq,
            transcript_preview: nil
        )))
    }
}

private actor FakeStreamResumeClient: SessionWorkspaceClient {
    private var workspaces: [SessionWorkspaceResponse]
    private var tailRequests = 0
    private var tailFailuresRemaining = 0

    init(workspaces: [SessionWorkspaceResponse]) {
        self.workspaces = workspaces
    }

    func tailRequestCount() -> Int { tailRequests }

    func failNextTailRequests(_ count: Int) {
        tailFailuresRemaining += max(0, count)
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        workspaces[0]
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse {
        tailRequests += 1
        if tailFailuresRemaining > 0 {
            tailFailuresRemaining -= 1
            throw URLError(.cannotConnectToHost)
        }
        let workspace = nextWorkspace()
        return SessionMobileTailResponse(
            session: workspace.session,
            projection: workspace.projection,
            snapshotEventId: workspace.events.map(\.id).max(),
            workspaceRevision: workspace.workspaceRevision
        )
    }

    private func nextWorkspace() -> SessionWorkspaceResponse {
        if workspaces.count > 1 {
            return workspaces.removeFirst()
        }
        return workspaces[0]
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse {
        SessionInputResponse(outcome: .sent, inputId: 1, clientRequestId: clientRequestId, intent: .auto, queued: [])
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

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}
}
