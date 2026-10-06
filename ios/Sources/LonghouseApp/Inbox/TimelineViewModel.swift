import OSLog
import SwiftUI
import WidgetKit

protocol TimelineSessionsClient: Sendable {
    func recentSessions(limit: Int, deviceId: String?) async throws -> [SessionSummary]
    func searchSessions(
        query: String,
        lane: TimelineSearchLane,
        daysBack: Int?,
        limit: Int,
        deviceId: String?
    ) async throws -> [SessionSummary]
}

extension LonghouseAPI: TimelineSessionsClient {}

struct TimelineSessionsStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<TimelineSessionsStream.Event>
    let stop: @Sendable () async -> Void

    static func live(baseURL: URL, limit: Int, deviceId: String? = nil) -> TimelineSessionsStreamSource {
        let stream = TimelineSessionsStream(baseURL: baseURL, limit: limit, deviceId: deviceId)
        return TimelineSessionsStreamSource(
            start: { await stream.start() },
            stop: { await stream.stop() }
        )
    }
}

/// Four-way state for the timeline screen. Replaces the prior cluster of
/// `isInitialLoading` / `errorMessage` / `recent.isEmpty` booleans, which
/// allowed nonsense combinations (loading + error + data) and forced the
/// view body to re-derive the state from if-else order.
enum TimelineLoadState: Equatable {
    case initial
    case empty
    case error(String)
    case loaded([SessionSummary])
}

enum TimelineSearchState: Equatable {
    case idle
    case loading
    case empty
    case error(String)
    case loaded([SessionSummary])
}

@MainActor
final class TimelineViewModel: ObservableObject {
    @Published private(set) var state: TimelineLoadState = .initial
    @Published private(set) var searchState: TimelineSearchState = .idle
    /// Which lane produced ``searchState``'s results. Nil before the server has
    /// been asked, and cleared whenever the query changes.
    @Published private(set) var searchLane: TimelineSearchLane?
    @Published private(set) var connectivity = TimelineConnectivityState()
    @Published private(set) var connectivityNow = Date()

    private var streamTask: Task<Void, Never>?
    private var stream: TimelineSessionsStreamSource?
    private var reconcileTask: Task<Void, Never>?
    private var persistTask: Task<Void, Never>?
    private var connectivityClockTask: Task<Void, Never>?
    private var lastWidgetReloadAt: Date?
    private var isRefreshInFlight = false
    private var loggedFirstPaint = false
    private var streamGeneration: UInt64 = 0
    private var hasReceivedFirstConnect = false
    private var streamAuthRefreshAttempted = false
    private var searchGeneration: UInt64 = 0
    private var searchTask: Task<Void, Never>?
    private var lastSearchQuery: String?
    private var lastSearchLane: TimelineSearchLane?
    private let apiFactory: (String) -> TimelineSessionsClient?
    private let streamFactory: (URL, Int, String?) -> TimelineSessionsStreamSource
    private let deviceId: String?
    private let enableRealtime: Bool
    private let enableConnectivityClock: Bool
    private let limit = 40
    // Search reaches the corpus the list does not hold. iOS has no
    // date-range picker of its own, so this stays nil: the server searches
    // all indexed history, the same default web and the machine API use.
    private let searchDaysBack: Int? = nil
    private let searchLimit = 30
    private let reconcileIntervalNanoseconds: UInt64 = 120_000_000_000 // 120s safety net
    private let connectivityClockIntervalNanoseconds: UInt64 = 15_000_000_000 // 15s freshness tick
    private let persistDebounceNanoseconds: UInt64 = 250_000_000 // 250ms cache/widget coalesce
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Timeline")

    var connectionBanner: TimelineConnectivityBanner {
        connectivity.banner(at: connectivityNow)
    }

    init(
        apiFactory: @escaping (String) -> TimelineSessionsClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, Int, String?) -> TimelineSessionsStreamSource = { baseURL, limit, deviceId in
            TimelineSessionsStreamSource.live(baseURL: baseURL, limit: limit, deviceId: deviceId)
        },
        enableRealtime: Bool = true,
        enableConnectivityClock: Bool = true,
        deviceId: String? = nil
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
        self.enableConnectivityClock = enableConnectivityClock
        self.deviceId = deviceId
    }

    func connectionBanner(at now: Date) -> TimelineConnectivityBanner {
        connectivity.banner(at: now)
    }

    private var isInitial: Bool {
        if case .initial = state { return true }
        return false
    }

    private var hasLoadedSessions: Bool {
        if case .loaded = state { return true }
        return false
    }
    private func scopedSessions(_ sessions: [SessionSummary], fromScopedEndpoint: Bool = false) -> [SessionSummary] {
        guard let deviceId, !deviceId.isEmpty else { return sessions }
        // A scoped server response can admit storage rows by machine_id while
        // their display metadata omits device_id. The global cache cannot.
        return sessions.filter { $0.deviceId == deviceId || (fromScopedEndpoint && $0.deviceId == nil) }
    }

    func load(using appState: AppState) async {
        startConnectivityClock()
        guard isInitial else {
            // A pushed detail stops this screen's stream. Refresh on return so
            // a read acknowledgement emitted while the detail was visible is
            // reflected immediately instead of waiting for the safety poll.
            await refresh(using: appState, reloadWidget: true)
            return
        }
        if let cached = TimelineCacheStore.load(serverURL: appState.serverURL) {
            let cachedSessions = scopedSessions(cached.sessions)
            applySessions(cachedSessions, source: "cache")
            applyConnectivity(.cacheLoaded(hasLoadedData: !cachedSessions.isEmpty, savedAt: cached.savedAt))
            logger.info("timeline cache hit sessions=\(cachedSessions.count, privacy: .public)")
            Task { [weak self] in
                await self?.refresh(using: appState, reloadWidget: true)
            }
            return
        }
        logger.info("timeline cache miss")
        await refresh(using: appState, reloadWidget: true)
    }

    /// Back to "the server lane has not been asked". Called on every keystroke:
    /// the answer on screen belongs to the previous query, so it is dropped
    /// rather than left standing under a new one.
    func clearSearch() {
        cancelRemoteSearch()
    }

    func cancelRemoteSearch() {
        searchGeneration &+= 1
        searchTask?.cancel()
        searchTask = nil
        searchLane = nil
        searchState = .idle
    }

    /// Ask the server lane for the query currently on screen.
    ///
    /// Returns immediately; the work runs in ``searchTask`` so a keystroke can
    /// cancel it, and so the caller's button press never blocks the UI. The
    /// timeline keeps its own rows for the whole duration of this call.
    func searchRemote(query: String, lane: TimelineSearchLane, using appState: AppState) {
        let normalized = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            cancelRemoteSearch()
            return
        }
        searchTask?.cancel()
        searchGeneration &+= 1
        let generation = searchGeneration
        lastSearchQuery = normalized
        lastSearchLane = lane
        searchLane = lane
        searchState = .loading

        searchTask = Task { [weak self] in
            guard let self else { return }
            guard let api = self.apiFactory(appState.serverURL) else {
                self.searchState = .error("Invalid server URL")
                return
            }
            do {
                let sessions = self.scopedSessions(try await api.searchSessions(
                    query: normalized,
                    lane: lane,
                    daysBack: self.searchDaysBack,
                    limit: self.searchLimit,
                    deviceId: self.deviceId
                ), fromScopedEndpoint: true)
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                self.searchState = sessions.isEmpty ? .empty : .loaded(sessions)
            } catch LonghouseAPIError.notAuthenticated {
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                self.applyConnectivity(.authFailed)
                appState.handleExpiredSession()
                self.searchState = .error("Sign in to search your sessions.")
            } catch {
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                let message = self.connectionBanner == .offline
                    ? "Search needs a connection."
                    : "Search failed: \(error.localizedDescription)"
                self.searchState = .error(message)
            }
        }
    }

    /// Await the in-flight server lane, for pull-to-refresh.
    func awaitRemoteSearch() async {
        await searchTask?.value
    }

    /// Repeat the last lane for the last query, so a retry after a failure or a
    /// pull-to-refresh does not silently change what is being asked.
    func retrySearch(using appState: AppState) {
        guard let query = lastSearchQuery, let lane = lastSearchLane else { return }
        searchRemote(query: query, lane: lane, using: appState)
    }

    func refresh(using appState: AppState, reloadWidget: Bool = false, force: Bool = false) async {
        if isRefreshInFlight && !force {
            logger.debug("timeline refresh skipped reason=in_flight")
            return
        }
        guard let api = apiFactory(appState.serverURL) else {
            state = .error("Invalid server URL")
            return
        }
        let startedAt = Date()
        let generation = streamGeneration
        isRefreshInFlight = true
        defer { isRefreshInFlight = false }

        do {
            let sessions = scopedSessions(try await api.recentSessions(limit: limit, deviceId: deviceId), fromScopedEndpoint: true)
            // Drop stale snapshots from a previous stream lifetime — a slow
            // reconnect bootstrap mustn't overwrite newer stream-applied state.
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            let attentionIds = Set(sessions.filter(\.needsAttention).map(\.id))
            applySessions(sessions, source: "network")
            applyConnectivity(.snapshotSucceeded(hasLoadedData: !sessions.isEmpty))
            // Machine-scoped timelines must not overwrite the all-machine
            // cache or widget snapshot with a narrowed projection.
            if deviceId == nil {
                schedulePersist(sessions: sessions, appState: appState)
                PushNotificationStore.removeResolvedAttentionNotifications(activeSessionIDs: attentionIds)
                if reloadWidget {
                    reloadWidgetTimelineIfNeeded()
                }
            }
            logger.info("timeline refresh finished sessions=\(sessions.count, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch LonghouseAPIError.notAuthenticated {
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh auth failure dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            applyConnectivity(.authFailed)
            appState.handleExpiredSession()
            logger.error("timeline refresh unauthenticated elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch {
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh failure dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            applyConnectivity(.snapshotFailed)
            // While we have data on screen, refresh failures are silent —
            // the connection strip is the signal. Only surface an error
            // page when there's nothing else to show.
            if !hasLoadedSessions {
                state = .error("Couldn't load sessions: \(error.localizedDescription)")
            }
            logger.error("timeline refresh failed elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    func resumeStream(using appState: AppState) {
        startStream(using: appState)
        guard !isInitial else { return }
        Task { await refresh(using: appState, reloadWidget: true) }
    }

    func startStream(using appState: AppState) {
        guard enableRealtime else { return }
        startConnectivityClock()
        guard streamTask == nil else { return }
        guard let baseURL = URL(string: appState.serverURL) else {
            logger.error("timeline stream invalid serverURL=\(appState.serverURL, privacy: .public)")
            return
        }
        streamGeneration &+= 1
        let generation = streamGeneration
        hasReceivedFirstConnect = false
        let stream = streamFactory(baseURL, limit, deviceId)
        self.stream = stream
        streamTask = Task { [weak self] in
            let events = await stream.start()
            for await event in events {
                guard let self else { break }
                await self.handleStreamEvent(event, generation: generation, appState: appState)
            }
            // Stream ended (cancellation or terminal 401). Clear the slot
            // so resumeStream / scenePhase can spin up a new task.
            self?.streamLoopDidExit(generation: generation)
        }
        startReconcileSafetyNet(using: appState, generation: generation)
    }

    func stopStream() {
        // Bump generation first so any event already in flight is dropped
        // by the guard in handleStreamEvent before it can mutate state.
        streamGeneration &+= 1
        applyConnectivity(.lifecycleStopped)
        streamTask?.cancel()
        streamTask = nil
        if let stream {
            Task { await stream.stop() }
        }
        stream = nil
        reconcileTask?.cancel()
        reconcileTask = nil
        stopConnectivityClock()
        // Flush any pending debounced cache/widget save before tearing down
        // so a fast stream stop (scene background) doesn't drop the last
        // snapshot. The detached task in schedulePersist already snapshots
        // sessions by value, so flushing == waiting for it to finish.
        if let pending = persistTask {
            persistTask = nil
            Task { await pending.value }
        }
    }

    private func streamLoopDidExit(generation: UInt64) {
        guard generation == streamGeneration else { return }
        streamTask = nil
    }

    private func startReconcileSafetyNet(using appState: AppState, generation: UInt64) {
        reconcileTask?.cancel()
        let interval = reconcileIntervalNanoseconds
        reconcileTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: interval)
                if Task.isCancelled { break }
                guard let self else { break }
                if self.streamGenerationMatches(generation) {
                    await self.refresh(using: appState, reloadWidget: true)
                } else {
                    break
                }
            }
        }
    }

    private func streamGenerationMatches(_ generation: UInt64) -> Bool {
        generation == streamGeneration
    }

    private func handleStreamEvent(
        _ event: TimelineSessionsStream.Event,
        generation: UInt64,
        appState: AppState
    ) async {
        guard generation == streamGeneration else { return }
        switch event {
        case .connected:
            // Reconnects need a snapshot resync because the stream has no
            // Last-Event-ID replay. The very first connect is already
            // covered by the `load()` REST bootstrap, so skip it. Don't
            // stamp data freshness on reconnects — wait until the bootstrap
            // actually lands so the banner doesn't lie.
            streamAuthRefreshAttempted = false
            if hasReceivedFirstConnect {
                applyConnectivity(.streamSignal(.reconnected), generation: generation)
                logger.info("timeline stream reconnected — bootstrapping snapshot")
                await refresh(using: appState, reloadWidget: true)
            } else {
                hasReceivedFirstConnect = true
                applyConnectivity(.streamSignal(.firstConnected), generation: generation)
            }
        case .admissionSnapshot(let runtimeEpoch, let admission):
            applyConnectivity(
                .servingEvidence(.admission(admission, runtimeEpoch: runtimeEpoch)),
                generation: generation
            )
        case .hostLifecycle(let lifecycle):
            applyConnectivity(.hostLifecycle(lifecycle), generation: generation)
        case .upsert(let card, _, _):
            if let deviceId, let eventDeviceId = card.sessionSummary.deviceId, eventDeviceId != deviceId { return }
            applyUpsert(card.sessionSummary, appState: appState)
            applyConnectivity(.streamSignal(.upsert), generation: generation)
        case .remove(let threadId, _, _):
            applyRemove(threadId: threadId, appState: appState)
            applyConnectivity(.streamSignal(.remove), generation: generation)
        case .heartbeat:
            applyConnectivity(.streamSignal(.heartbeat), generation: generation)
        case .disconnected(let error):
            let reason = classifyStreamDisconnect(error)
            if reason == .authFailure {
                await handleStreamAuthFailure(generation: generation, appState: appState)
            } else {
                applyConnectivity(.streamDisconnected(reason), generation: generation)
            }
            logger.info("timeline stream disconnected reason=\(String(describing: reason), privacy: .public) error=\(error?.localizedDescription ?? "nil", privacy: .public)")
        }
    }

    private func handleStreamAuthFailure(generation: UInt64, appState: AppState) async {
        guard generation == streamGeneration else { return }
        guard !streamAuthRefreshAttempted else {
            applyConnectivity(.streamDisconnected(.authFailure), generation: generation)
            return
        }
        streamAuthRefreshAttempted = true

        await refresh(using: appState, reloadWidget: true, force: true)
        guard generation == streamGeneration,
              appState.isAuthenticated,
              connectivity.reachability == .reachable else { return }

        streamTask = nil
        stream = nil
        startStream(using: appState)
    }

    private func applyConnectivity(
        _ event: TimelineConnectivityEvent,
        now: Date = Date(),
        generation: UInt64? = nil
    ) {
        let previousHostUpdate = connectivity.hostUpdate
        var next = connectivity
        if let generation {
            next.apply(event, now: now, eventGeneration: generation, currentGeneration: streamGeneration)
        } else {
            next.apply(event, now: now)
        }
        connectivity = next
        connectivityNow = now
        if next.hostUpdate != previousHostUpdate {
            restartConnectivityClock()
        }
    }

    private func startConnectivityClock() {
        guard enableConnectivityClock, connectivityClockTask == nil else { return }
        let defaultInterval = TimeInterval(connectivityClockIntervalNanoseconds) / 1_000_000_000
        connectivityClockTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { break }
                let now = Date()
                let interval = self.connectivity.hostUpdate.nextClockInterval(at: now, default: defaultInterval)
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                if Task.isCancelled { break }
                self.tickConnectivityClock()
            }
        }
    }

    private func restartConnectivityClock() {
        guard enableConnectivityClock else { return }
        connectivityClockTask?.cancel()
        connectivityClockTask = nil
        startConnectivityClock()
    }

    private func stopConnectivityClock() {
        connectivityClockTask?.cancel()
        connectivityClockTask = nil
    }

    private func tickConnectivityClock() {
        connectivityNow = Date()
    }

    private func classifyStreamDisconnect(_ error: Error?) -> StreamDisconnectReason {
        guard let error else { return .serverEOF }
        if error is CancellationError { return .cancelled }
        if let apiError = error as? LonghouseAPIError, case .notAuthenticated = apiError {
            return .authFailure
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return .cancelled
            case .notConnectedToInternet, .networkConnectionLost, .cannotFindHost,
                 .cannotConnectToHost, .dnsLookupFailed, .timedOut:
                return .networkError
            default:
                return .unknown
            }
        }
        return .unknown
    }

    private func applyUpsert(_ session: SessionSummary, appState: AppState) {
        var current = currentSessions()
        let incomingThread = session.threadId
        // Match either by thread (when both sides have one) or by head id —
        // pre-stream cached rows can have threadId == nil and would otherwise
        // duplicate or fail to delete until the next REST bootstrap. Also
        // sweep on head id always, so a legacy row without threadId still
        // gets replaced when a stream upsert with a threadId arrives for it.
        current.removeAll { existing in
            if existing.id == session.id { return true }
            if let incomingThread, let existingThread = existing.threadId {
                return existingThread == incomingThread
            }
            return false
        }
        current.append(session)
        // Keys first: parsing inside the comparator re-parsed each row's
        // date O(log n) times per upsert, on the main thread. The key is the
        // frozen display time, not the moving card anchor, so an upsert for a
        // session touches only that row's position, never everyone else's.
        current = timelineDisplayOrder(current)
        current = SessionSummary.residentCap(current, limit: limit)
        applySessions(current, source: "stream")
        if deviceId == nil {
            schedulePersist(sessions: current, appState: appState)
            reloadWidgetTimelineIfNeeded()
        }
    }

    private func applyRemove(threadId: String, appState: AppState) {
        var current = currentSessions()
        let before = current.count
        // Match by threadId when present, otherwise fall back to head id —
        // legacy cached rows without a threadId still need to be reachable.
        current.removeAll { existing in
            if let existingThread = existing.threadId {
                return existingThread == threadId
            }
            return existing.id == threadId
        }
        guard current.count != before else { return }
        applySessions(current, source: "stream")
        if deviceId == nil {
            schedulePersist(sessions: current, appState: appState)
            reloadWidgetTimelineIfNeeded()
        }
    }

    private func currentSessions() -> [SessionSummary] {
        if case .loaded(let sessions) = state { return sessions }
        return []
    }

    /// Coalesce cache + widget-snapshot disk writes. Stream upsert/remove
    /// bursts (5–20/sec during an active session) would otherwise hit the
    /// main actor with synchronous JSON-encode + file writes per event.
    private func schedulePersist(sessions: [SessionSummary], appState: AppState) {
        persistTask?.cancel()
        let serverURL = appState.serverURL
        let delay = persistDebounceNanoseconds
        persistTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            if Task.isCancelled { return }
            await Task.detached(priority: .utility) {
                TimelineCacheStore.save(sessions: sessions, serverURL: serverURL)
                WidgetSessionSnapshotStore.save(sessions: sessions)
            }.value
            _ = self
        }
    }

    private func reloadWidgetTimelineIfNeeded() {
        let now = Date()
        guard lastWidgetReloadAt == nil || now.timeIntervalSince(lastWidgetReloadAt!) > 60 else {
            return
        }
        WidgetCenter.shared.reloadAllTimelines()
        lastWidgetReloadAt = now
    }

    private func applySessions(_ sessions: [SessionSummary], source: String) {
        state = sessions.isEmpty ? .empty : .loaded(sessions)
        if !loggedFirstPaint {
            loggedFirstPaint = true
            logger.info("timeline first paint source=\(source, privacy: .public) sessions=\(sessions.count, privacy: .public)")
        }
    }
}
