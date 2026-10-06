import Foundation

enum SnapshotReachability: Equatable {
    case unknown
    case reachable
    case degraded
    case offline
    case authRequired
}

enum TimelineFreshness: Equatable {
    case unknown
    case fresh
    case aging
    case stale
}

enum TimelineConnectivityBanner: Equatable, Hashable {
    case none
    case degraded
    case offline
    case authRequired
    case updating
    case slowUpdate(elapsed: String)
}

enum StreamDisconnectReason: Equatable {
    case clientStop
    case watchdogStop
    case serverEOF
    case networkError
    case cancelled
    case authFailure
    case waitingForConnectivity
    case unknown
}

enum HostUpdatePresentation: Equatable, Hashable {
    case updating
    case slowUpdate(elapsed: String)
}

struct HostUpdateState: Equatable, Hashable, Sendable {
    static let announceAfterSeconds: TimeInterval = 2

    private(set) var claim: HostLifecycle?
    private(set) var claimStartedAt: Date?
    private(set) var waitingForAction = false
    init() {}

    var hasClaim: Bool { claim != nil }

    func isActive(at now: Date) -> Bool {
        guard claimStartedAt != nil else { return false }
        guard let expiresAt = expiryDate, expiresAt <= now else { return true }
        return false
    }

    func presentation(at now: Date) -> HostUpdatePresentation? {
        guard isActive(at: now), let claimStartedAt else { return nil }
        if let expectedBackBy = claim?.expectedBackBy.flatMap(LonghouseDateParser.parse),
           expectedBackBy <= now {
            return .slowUpdate(elapsed: RuntimeElapsed.label(from: claimStartedAt, to: now, precise: true))
        }
        guard waitingForAction || now.timeIntervalSince(claimStartedAt) >= Self.announceAfterSeconds else {
            return nil
        }
        return .updating
    }

    mutating func apply(_ lifecycle: HostLifecycle, now: Date) {
        guard lifecycle.state == .updating else {
            observeServingEvidence()
            return
        }
        if !isActive(at: now) {
            observeServingEvidence()
        }
        let sameAttempt = claim.map { current in
            guard let currentId = current.attemptId, let nextId = lifecycle.attemptId else {
                // Epoch changes identify a new process, not a reopened host.
                return true
            }
            return currentId == nextId
        } ?? (waitingForAction && claimStartedAt != nil)
        if !sameAttempt {
            claimStartedAt = now
        } else if claimStartedAt == nil {
            claimStartedAt = now
        }
        claim = lifecycle
    }

    mutating func observeRuntimeRestarting(claim lifecycle: HostLifecycle?, now: Date) {
        if !isActive(at: now) {
            observeServingEvidence()
        }
        if let lifecycle, lifecycle.state == .updating {
            apply(lifecycle, now: now)
        } else if claimStartedAt == nil {
            claimStartedAt = now
        }
        waitingForAction = true
    }

    mutating func observeServingEvidence() {
        claim = nil
        claimStartedAt = nil
        waitingForAction = false
    }

    func nextClockInterval(at now: Date, default interval: TimeInterval) -> TimeInterval {
        guard isActive(at: now), let claimStartedAt else { return interval }
        var transitions = [Date]()
        if !waitingForAction {
            transitions.append(claimStartedAt.addingTimeInterval(Self.announceAfterSeconds))
        }
        if let expectedBackBy = claim?.expectedBackBy.flatMap(LonghouseDateParser.parse) {
            transitions.append(expectedBackBy)
            if expectedBackBy <= now {
                transitions.append(now.addingTimeInterval(1))
            }
        }
        if let expiryDate { transitions.append(expiryDate) }
        guard let next = transitions.filter({ $0 > now }).min() else { return interval }
        return min(interval, max(0.05, next.timeIntervalSince(now)))
    }

    private var expiryDate: Date? {
        [claim?.deadline, claim?.cutoff]
            .compactMap { $0.flatMap(LonghouseDateParser.parse) }
            .min()
    }
}

enum HostUpdateServingEvidence: Equatable, Sendable {
    case admission(HostAdmission?, runtimeEpoch: String?)
    case writeAccepted
}


enum TimelineStreamSignal: Equatable {
    case firstConnected
    case reconnected
    case heartbeat
    case upsert
    case remove
}

enum TimelineNetworkPathStatus: Equatable {
    case unknown
    case satisfied
    case unsatisfied
}

enum TimelineConnectivityEvent: Equatable {
    case cacheLoaded(hasLoadedData: Bool, savedAt: Date)
    case snapshotSucceeded(hasLoadedData: Bool)
    case snapshotFailed
    case authFailed
    case streamSignal(TimelineStreamSignal)
    case streamDisconnected(StreamDisconnectReason)
    case lifecycleStopped
    case networkPathChanged(TimelineNetworkPathStatus)
    case hostLifecycle(HostLifecycle)
    case runtimeRestarting(HostLifecycle?)
    case servingEvidence(HostUpdateServingEvidence)
}

struct TimelineConnectivityState: Equatable {
    static let freshAfterSeconds: TimeInterval = 90
    static let staleAfterSeconds: TimeInterval = 180
    static let offlineAfterSnapshotFailures = 2

    var reachability: SnapshotReachability = .unknown
    var consecutiveSnapshotFailures = 0
    var lastUpdatedAt: Date?
    var hasLoadedData = false
    var hasFreshnessEvidence = false
    var networkPathStatus: TimelineNetworkPathStatus = .unknown
    var hostUpdate = HostUpdateState()

    init(
        reachability: SnapshotReachability = .unknown,
        consecutiveSnapshotFailures: Int = 0,
        lastUpdatedAt: Date? = nil,
        hasLoadedData: Bool = false,
        hasFreshnessEvidence: Bool? = nil,
        networkPathStatus: TimelineNetworkPathStatus = .unknown,
        hostUpdate: HostUpdateState = HostUpdateState()
    ) {
        self.reachability = reachability
        self.consecutiveSnapshotFailures = consecutiveSnapshotFailures
        self.lastUpdatedAt = lastUpdatedAt
        self.hasLoadedData = hasLoadedData
        self.hasFreshnessEvidence = hasFreshnessEvidence ?? hasLoadedData
        self.networkPathStatus = networkPathStatus
        self.hostUpdate = hostUpdate
    }
    func freshness(at now: Date) -> TimelineFreshness {
        guard hasFreshnessEvidence, let lastUpdatedAt else { return .unknown }
        let age = max(0, now.timeIntervalSince(lastUpdatedAt))
        if age <= Self.freshAfterSeconds { return .fresh }
        if age <= Self.staleAfterSeconds { return .aging }
        return .stale
    }

    /// Claims are separate from transport failures: a planned restart is
    /// announced calmly, while a plain stream loss keeps the existing path.
    func banner(at now: Date) -> TimelineConnectivityBanner {
        if reachability != .authRequired,
           let update = hostUpdate.presentation(at: now) {
            switch update {
            case .updating:
                return .updating
            case .slowUpdate(let elapsed):
                return .slowUpdate(elapsed: elapsed)
            }
        }
        switch reachability {
        case .authRequired:
            return .authRequired
        case .unknown, .reachable:
            return .none
        case .degraded:
            // A single failed snapshot is a retry, not a fault. Stay silent
            // until the data is genuinely stale and failures have repeated.
            guard freshness(at: now) == .stale,
                  consecutiveSnapshotFailures >= Self.offlineAfterSnapshotFailures
            else { return .none }
            return .degraded
        case .offline:
            // Offline is hard evidence (OS path or sustained failure). Once
            // the data stops being fresh, explain the frozen timeline.
            return freshness(at: now) == .fresh ? .none : .offline
        }
    }
    mutating func apply(_ event: TimelineConnectivityEvent, now: Date) {
        switch event {
        case .cacheLoaded(let hasLoadedData, let savedAt):
            self.hasLoadedData = hasLoadedData
            if hasLoadedData {
                hasFreshnessEvidence = true
                lastUpdatedAt = savedAt
            }
        case .snapshotSucceeded(let hasLoadedData):
            reachability = .reachable
            consecutiveSnapshotFailures = 0
            self.hasLoadedData = hasLoadedData
            hasFreshnessEvidence = true
            lastUpdatedAt = now
        case .snapshotFailed:
            consecutiveSnapshotFailures += 1
            if hasLoadedData {
                reachability = .degraded
            } else if hasFreshnessEvidence && consecutiveSnapshotFailures < Self.offlineAfterSnapshotFailures {
                reachability = .degraded
            } else if consecutiveSnapshotFailures >= Self.offlineAfterSnapshotFailures {
                reachability = .offline
            } else {
                reachability = .degraded
            }
        case .authFailed:
            reachability = .authRequired
        case .streamSignal(let signal):
            applyStreamSignal(signal, now: now)
        case .streamDisconnected(let reason):
            // Stream disconnects are diagnostics only. Auth is the terminal
            // exception; transport churn is not a product fault.
            if reason == .authFailure {
                reachability = .authRequired
            }
        case .lifecycleStopped:
            break
        case .networkPathChanged(let status):
            networkPathStatus = status
            applyNetworkPathStatus(status, now: now)
        case .hostLifecycle(let lifecycle):
            hostUpdate.apply(lifecycle, now: now)
        case .runtimeRestarting(let claim):
            hostUpdate.observeRuntimeRestarting(claim: claim, now: now)
        case .servingEvidence(let evidence):
            switch evidence {
            case .admission(let admission, _):
                if admission == .open { hostUpdate.observeServingEvidence() }
            case .writeAccepted:
                hostUpdate.observeServingEvidence()
            }
        }
    }

    mutating func apply(
        _ event: TimelineConnectivityEvent,
        now: Date,
        eventGeneration: UInt64,
        currentGeneration: UInt64
    ) {
        guard eventGeneration == currentGeneration else { return }
        apply(event, now: now)
    }

    private mutating func applyStreamSignal(_ signal: TimelineStreamSignal, now: Date) {
        switch signal {
        // Transport-only signals do not prove data freshness or recovery.
        case .firstConnected, .reconnected, .heartbeat:
            break
        case .upsert, .remove:
            hasLoadedData = true
            hasFreshnessEvidence = true
            lastUpdatedAt = now
        }
    }

    private mutating func applyNetworkPathStatus(_ status: TimelineNetworkPathStatus, now: Date) {
        switch status {
        case .unsatisfied:
            guard reachability != .authRequired else { return }
            if freshness(at: now) != .fresh {
                reachability = .offline
            }
        case .satisfied:
            if reachability == .offline {
                reachability = hasLoadedData ? .degraded : .unknown
            }
        case .unknown:
            break
        }
    }
}
