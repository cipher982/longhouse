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

    init(
        reachability: SnapshotReachability = .unknown,
        consecutiveSnapshotFailures: Int = 0,
        lastUpdatedAt: Date? = nil,
        hasLoadedData: Bool = false,
        hasFreshnessEvidence: Bool? = nil,
        networkPathStatus: TimelineNetworkPathStatus = .unknown
    ) {
        self.reachability = reachability
        self.consecutiveSnapshotFailures = consecutiveSnapshotFailures
        self.lastUpdatedAt = lastUpdatedAt
        self.hasLoadedData = hasLoadedData
        self.hasFreshnessEvidence = hasFreshnessEvidence ?? hasLoadedData
        self.networkPathStatus = networkPathStatus
    }

    func freshness(at now: Date) -> TimelineFreshness {
        guard hasFreshnessEvidence, let lastUpdatedAt else { return .unknown }
        let age = max(0, now.timeIntervalSince(lastUpdatedAt))
        if age <= Self.freshAfterSeconds { return .fresh }
        if age <= Self.staleAfterSeconds { return .aging }
        return .stale
    }

    /// There is no "updating" banner. The timeline is a live stream, so
    /// "we are fetching" is the permanent steady state and saying it is
    /// noise. Every visible banner names a fault the user can act on
    /// (retry, reconnect, sign in); everything else is silence.
    func banner(at now: Date) -> TimelineConnectivityBanner {
        switch reachability {
        case .authRequired:
            return .authRequired
        case .unknown, .reachable:
            return .none
        case .degraded:
            // A single failed snapshot is a retry, not a fault. Stay silent
            // until the data is genuinely stale AND failures have repeated.
            guard freshness(at: now) == .stale,
                  consecutiveSnapshotFailures >= Self.offlineAfterSnapshotFailures
            else { return .none }
            return .degraded
        case .offline:
            // Offline is hard evidence (OS path or sustained failure). Once
            // the data stops being fresh, say so rather than showing a
            // frozen timeline with no explanation.
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
            // Rule 5: stream disconnects are diagnostics only and must not
            // alter snapshot reachability OR the user banner. Auth is the one
            // terminal exception. Transport churn is not evidence of a
            // product fault — snapshot failures are.
            if reason == .authFailure {
                reachability = .authRequired
            }
        case .lifecycleStopped:
            break
        case .networkPathChanged(let status):
            networkPathStatus = status
            applyNetworkPathStatus(status, now: now)
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
        case .firstConnected, .reconnected, .heartbeat:
            // Transport-only signals do not prove data freshness or recovery.
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
