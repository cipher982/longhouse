import Foundation
import os
import Testing

@testable import Longhouse

@MainActor
struct TimelineViewModelConnectivityTests {
    private let serverURL = "https://example.longhouse.ai"

    @Test
    func streamDisconnectChurnDoesNotShowTimelineWarningWhenSnapshotsRecover() async {
        let session = makeSession()
        let api = FakeTimelineSessionsClient([
            .success([session]),
            .success([session]),
        ])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        #expect(model.connectionBanner == .none)

        model.startStream(using: appState)
        await waitUntil { stream.startCount() >= 1 }
        stream.emit(.connected)
        await drain()

        stream.emit(.disconnected(nil))
        await drain()
        #expect(model.connectionBanner == .none)

        stream.emit(.disconnected(URLError(.cancelled)))
        await drain()
        #expect(model.connectionBanner == .none)

        await model.refresh(using: appState, force: true)
        #expect(model.connectionBanner == .none)
        #expect(await api.requestCount() == 2)

        model.stopStream()
    }

    @Test
    func snapshotFailuresDriveBannerOnlyAfterDataIsStale() async {
        let session = makeSession()
        let api = FakeTimelineSessionsClient([
            .success([session]),
            .failure,
            .failure,
        ])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        let loadedAt = model.connectivity.lastUpdatedAt ?? Date()

        await model.refresh(using: appState, force: true)
        #expect(model.connectionBanner(at: loadedAt.addingTimeInterval(1)) == .none)

        await model.refresh(using: appState, force: true)
        #expect(model.connectivity.reachability == .degraded)
        #expect(model.connectionBanner(at: loadedAt.addingTimeInterval(181)) == .degraded)
    }

    @Test
    func emptySnapshotSuccessStillCountsAsFreshConnectivity() async {
        let api = FakeTimelineSessionsClient([.success([])])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        let loadedAt = model.connectivity.lastUpdatedAt ?? Date()

        #expect(model.connectivity.reachability == .reachable)
        #expect(!model.connectivity.hasLoadedData)
        #expect(model.connectivity.hasFreshnessEvidence)
        #expect(model.connectionBanner(at: loadedAt.addingTimeInterval(1)) == .none)
    }

    @Test
    func streamAuthFailureRefreshesSessionAndRestartsStream() async {
        let session = makeSession()
        let api = FakeTimelineSessionsClient([
            .success([session]),
            .success([session]),
        ])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        appState.isAuthenticated = true
        model.startStream(using: appState)
        await waitUntil { stream.startCount() >= 1 }
        let startCountBefore401 = stream.startCount()

        stream.emit(.disconnected(LonghouseAPIError.notAuthenticated))
        await waitUntil {
            await api.requestCount() == 2 && stream.startCount() == startCountBefore401 + 1
        }

        #expect(await api.requestCount() == 2)
        #expect(stream.startCount() == startCountBefore401 + 1)
        #expect(appState.isAuthenticated)
        #expect(model.connectionBanner == .none)
        #expect(model.connectivity.reachability == .reachable)

        model.stopStream()
    }

    @Test
    func streamAuthFailureShowsAuthRequiredOnlyAfterRefreshFails() async {
        let session = makeSession()
        let api = FakeTimelineSessionsClient([
            .success([session]),
            .notAuthenticated,
        ])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        appState.isAuthenticated = true
        model.startStream(using: appState)
        await waitUntil { stream.startCount() >= 1 }

        stream.emit(.disconnected(LonghouseAPIError.notAuthenticated))
        await waitUntil { !appState.isAuthenticated }

        #expect(model.connectionBanner == .authRequired)
        #expect(model.connectivity.reachability == .authRequired)
        #expect(!appState.isAuthenticated)
    }

    @Test
    func stoppingStreamForLifecycleDoesNotShowWarning() async {
        let session = makeSession()
        let api = FakeTimelineSessionsClient([.success([session])])
        let stream = TimelineStreamRecorder()
        let model = makeModel(api: api, stream: stream)
        let appState = makeAppState()

        await model.refresh(using: appState, force: true)
        model.startStream(using: appState)
        await waitUntil { stream.startCount() >= 1 }
        stream.emit(.connected)
        await drain()

        model.stopStream()
        await drain()

        #expect(model.connectionBanner == .none)
    }

    private func makeModel(
        api: FakeTimelineSessionsClient,
        stream: TimelineStreamRecorder
    ) -> TimelineViewModel {
        TimelineViewModel(
            apiFactory: { _ in api },
            streamFactory: { _, _ in stream.make() },
            enableRealtime: true,
            enableConnectivityClock: false
        )
    }

    private func makeAppState() -> AppState {
        let appState = AppState()
        appState.serverURL = serverURL
        return appState
    }

    /// Wait until `condition` holds, or give up after `budget`.
    ///
    /// Returns as soon as the condition is true, so a healthy test costs
    /// milliseconds; the budget only bounds how much scheduling delay is
    /// tolerated. This replaces a blind sleep for every assertion that waits
    /// for something to *happen* — those are the ones a loaded CI runner can
    /// starve, and the ones that fail rather than pass when starved.
    private func waitUntil(
        _ condition: () async -> Bool,
        budget: Duration = .seconds(10)
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: budget)
        while clock.now < deadline {
            if await condition() {
                return
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    /// Give the model a chance to process an event that should change nothing.
    ///
    /// Deliberately still time-based: you cannot wait for the absence of an
    /// effect. This is safe where a condition wait is not, because a too-short
    /// drain makes a "nothing changed" assertion pass rather than fail — it
    /// cannot manufacture the CI flakes a starved positive wait does.
    private func drain() async {
        try? await Task.sleep(nanoseconds: 50_000_000)
    }

    private func makeSession(id: String = "session-1") -> SessionSummary {
        let display = SessionRuntimeDisplay(
            truthTier: "live",
            signalTier: "live",
            state: "running",
            tone: "thinking",
            headline: "Thinking",
            detail: nil,
            phaseLabel: "Thinking",
            compactToolLabel: nil,
            isLive: true,
            isExecuting: true,
            needsAttention: false,
            isIdle: false,
            isStalled: false,
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: "live",
            lifecycle: "open",
            hostState: "online",
            terminalReason: nil
        )
        return SessionSummary(
            id: id,
            threadId: "thread-\(id)",
            title: "Timeline Session",
            presenceState: "running",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-06-02T14:00:00Z",
            summary: "Timeline connectivity test session.",
            userState: "active",
            status: nil,
            timelineAnchorAt: "2026-06-02T14:00:00Z",
            userMessages: 1,
            toolCalls: 1,
            liveControlAvailable: true,
            hostReattachAvailable: false,
            replyToLiveSessionAvailable: true,
            runtimeDisplay: display
        )
    }
}

private enum FakeTimelineResponse: Sendable {
    case success([SessionSummary])
    case failure
    case notAuthenticated
}

private actor FakeTimelineSessionsClient: TimelineSessionsClient {
    private var responses: [FakeTimelineResponse]
    private var requests = 0

    init(_ responses: [FakeTimelineResponse]) {
        self.responses = responses
    }

    func requestCount() -> Int {
        requests
    }

    func recentSessions(limit: Int) async throws -> [SessionSummary] {
        requests += 1
        guard !responses.isEmpty else { return [] }
        switch responses.removeFirst() {
        case .success(let sessions):
            return sessions
        case .failure:
            throw URLError(.timedOut)
        case .notAuthenticated:
            throw LonghouseAPIError.notAuthenticated
        }
    }
}

private final class TimelineStreamRecorder: Sendable {
    private struct State {
        var continuation: AsyncStream<TimelineSessionsStream.Event>.Continuation?
        var startCount = 0
    }

    private let state = OSAllocatedUnfairLock(initialState: State())

    func make() -> TimelineSessionsStreamSource {
        TimelineSessionsStreamSource(
            start: { [state] in
                AsyncStream { continuation in
                    state.withLock {
                        $0.continuation = continuation
                        $0.startCount += 1
                    }
                }
            },
            stop: { [state] in
                state.withLock {
                    $0.continuation?.finish()
                    $0.continuation = nil
                }
            }
        )
    }

    func emit(_ event: TimelineSessionsStream.Event) {
        let continuation = state.withLock { $0.continuation }
        continuation?.yield(event)
    }

    func startCount() -> Int {
        state.withLock { $0.startCount }
    }
}
