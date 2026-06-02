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
    case updating
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
    var recoveryActive = false
    var networkPathStatus: TimelineNetworkPathStatus = .unknown

    init(
        reachability: SnapshotReachability = .unknown,
        consecutiveSnapshotFailures: Int = 0,
        lastUpdatedAt: Date? = nil,
        hasLoadedData: Bool = false,
        hasFreshnessEvidence: Bool? = nil,
        recoveryActive: Bool = false,
        networkPathStatus: TimelineNetworkPathStatus = .unknown
    ) {
        self.reachability = reachability
        self.consecutiveSnapshotFailures = consecutiveSnapshotFailures
        self.lastUpdatedAt = lastUpdatedAt
        self.hasLoadedData = hasLoadedData
        self.hasFreshnessEvidence = hasFreshnessEvidence ?? hasLoadedData
        self.recoveryActive = recoveryActive
        self.networkPathStatus = networkPathStatus
    }

    func freshness(at now: Date) -> TimelineFreshness {
        guard hasFreshnessEvidence, let lastUpdatedAt else { return .unknown }
        let age = max(0, now.timeIntervalSince(lastUpdatedAt))
        if age <= Self.freshAfterSeconds { return .fresh }
        if age <= Self.staleAfterSeconds { return .aging }
        return .stale
    }

    func banner(at now: Date) -> TimelineConnectivityBanner {
        let freshness = freshness(at: now)
        switch reachability {
        case .authRequired:
            return .authRequired
        case .unknown:
            if freshness == .stale && recoveryActive { return .updating }
            return .none
        case .reachable:
            return .none
        case .degraded:
            switch freshness {
            case .fresh, .unknown:
                return .none
            case .aging:
                return .updating
            case .stale:
                return consecutiveSnapshotFailures >= Self.offlineAfterSnapshotFailures ? .degraded : .updating
            }
        case .offline:
            switch freshness {
            case .fresh:
                return .none
            case .aging:
                return .updating
            case .stale, .unknown:
                return .offline
            }
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
            recoveryActive = false
        case .snapshotFailed:
            consecutiveSnapshotFailures += 1
            recoveryActive = true
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
            recoveryActive = false
        case .streamSignal(let signal):
            applyStreamSignal(signal, now: now)
        case .streamDisconnected(let reason):
            // Rule 5: stream disconnects are diagnostics only and must not
            // alter snapshot reachability OR the user banner. Auth is the one
            // terminal exception. A non-auth disconnect must NOT set
            // `recoveryActive`, because in the `unknown + stale` cell that
            // would surface an `Updating` strip from pure transport churn —
            // exactly the false-health claim this model exists to kill.
            // Recovery is owned by snapshot failures (real product-health
            // evidence) and cleared by snapshot success / data-bearing events.
            if reason == .authFailure {
                reachability = .authRequired
                recoveryActive = false
            }
        case .lifecycleStopped:
            recoveryActive = false
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
            recoveryActive = false
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
