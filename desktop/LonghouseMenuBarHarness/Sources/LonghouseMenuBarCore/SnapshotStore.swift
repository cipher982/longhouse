import Foundation
import SwiftUI

public enum SnapshotRefreshReason: Sendable {
    case initial
    case background
    case manual
}

@MainActor
public final class SnapshotStore: ObservableObject {
    public static let historyRetentionMinutes = 30
    public static let bootGraceSeconds: TimeInterval = 10
    public static let presentationRefreshFreshnessSeconds: TimeInterval = 10
    /// Backstop for a producer that goes quiet without reporting a failure.
    /// The primary mechanism is the outcome of the last completed attempt; this
    /// only catches a wedged refresh that never completes at all.
    public static let defaultRefreshDeadlineSeconds: TimeInterval = 120
    /// How long Runtime Host presentation and control stay authoritative after
    /// the projection stream last succeeded.
    public static let defaultProjectionDeadlineSeconds: TimeInterval = 300
    /// Refresh is event driven from the engine's status file, and a Mac that
    /// is not signed in yet has no engine writing one. Sign-in finishes in
    /// Terminal and the browser, so without this the window kept "Finish
    /// setup on this Mac" after the machine was connected and healthy, until
    /// a manual Refresh. `longhouse local-health --json` costs ~30 ms.
    public static let defaultSetupRefreshIntervalSeconds: TimeInterval = 5
    /// Session phase/ledger churn can arrive twice a second. Its current
    /// projection is applied immediately; a full producer snapshot only needs
    /// to catch up every few phase updates, keeping non-critical health fresh
    /// within five seconds without re-running local-health for each write.
    static let localStatusRefreshCoalescingSeconds: TimeInterval = 5

    /// How often relative age labels ("updated 12s ago") move while a panel is
    /// visible. Only while a presentation consumer exists; hidden panels never
    /// tick.
    public static let presentationUpdateIntervalSeconds: TimeInterval = 5

    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var history: [SnapshotHistorySample]
    @Published public private(set) var loadError: String?
    /// How the configured producer command has been behaving. The only clock
    /// permitted to decide whether displayed data is current.
    @Published public private(set) var refreshState: ProducerRefreshState
    /// How the Runtime Host session projection stream has been behaving.
    ///
    /// Tracked separately because it is an independent authority with an
    /// independent failure mode: the stream retries forever without surfacing
    /// anything, so a healthy producer can coexist with hours-old presentation
    /// and control data.
    @Published public private(set) var projectionState: ProducerRefreshState
    @Published public private(set) var isInitialLoading: Bool
    @Published public private(set) var isManualRefreshActive: Bool
    @Published public private(set) var isBooting: Bool
    @Published public private(set) var isRecovering: Bool
    @Published public private(set) var presentationDate: Date
    @Published public private(set) var feedback: HealthActionFeedback?

    private let source: any HealthSnapshotSource
    private var refreshTask: Task<Void, Never>?
    private var bootGraceTask: Task<Void, Never>?
    private var realtimeTask: Task<Void, Never>?
    private var realtimeFlushTask: Task<Void, Never>?
    private var transientRetryTask: Task<Void, Never>?
    private var setupFollowUpTask: Task<Void, Never>?
    /// Producer refresh before `refreshDeadline` lapses. Engine pulses that
    /// change nothing no longer trigger a refresh, so without it a healthy,
    /// quiet machine with no `--refresh-seconds` aged into "refresh is
    /// failing".
    private var trustBackstopTask: Task<Void, Never>?
    /// One deferred refresh for local-status churn that arrived inside the
    /// coalescing window (or during a refresh), so the last change is never
    /// left to the trust backstop.
    private var trailingChurnRefreshTask: Task<Void, Never>?
    private var realtimeConnection: RealtimeConnectionSnapshot?
    private var realtimeSessionIds: [String] = []
    private var pendingRealtimeProjections: [String: SessionProjection] = [:]
    private var pendingRealtimeRemovals: Set<String> = []
    private var localStatusMonitor: LocalStatusMonitor?
    private var latestLocalProjection: LocalStatusMonitor.Projection?
    private var localStatusPath: String?
    private var activeRefreshReason: SnapshotRefreshReason?
    private var queuedManualRefresh = false
    private var lastLocalStatusRefreshAtUptime: TimeInterval?
    private var presentationTimer: Timer?
    private var presentationConsumerCount = 0
    private let cacheURL: URL?
    private var lastCachedSnapshotData: Data?
    private var lastCachedSnapshotHeadline: [String]?
    private var lastCachedSnapshotWriteUptime: TimeInterval?
    private let transientRetryDelay: TimeInterval
    private let setupRefreshInterval: TimeInterval
    private let refreshDeadline: TimeInterval
    /// The stream is long-lived and mostly idle, so its authority lease is more
    /// generous than the producer's polling deadline.
    private let projectionDeadline: TimeInterval
    private static let historyRetentionSeconds: TimeInterval = Double(historyRetentionMinutes * 60)
    private static let maxHistorySamples = 180

    public init(
        source: any HealthSnapshotSource,
        cacheURL: URL? = nil,
        transientRetryDelay: TimeInterval = 2,
        refreshDeadline: TimeInterval = SnapshotStore.defaultRefreshDeadlineSeconds,
        projectionDeadline: TimeInterval = SnapshotStore.defaultProjectionDeadlineSeconds,
        setupRefreshInterval: TimeInterval = SnapshotStore.defaultSetupRefreshIntervalSeconds
    ) {
        self.source = source
        self.cacheURL = cacheURL ?? Self.defaultCacheURL(for: source)
        self.transientRetryDelay = transientRetryDelay
        self.setupRefreshInterval = setupRefreshInterval
        self.refreshDeadline = refreshDeadline
        self.projectionDeadline = projectionDeadline
        self.refreshState = .neverAttempted
        self.projectionState = .neverAttempted
        self.history = []
        self.isInitialLoading = false
        self.isManualRefreshActive = false
        self.isBooting = false
        self.isRecovering = false
        self.presentationDate = Date()
        self.feedback = nil
        // A cached snapshot is last-known evidence, never a producer success.
        // `refreshState` deliberately stays `.neverAttempted` here: restoring the
        // cache must not make the app believe it has current data.
        if let cachedSnapshot = Self.loadCachedSnapshot(from: self.cacheURL) {
            snapshot = cachedSnapshot.snapshot
            lastCachedSnapshotData = cachedSnapshot.data
            appendHistorySample(for: cachedSnapshot.snapshot)
            loadError = nil
        }
        if let cliSource = source as? CLIHealthSnapshotSource {
            if cliSource.usesDefaultInvocation,
               snapshot == nil,
               let nativeSnapshot = Self.loadNativeBootstrapSnapshot() {
                snapshot = nativeSnapshot
                appendHistorySample(for: nativeSnapshot)
            }
            isBooting = true
            scheduleBootGraceTimeout()
            refresh(reason: .initial)
        } else {
            do {
                let loadedSnapshot = try source.load()
                snapshot = loadedSnapshot
                appendHistorySample(for: loadedSnapshot)
                persistCachedSnapshot(loadedSnapshot)
                loadError = nil
                refreshState = refreshState.recordingSuccess(at: Date())
            } catch {
                loadError = error.localizedDescription
                refreshState = refreshState.recordingFailure(
                    ProducerRefreshFailure(
                        message: error.localizedDescription,
                        command: source.describedCommand,
                        observedAt: Date()
                    )
                )
            }
        }
    }

    // Freeze fixture labels without changing the wall clock that governs
    // producer/projection trust in the live window host.
    var snapshotPresentationDate: Date {
        if source is FixtureHealthSnapshotSource, let collectedAt = snapshot?.collectedAtDate {
            return collectedAt
        }
        return presentationDate
    }
    /// Applies the latest local evidence without publishing a new panel snapshot.
    /// Clock-only file updates are ingested immediately and become visible on
    /// the next presentation invalidation.
    public func snapshotForPresentation(relativeTo date: Date = Date()) -> HealthSnapshot? {
        guard let snapshot, let latestLocalProjection else {
            return snapshot
        }
        return snapshot.applyingLocalProjection(latestLocalProjection, relativeTo: date)
    }

    /// How much the app may trust what it is currently displaying.
    ///
    /// Derived only from producer refresh outcomes. Callers pass the current
    /// date rather than reading a stored value so the verdict cannot itself go
    /// stale between refreshes.
    public func dataTrust(relativeTo date: Date = Date()) -> DataTrust {
        refreshState.trust(relativeTo: date, deadline: refreshDeadline)
    }

    /// Whether a failure may still be presented as a quiet retry rather than a
    /// visible problem.
    ///
    /// Bounded on purpose. A hung producer reports `.timedOut` on every attempt,
    /// and `.timedOut` is transient, so gating the banner on `isRecovering`
    /// alone would keep a permanently broken producer looking normal forever.
    public var isBrieflyRecovering: Bool {
        isRecovering && refreshState.isWithinTransientGrace
    }

    /// Whether Runtime Host presentation and control may be shown as current.
    ///
    /// `.neverLoaded` is normal and benign here: a machine with no `realtime`
    /// block, or no managed sessions, never opens a stream. Only `.lastKnown`
    /// means the stream had authority and lost it.
    public func projectionTrust(relativeTo date: Date = Date()) -> DataTrust {
        projectionState.trust(relativeTo: date, deadline: projectionDeadline)
    }

    /// A machine with no managed session projection has no stream lease to
    /// expire. Treat that ordinary case as current so local transport health is
    /// not made unknown merely because the stream was never needed.
    public func projectionTrustForPresentation(relativeTo date: Date = Date()) -> DataTrust {
        guard let snapshot,
              snapshot.realtime != nil,
              !(snapshot.managedSessions ?? []).isEmpty else {
            return .current
        }
        return projectionTrust(relativeTo: date)
    }

    deinit {
        refreshTask?.cancel()
        bootGraceTask?.cancel()
        realtimeTask?.cancel()
        realtimeFlushTask?.cancel()
        transientRetryTask?.cancel()
        setupFollowUpTask?.cancel()
        trustBackstopTask?.cancel()
        trailingChurnRefreshTask?.cancel()
        localStatusMonitor?.stop()
    }

    private func scheduleBootGraceTimeout() {
        bootGraceTask?.cancel()
        bootGraceTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.bootGraceSeconds))
            guard !Task.isCancelled, let self else {
                return
            }
            self.isBooting = false
        }
    }

    private func exitBootingIfReady(for snapshot: HealthSnapshot) {
        guard isBooting else {
            return
        }
        if snapshot.parsedSeverity == .green {
            isBooting = false
            bootGraceTask?.cancel()
            bootGraceTask = nil
        }
    }

    public func refresh(reason: SnapshotRefreshReason = .background) {
        if let activeRefreshReason {
            if reason == .manual && activeRefreshReason != .manual {
                queuedManualRefresh = true
                isManualRefreshActive = true
            }
            return
        }

        startRefresh(reason: reason)
    }

    public func refreshForPresentation(
        maxSnapshotAge: TimeInterval = SnapshotStore.presentationRefreshFreshnessSeconds,
        referenceDate: Date = Date()
    ) {
        guard activeRefreshReason == nil else {
            return
        }
        guard let snapshot,
              let collectedAt = snapshot.collectedAtDate
        else {
            refresh(reason: .background)
            return
        }

        if referenceDate.timeIntervalSince(collectedAt) >= maxSnapshotAge {
            refresh(reason: .background)
        }
    }

    public func beginPresentationUpdates() {
        presentationConsumerCount += 1
        let now = Date()
        presentationDate = now
        refreshLocalPresentation(relativeTo: now)
        armPresentationDeadline()
    }

    public func endPresentationUpdates() {
        presentationConsumerCount = max(0, presentationConsumerCount - 1)
        guard presentationConsumerCount == 0 else {
            return
        }

        presentationTimer?.invalidate()
        presentationTimer = nil
    }

    private func armPresentationDeadline() {
        presentationTimer?.invalidate()
        presentationTimer = nil
        guard presentationConsumerCount > 0,
              let deadline = nextPresentationDeadline(relativeTo: Date()) else {
            return
        }

        let timer = Timer(
            timeInterval: max(0.01, deadline.timeIntervalSinceNow),
            repeats: false
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.presentationConsumerCount > 0 else { return }
                let now = Date()
                self.presentationDate = now
                self.refreshLocalPresentation(relativeTo: now)
                self.armPresentationDeadline()
            }
        }
        timer.tolerance = 0
        RunLoop.main.add(timer, forMode: .common)
        presentationTimer = timer
    }

    private func nextPresentationDeadline(relativeTo now: Date) -> Date? {
        var deadlines: [Date] = []
        func append(_ date: Date?) {
            if let date, date > now {
                deadlines.append(date)
            }
        }
        func appendThreshold(_ raw: String?, seconds: TimeInterval) {
            guard let raw, let date = HealthSnapshot.parseISO8601(raw) else { return }
            append(date.addingTimeInterval(seconds))
        }

        // Relative age labels advance while the panel is visible
        // (`armPresentationDeadline` only runs with a consumer). Anchored to
        // the last presentation date, not to "now": re-arming on every
        // realtime or local update must not keep pushing the tick away.
        append(max(
            presentationDate.addingTimeInterval(Self.presentationUpdateIntervalSeconds),
            now.addingTimeInterval(0.01)
        ))
        if let lastSuccess = refreshState.lastSuccessAt {
            append(lastSuccess.addingTimeInterval(refreshDeadline + 0.01))
        }
        if let lastProjectionSuccess = projectionState.lastSuccessAt {
            append(lastProjectionSuccess.addingTimeInterval(projectionDeadline + 0.01))
        }
        if let snapshot {
            if let collectedAt = snapshot.collectedAtDate {
                append(collectedAt.addingTimeInterval(60.01))
            }
            for session in snapshot.managedSessions ?? [] {
                guard let signal = session.presentation?.signal,
                      signal.state == "working" || signal.state == "attention" else { continue }
                appendThreshold(signal.validUntil, seconds: 0)
            }
            if let hostLink = snapshot.hostLink,
               hostLink.isUpdateInProgress,
               let claim = hostLink.claim,
               claim.state == "updating" {
                appendThreshold(claim.deadline, seconds: 0)
                appendThreshold(claim.cutoff, seconds: 0)
            }

            let payload = latestLocalProjection?.engine ?? snapshot.engineStatus?.payload
            appendThreshold(payload?.localProjection?.enginePulseAt ?? payload?.lastUpdated, seconds: 31)
            appendThreshold(payload?.localProjection?.enginePulseAt ?? payload?.lastUpdated, seconds: 121)
            appendThreshold(payload?.localProjection?.generatedAt, seconds: 31)
            appendThreshold(payload?.localProjection?.generatedAt, seconds: 61)
            appendThreshold(payload?.localProjection?.generatedAt, seconds: 121)
            appendThreshold(latestLocalProjection?.evidenceObservedAt, seconds: 61)
        }
        return deadlines.min()
    }

    private func refreshLocalPresentation(relativeTo date: Date) {
        // Only a real file observation re-derives local engine evidence:
        // synthesizing one from the snapshot would mark a missing or
        // unreadable status file as present and clear its error.
        guard let snapshot, let projection = latestLocalProjection else { return }
        let updated = snapshot.applyingLocalProjection(projection, relativeTo: date)
        guard !Self.hasSameLocalStatusPresentation(snapshot, updated) else { return }
        self.snapshot = updated
        persistCachedSnapshot(updated)
    }

    private static func hasSameLocalStatusPresentation(
        _ lhs: HealthSnapshot,
        _ rhs: HealthSnapshot
    ) -> Bool {
        lhs.healthState == rhs.healthState
            && lhs.severity == rhs.severity
            && lhs.headline == rhs.headline
            && lhs.reasons == rhs.reasons
            && lhs.suggestedActions == rhs.suggestedActions
            && lhs.suggestedActionIds == rhs.suggestedActionIds
            && lhs.attention == rhs.attention
            && lhs.transport == rhs.transport
            && lhs.engineStatus?.path == rhs.engineStatus?.path
            && lhs.engineStatus?.exists == rhs.engineStatus?.exists
            && lhs.engineStatus?.fresh == rhs.engineStatus?.fresh
            && lhs.engineStatus?.error == rhs.engineStatus?.error
            && LocalStatusMonitor.PresentationKey(
                sessionIds: [],
                engine: lhs.engineStatus?.payload
            ) == LocalStatusMonitor.PresentationKey(
                sessionIds: [],
                engine: rhs.engineStatus?.payload
            )
    }

    public func setFeedback(_ feedback: HealthActionFeedback?) {
        self.feedback = feedback
    }

    public func clearFeedback() {
        feedback = nil
    }

    private func startRefresh(reason: SnapshotRefreshReason) {
        activeRefreshReason = reason
        if snapshot == nil {
            isInitialLoading = true
        }
        if reason == .manual {
            isManualRefreshActive = true
        }

        let source = self.source
        refreshTask = Task { [weak self] in
            let result = await Self.loadSnapshot(from: source)
            guard !Task.isCancelled, let self else {
                return
            }

            switch result {
            case let .success(loadedSnapshot):
                self.transientRetryTask?.cancel()
                self.transientRetryTask = nil
                self.isRecovering = false
                var snapshot = loadedSnapshot.preservingRealtimeProjection(from: self.snapshot)
                // Only `.lastKnown` means the stream held authority and lost it.
                // `.neverLoaded` is the ordinary case for a machine that never
                // opens a stream at all, and must not strip anything.
                if case .lastKnown = self.projectionTrust() {
                    snapshot = snapshot.markingRuntimeHostProjectionUnavailable()
                }
                // A producer result that found the status file missing or
                // unreadable supersedes the last file observation; the next
                // readable write re-establishes it.
                if loadedSnapshot.engineStatus?.exists == false
                    || loadedSnapshot.engineStatus?.error != nil {
                    self.latestLocalProjection = nil
                }
                if let latestLocalProjection = self.latestLocalProjection {
                    snapshot = snapshot.applyingLocalProjection(latestLocalProjection)
                }
                if self.snapshot?.isSetupRequired == true, !snapshot.isSetupRequired {
                    // "Sign-in opened in Terminal ... then click Refresh" is
                    // done; leaving it up told a connected user to act.
                    self.feedback = nil
                }
                self.snapshot = snapshot
                self.connectRealtimeIfNeeded(
                    snapshot.realtime,
                    sessionIds: (snapshot.managedSessions ?? []).compactMap(\.sessionId)
                )
                self.monitorLocalStatusIfNeeded(snapshot.engineStatus?.path)
                self.appendHistorySample(for: snapshot)
                self.persistCachedSnapshot(snapshot)
                self.loadError = nil
                self.refreshState = self.refreshState.recordingSuccess(at: Date())
                self.exitBootingIfReady(for: snapshot)
                self.scheduleSetupFollowUpIfNeeded(for: snapshot)
            case let .failure(message, transient):
                self.isRecovering = transient
                self.loadError = message
                self.refreshState = self.refreshState.recordingFailure(
                    ProducerRefreshFailure(
                        message: message,
                        command: source.describedCommand,
                        observedAt: Date()
                    )
                )
                if transient {
                    self.scheduleTransientRetry()
                }
            }

            self.completeRefresh(reason: reason)
        }
    }

    /// Event-driven refresh needs a live watch on the engine's status file.
    /// Until there is one (not signed in yet, or the engine has not created its
    /// status directory), poll instead; otherwise the window kept "Finish
    /// setup" and then "Local status is stale" after sign-in had succeeded.
    private func scheduleSetupFollowUpIfNeeded(for snapshot: HealthSnapshot) {
        setupFollowUpTask?.cancel()
        setupFollowUpTask = nil
        // Fixture sources never watch anything, so they must not poll either.
        guard source.supportsLiveMonitoring,
              snapshot.isSetupRequired || !isWatchingLocalStatus else { return }
        setupFollowUpTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(self.setupRefreshInterval))
            guard !Task.isCancelled else { return }
            self.setupFollowUpTask = nil
            self.refresh(reason: .background)
        }
    }

    private func scheduleTransientRetry() {
        guard transientRetryTask == nil else { return }
        transientRetryTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(self.transientRetryDelay))
            guard !Task.isCancelled else { return }
            self.transientRetryTask = nil
            self.refresh(reason: .background)
        }
    }

    private func monitorLocalStatusIfNeeded(_ path: String?) {
        guard source.supportsLiveMonitoring else {
            localStatusMonitor?.stop()
            localStatusMonitor = nil
            localStatusPath = nil
            latestLocalProjection = nil
            armPresentationDeadline()
            return
        }
        // A watch that could not open the status directory (the engine had not
        // created it yet) or whose directory was deleted never fires, so re-arm.
        guard path != localStatusPath || !isWatchingLocalStatus else { return }
        localStatusMonitor?.stop()
        localStatusMonitor = nil
        latestLocalProjection = nil
        localStatusPath = path
        guard let path, !path.isEmpty else {
            armPresentationDeadline()
            return
        }
        let monitor = LocalStatusMonitor(
            statusPath: path,
            onWatchLost: { [weak self] in
                // The next refresh re-arms the watch, or polls until it can.
                Task { @MainActor [weak self] in self?.refresh(reason: .background) }
            },
            onUnreadable: { [weak self] in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.latestLocalProjection = nil
                    self.refresh(reason: .background)
                }
            }
        ) { [weak self] projection in
            Task { @MainActor [weak self] in
                self?.ingestLocalStatus(projection)
            }
        }
        localStatusMonitor = monitor
        monitor.start()
    }

    func ingestLocalStatus(_ projection: LocalStatusMonitor.Projection) {
        latestLocalProjection = projection
        if let snapshot {
            let currentIds = Set((snapshot.managedSessions ?? []).compactMap(\.sessionId))
            let nextIds = Set(projection.sessions.map(\.sessionId))
            let classificationChanged = Self.classificationChanged(
                from: snapshot.engineStatus?.payload,
                to: projection.engine
            )
            let updated = snapshot.applyingLocalProjection(projection)
            if !Self.hasSameLocalStatusPresentation(snapshot, updated) {
                self.snapshot = updated
                persistCachedSnapshot(updated)
            }
            if currentIds != nextIds || classificationChanged {
                lastLocalStatusRefreshAtUptime = ProcessInfo.processInfo.systemUptime
                refresh(reason: .background)
            } else if projection.requiresSnapshotRefresh {
                refreshForLocalStatusChurn()
            }
        }
        armPresentationDeadline()
    }

    private func refreshForLocalStatusChurn() {
        let now = ProcessInfo.processInfo.systemUptime
        let wait = lastLocalStatusRefreshAtUptime.map {
            Self.localStatusRefreshCoalescingSeconds - (now - $0)
        } ?? 0
        guard activeRefreshReason != nil || wait > 0 else {
            lastLocalStatusRefreshAtUptime = now
            refresh(reason: .background)
            return
        }
        // Inside the window or mid-refresh: catch up once at the window's end.
        guard trailingChurnRefreshTask == nil else { return }
        let delay = max(wait, 0.5)
        trailingChurnRefreshTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self else { return }
            self.trailingChurnRefreshTask = nil
            self.refreshForLocalStatusChurn()
        }
    }

    var isWatchingLocalStatus: Bool {
        localStatusMonitor?.isWatching == true
    }

    static func classificationChanged(
        from current: EngineStatusPayload?,
        to next: EngineStatusPayload?
    ) -> Bool {
        guard let next else { return false }
        let currentArchive = current?.archiveBacklog
        let nextArchive = next.archiveBacklog
        let currentProjection = current?.localProjection
        let nextProjection = next.localProjection
        let currentReconciliation = currentProjection?.reconciliation
        let nextReconciliation = nextProjection?.reconciliation
        return current?.isOffline != next.isOffline
            || (current?.spoolPendingCount ?? 0 > 0) != (next.spoolPendingCount ?? 0 > 0)
            || (current?.spoolDeadCount ?? 0 > 0) != (next.spoolDeadCount ?? 0 > 0)
            || currentArchive?.state != nextArchive?.state
            || currentArchive?.mode != nextArchive?.mode
            || (currentArchive?.pendingRanges ?? 0 > 0) != (nextArchive?.pendingRanges ?? 0 > 0)
            || (currentArchive?.deadRanges ?? 0 > 0) != (nextArchive?.deadRanges ?? 0 > 0)
            || current?.shippingProgress?.pendingWork != next.shippingProgress?.pendingWork
            || current?.shippingProgress?.stalled != next.shippingProgress?.stalled
            || currentReconciliation?.state != nextReconciliation?.state
            || currentReconciliation?.failureReason != nextReconciliation?.failureReason
        // `lastReconciledAt` is a clock: the local projection shows it
        // without a producer refresh, and the state/failure transitions
        // above carry every classification change a reconciliation makes.
    }

    private func connectRealtimeIfNeeded(
        _ connection: RealtimeConnectionSnapshot?,
        sessionIds: [String]
    ) {
        guard source.supportsLiveMonitoring else {
            realtimeTask?.cancel()
            realtimeTask = nil
            clearQueuedRealtimeEvents()
            realtimeConnection = nil
            realtimeSessionIds = []
            return
        }
        let normalizedSessionIds = Array(Set(sessionIds)).sorted()
        guard connection != realtimeConnection || normalizedSessionIds != realtimeSessionIds else { return }
        realtimeTask?.cancel()
        realtimeTask = nil
        clearQueuedRealtimeEvents()
        realtimeConnection = connection
        realtimeSessionIds = normalizedSessionIds
        guard let connection,
              connection.runtimeUrl != nil,
              connection.tokenPath != nil,
              !normalizedSessionIds.isEmpty
        else { return }

        realtimeTask = Task { [weak self] in
            for await event in SessionProjectionStream.projections(
                connection: connection,
                sessionIds: normalizedSessionIds
            ) {
                guard !Task.isCancelled, let self else { return }
                switch event {
                case .connected, .alive:
                    self.projectionState = self.projectionState.recordingSuccess(at: Date())
                    self.armPresentationDeadline()
                case let .failed(message):
                    self.flushQueuedRealtimeEvents()
                    self.projectionState = self.projectionState.recordingFailure(
                        ProducerRefreshFailure(
                            message: message,
                            command: "Longhouse server session view",
                            observedAt: Date()
                        )
                    )
                    self.armPresentationDeadline()
                case let .delta(projection):
                    self.queueRealtimeProjection(projection)
                case let .remove(sessionId):
                    self.queueRealtimeRemoval(sessionId)
                }
            }
        }
    }

    /// Coalesce projection traffic before invalidating the full SwiftUI tree and
    /// atomically rewriting the cached snapshot. The stream's initial replay can
    /// contain 100 rows, and active agents can emit several deltas per second;
    /// neither should turn protocol framing into a render or disk-write loop.
    private func queueRealtimeProjection(_ projection: SessionProjection) {
        pendingRealtimeRemovals.remove(projection.sessionId)
        pendingRealtimeProjections[projection.sessionId] = projection
        scheduleRealtimeFlush()
    }

    private func queueRealtimeRemoval(_ sessionId: String) {
        pendingRealtimeProjections.removeValue(forKey: sessionId)
        pendingRealtimeRemovals.insert(sessionId)
        scheduleRealtimeFlush()
    }

    private func scheduleRealtimeFlush() {
        guard realtimeFlushTask == nil else { return }
        realtimeFlushTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled, let self else { return }
            self.realtimeFlushTask = nil
            self.flushQueuedRealtimeEvents()
        }
    }

    private func flushQueuedRealtimeEvents() {
        realtimeFlushTask?.cancel()
        realtimeFlushTask = nil
        guard var updated = snapshot else {
            pendingRealtimeProjections.removeAll(keepingCapacity: true)
            pendingRealtimeRemovals.removeAll(keepingCapacity: true)
            return
        }
        guard !pendingRealtimeProjections.isEmpty || !pendingRealtimeRemovals.isEmpty else {
            return
        }

        let projections = Array(pendingRealtimeProjections.values)
        let removals = Array(pendingRealtimeRemovals)
        pendingRealtimeProjections.removeAll(keepingCapacity: true)
        pendingRealtimeRemovals.removeAll(keepingCapacity: true)
        for projection in projections {
            updated = updated.applying(projection)
        }
        for sessionId in removals {
            updated = updated.removingSession(sessionId)
        }
        snapshot = updated
        armPresentationDeadline()
        persistCachedSnapshot(updated)
    }

    private func clearQueuedRealtimeEvents() {
        realtimeFlushTask?.cancel()
        realtimeFlushTask = nil
        pendingRealtimeProjections.removeAll(keepingCapacity: true)
        pendingRealtimeRemovals.removeAll(keepingCapacity: true)
    }

    private func completeRefresh(reason: SnapshotRefreshReason) {
        activeRefreshReason = nil
        isInitialLoading = false

        if queuedManualRefresh {
            queuedManualRefresh = false
            startRefresh(reason: .manual)
            return
        }

        if reason == .manual {
            isManualRefreshActive = false
        }
        armPresentationDeadline()
        scheduleTrustBackstop()
    }

    private func scheduleTrustBackstop() {
        trustBackstopTask?.cancel()
        trustBackstopTask = nil
        // Fixture sources never refresh on their own.
        guard source.supportsLiveMonitoring else { return }
        let delay = refreshDeadline / 2
        trustBackstopTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self else { return }
            self.trustBackstopTask = nil
            self.refresh(reason: .background)
        }
    }

    private static func loadSnapshot(from source: any HealthSnapshotSource) async -> SnapshotLoadResult {
        await Task.detached(priority: .userInitiated) {
            do {
                return .success(try source.load())
            } catch {
                let transient = (error as? SnapshotSourceError)?.isTransient ?? false
                return .failure(error.localizedDescription, transient: transient)
            }
        }.value
    }

    private func appendHistorySample(for snapshot: HealthSnapshot) {
        let capturedAt = snapshot.collectedAtDate ?? Date()
        let sample = SnapshotHistorySample(
            capturedAt: capturedAt,
            sessionsRecent: snapshot.activitySummary?.sessionsRecent ?? 0,
            spoolPendingCount: snapshot.engineStatus?.payload?.spoolPendingCount ?? 0,
            outboxCount: snapshot.outboxCount,
            severity: snapshot.parsedSeverity
        )

        if let last = history.last,
           abs(last.capturedAt.timeIntervalSince(sample.capturedAt)) < 0.5,
           last.sessionsRecent == sample.sessionsRecent,
           last.spoolPendingCount == sample.spoolPendingCount,
           last.outboxCount == sample.outboxCount,
           last.severity == sample.severity {
            return
        }

        history.append(sample)
        let cutoff = capturedAt.addingTimeInterval(-Self.historyRetentionSeconds)
        history.removeAll { $0.capturedAt < cutoff }
        if history.count > Self.maxHistorySamples {
            history.removeFirst(history.count - Self.maxHistorySamples)
        }
    }

    private static func defaultCacheURL(for source: any HealthSnapshotSource) -> URL? {
        guard source is CLIHealthSnapshotSource else {
            return nil
        }
        guard let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        return appSupport
            .appendingPathComponent("Longhouse", isDirectory: true)
            .appendingPathComponent("MenuBar", isDirectory: true)
            .appendingPathComponent("last-good-snapshot.json")
    }

    private static func loadCachedSnapshot(from cacheURL: URL?) -> (snapshot: HealthSnapshot, data: Data)? {
        guard let cacheURL,
              let data = try? Data(contentsOf: cacheURL),
              let snapshot = try? HealthSnapshotDecoder.decode(data: data) else {
            return nil
        }
        return (snapshot, data)
    }

    static func loadNativeBootstrapSnapshot(
        from statusURL: URL? = nil,
        referenceDate: Date = Date()
    ) -> HealthSnapshot? {
        let resolvedURL: URL
        if let statusURL {
            resolvedURL = statusURL
        } else {
            let home = ProcessInfo.processInfo.environment["LONGHOUSE_HOME"]
                .map { URL(fileURLWithPath: $0, isDirectory: true) }
                ?? FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".longhouse", isDirectory: true)
            resolvedURL = home
                .appendingPathComponent("agent", isDirectory: true)
                .appendingPathComponent("engine-status.json")
        }
        guard let data = try? Data(contentsOf: resolvedURL) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let payload = try? decoder.decode(EngineStatusPayload.self, from: data) else {
            return nil
        }
        let pulse = payload.localProjection?.enginePulseAt ?? payload.lastUpdated
        let pulseDate = pulse.flatMap(parseISO8601)
        let age = pulseDate.map { max(0, referenceDate.timeIntervalSince($0)) }
        let fresh = age.map { $0 <= 30 } ?? false
        let reconciliation = payload.localProjection?.reconciliation?.state
        let headline = reconciliation == "reconciling"
            ? "Refreshing local status"
            : "Loading local status"
        return HealthSnapshot(
            schemaVersion: 1,
            collectedAt: pulse,
            healthState: "loading",
            severity: "gray",
            headline: headline,
            reasons: ["native_status_bootstrap"],
            suggestedActions: [],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: resolvedURL.path,
                exists: true,
                fresh: fresh,
                ageSeconds: age.map { Int($0) },
                payload: payload,
                error: nil
            ),
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
    }

    private static func parseISO8601(_ value: String) -> Date? {
        ISO8601DateFormatters.parse(value)
    }

    /// A cold launch reads the cache once and a live refresh replaces it
    /// within ~30 ms, so a cache up to a minute old costs nothing. Every
    /// refresh restamps `collected_at` and session ages, so without this the
    /// 16 KB file was atomically replaced on every 5 s refresh.
    static let cachedSnapshotMinimumRewriteSeconds: TimeInterval = 60

    private func persistCachedSnapshot(_ snapshot: HealthSnapshot) {
        guard let cacheURL else {
            return
        }
        let headline = [snapshot.healthState, snapshot.severity, snapshot.headline]
        let now = ProcessInfo.processInfo.systemUptime
        if headline == lastCachedSnapshotHeadline,
           let lastWrite = lastCachedSnapshotWriteUptime,
           now - lastWrite < Self.cachedSnapshotMinimumRewriteSeconds {
            return
        }
        do {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(snapshot)
            // Identical bytes carry no new information and need no atomic
            // disk replacement.
            guard data != lastCachedSnapshotData else { return }
            try FileManager.default.createDirectory(
                at: cacheURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try data.write(to: cacheURL, options: [.atomic])
            lastCachedSnapshotData = data
            lastCachedSnapshotHeadline = headline
            lastCachedSnapshotWriteUptime = now
        } catch {
            // Cache failures should never make the menu bar look unhealthy.
        }
    }
}

private enum SnapshotLoadResult: Sendable {
    case success(HealthSnapshot)
    case failure(String, transient: Bool)
}

public struct SnapshotHistorySample: Equatable, Sendable {
    public let capturedAt: Date
    public let sessionsRecent: Int
    public let spoolPendingCount: Int
    public let outboxCount: Int
    public let severity: HarnessSeverity
}
