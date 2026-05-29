import Foundation
import Testing

@testable import Longhouse

/// M1 "No blank transcript on resume" guardrails: cold relaunch hydrates from
/// disk, a failed refresh degrades to a banner instead of erasing the
/// transcript, and backgrounding (pauseRealtime) keeps content on screen.
@MainActor
struct SessionResumeHydrationTests {
    private let serverURL = "https://example.longhouse.ai"

    private func tempDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-resume-tests-\(UUID().uuidString)", isDirectory: true)
    }

    private func seedDiskSnapshot(
        store: TranscriptSnapshotStore,
        workspace: SessionWorkspaceResponse,
        sessionId: String = "session-1"
    ) {
        store.save(
            serverURL: serverURL,
            sessionId: sessionId,
            detail: workspace.session,
            events: workspace.events,
            loadedProjectionItemCount: workspace.events.count,
            totalProjectionItemCount: workspace.projection.total,
            tailSnapshotEventId: workspace.events.map(\.id).max(),
            lastPubsubSeq: nil
        )
        store.waitForPendingWrites()
    }

    @Test
    func coldRelaunchHydratesFromDiskBeforeNetwork() async throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = TranscriptSnapshotStore(directory: dir)
        let cached = try makeWorkspace(eventId: 30, content: "Disk tail")
        seedDiskSnapshot(store: store, workspace: cached)

        let fresh = try makeWorkspace(eventId: 31, content: "Network tail")
        let api = BlockingResumeClient(workspace: fresh)
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            enableRealtime: false,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)

        // The network tail request is deliberately blocked, so this proves the
        // disk snapshot rendered before any server response could replace it.
        #expect(model.isInitialLoading == false)
        #expect(model.detail?.id == "session-1")
        #expect(model.items.map(\.id) == ["user:30"])
        #expect(model.errorMessage == nil)

        await api.waitUntilTailRequested()
        await api.releaseTail()
        #expect(await waitForItems(model, ["user:31"]))
    }

    @Test
    func coldRelaunchWithFailedRefreshKeepsTranscriptVisible() async throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = TranscriptSnapshotStore(directory: dir)
        let cached = try makeWorkspace(eventId: 30, content: "Disk tail")
        seedDiskSnapshot(store: store, workspace: cached)

        let api = FakeResumeClient(workspaces: [cached])
        await api.failFutureTails()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            enableRealtime: false,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)
        // Allow the background refresh task to run and fail.
        await waitForRefreshError(model)

        // The transcript must survive the failed refresh.
        #expect(model.items.map(\.id) == ["user:30"])
        // Full-screen blocking error must NOT be set (that's the lone triangle).
        #expect(model.errorMessage == nil)
        // The failure degrades to the non-destructive banner.
        #expect(model.refreshErrorMessage != nil)
    }

    @Test
    func backgroundResumeDoesNotEmptyItems() async throws {
        let before = try makeWorkspace(eventId: 40, content: "Loaded tail")
        let api = FakeResumeClient(workspaces: [before])
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            enableRealtime: false,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: nil
        )

        await model.start(sessionId: "session-1", appState: appState)
        #expect(model.items.map(\.id) == ["user:40"])

        // Simulate scene background.
        model.pauseRealtime()
        #expect(model.items.map(\.id) == ["user:40"], "pause must not erase the transcript")

        // Now the network goes bad and we resume to foreground.
        await api.failFutureTails()
        await model.start(sessionId: "session-1", appState: appState)
        await waitForRefreshError(model)

        // Same session resumed: content preserved, no blocking error.
        #expect(model.items.map(\.id) == ["user:40"])
        #expect(model.errorMessage == nil)
    }

    @Test
    func coldLoadWithNoCacheShowsBlockingErrorOnFailure() async throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = TranscriptSnapshotStore(directory: dir)
        // Nothing seeded on disk.
        let placeholder = try makeWorkspace(eventId: 1, content: "unused")
        let api = FakeResumeClient(workspaces: [placeholder])
        await api.failFutureTails()
        let appState = AppState()
        appState.serverURL = serverURL
        let model = SessionViewModel(
            apiFactory: { _ in api },
            enableRealtime: false,
            transcriptCache: SessionTranscriptCache(maxBytes: 0),
            snapshotStore: store
        )

        await model.start(sessionId: "session-1", appState: appState)

        // No cache anywhere → the blocking full-screen error is correct here.
        #expect(model.items.isEmpty)
        #expect(model.errorMessage != nil)
    }

    // MARK: - Helpers

    private func waitForRefreshError(_ model: SessionViewModel) async {
        for _ in 0..<50 {
            if model.refreshErrorMessage != nil { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func waitForItems(_ model: SessionViewModel, _ ids: [String]) async -> Bool {
        for _ in 0..<50 {
            if model.items.map(\.id) == ids { return true }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return false
    }

    nonisolated private func makeWorkspace(
        eventId: Int,
        content: String,
        total: Int = 1,
        pageOffset: Int = 0
    ) throws -> SessionWorkspaceResponse {
        try TestWorkspaceFactory.make(eventId: eventId, content: content, total: total, pageOffset: pageOffset)
    }
}

private actor FakeResumeClient: SessionWorkspaceClient {
    private var workspaces: [SessionWorkspaceResponse]
    private var shouldFailTails = false

    init(workspaces: [SessionWorkspaceResponse]) {
        self.workspaces = workspaces
    }

    func failFutureTails() {
        shouldFailTails = true
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        if shouldFailTails { throw URLError(.cannotConnectToHost) }
        return current()
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse {
        if shouldFailTails { throw URLError(.cannotConnectToHost) }
        let workspace = current()
        return SessionMobileTailResponse(
            session: workspace.session,
            projection: workspace.projection,
            snapshotEventId: workspace.events.map(\.id).max()
        )
    }

    private func current() -> SessionWorkspaceResponse {
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

private actor BlockingResumeClient: SessionWorkspaceClient {
    private let workspace: SessionWorkspaceResponse
    private var tailRequested = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var releaseContinuation: CheckedContinuation<Void, Never>?
    private var released = false

    init(workspace: SessionWorkspaceResponse) {
        self.workspace = workspace
    }

    func waitUntilTailRequested() async {
        if tailRequested { return }
        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }

    func releaseTail() {
        released = true
        releaseContinuation?.resume()
        releaseContinuation = nil
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        workspace
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse {
        tailRequested = true
        let pending = waiters
        waiters.removeAll()
        pending.forEach { $0.resume() }
        if !released {
            await withCheckedContinuation { continuation in
                releaseContinuation = continuation
            }
        }
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
