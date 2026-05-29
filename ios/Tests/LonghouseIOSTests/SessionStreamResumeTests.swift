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
            lastPubsubSeq: 777
        )
        store.waitForPendingWrites()

        let recorder = StreamFactoryRecorder()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _, sinceSeq in recorder.make(sinceSeq: sinceSeq) },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)
        // Let the stream task spin up.
        try? await Task.sleep(nanoseconds: 50_000_000)

        #expect(recorder.lastSinceSeq == 777)
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
            streamFactory: { _, _, sinceSeq in recorder.make(sinceSeq: sinceSeq) },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)
        let startsBefore = recorder.startCount

        // The active stream reports a 401.
        recorder.emitUnauthorized()
        try? await Task.sleep(nanoseconds: 100_000_000)

        // Exactly one refresh REST call and one stream restart.
        #expect(await api.tailRequestCount() >= 1)
        #expect(recorder.startCount == startsBefore + 1)

        // A second 401 on the restarted stream must NOT loop again — the guard
        // holds until a successful connect resets it.
        let startsAfterFirst = recorder.startCount
        recorder.emitUnauthorized()
        try? await Task.sleep(nanoseconds: 100_000_000)
        #expect(recorder.startCount == startsAfterFirst, "must not refresh-loop")

        // After a successful connect, the guard resets so a later 401 (e.g. a
        // cookie that expires mid-session) can refresh again.
        recorder.emitConnected()
        try? await Task.sleep(nanoseconds: 30_000_000)
        recorder.emitUnauthorized()
        try? await Task.sleep(nanoseconds: 100_000_000)
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
            streamFactory: { _, _, sinceSeq in recorder.make(sinceSeq: sinceSeq) },
            enableRealtime: true,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)

        // First 401 → one refresh + restart. Second 401 (no connect) is
        // suppressed and must drop the dead stream handle.
        recorder.emitUnauthorized()
        try? await Task.sleep(nanoseconds: 100_000_000)
        recorder.emitUnauthorized()
        try? await Task.sleep(nanoseconds: 100_000_000)
        let startsAfterSuppressed = recorder.startCount

        // A foreground resume of the same session must reattach the stream,
        // proving the dead handle was cleared (start() gates on streamTask==nil).
        await model.start(sessionId: "session-1", appState: appState)
        try? await Task.sleep(nanoseconds: 50_000_000)
        #expect(recorder.startCount == startsAfterSuppressed + 1, "resume should reattach a dead stream")

        model.stop()
    }
}

/// Records stream factory invocations and lets a test drive the live stream.
/// `OSAllocatedUnfairLock.withLock` is safe to call from async contexts (unlike
/// `NSLock.lock()`/`unlock()` under Swift 6 strict concurrency).
private final class StreamFactoryRecorder: Sendable {
    private struct State {
        var lastSinceSeq: Int?
        var startCount = 0
        var continuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    }
    private let state = OSAllocatedUnfairLock(initialState: State())

    var lastSinceSeq: Int? { state.withLock { $0.lastSinceSeq } }
    var startCount: Int { state.withLock { $0.startCount } }

    func make(sinceSeq: Int?) -> SessionWorkspaceStreamSource {
        state.withLock { $0.lastSinceSeq = sinceSeq }
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
}

private actor FakeStreamResumeClient: SessionWorkspaceClient {
    private var workspaces: [SessionWorkspaceResponse]
    private var tailRequests = 0

    init(workspaces: [SessionWorkspaceResponse]) {
        self.workspaces = workspaces
    }

    func tailRequestCount() -> Int { tailRequests }

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
        let workspace = workspaces[0]
        return SessionMobileTailResponse(
            session: workspace.session,
            projection: workspace.projection,
            snapshotEventId: workspace.events.map(\.id).max()
        )
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
