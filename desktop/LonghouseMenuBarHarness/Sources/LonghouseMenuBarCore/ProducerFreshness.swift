import Foundation

/// Why the last producer refresh failed, and what was being run when it did.
public struct ProducerRefreshFailure: Equatable, Sendable {
    public let message: String
    public let command: String?
    public let observedAt: Date

    public init(message: String, command: String?, observedAt: Date) {
        self.message = message
        self.command = command
        self.observedAt = observedAt
    }
}

/// Raw record of how the configured producer command has been behaving.
///
/// This is the only clock the app is allowed to use when deciding whether what
/// it is showing is current. It advances solely on a successful execute-and-decode
/// of the configured health command.
///
/// It is deliberately independent of payload `collected_at`, engine pulses,
/// realtime projection deltas, and cache file mtime. Every one of those can keep
/// advancing while the producer is failing — `applyingLocalProjection` rewrites
/// `collectedAt` from the engine pulse and stamps `fresh: true, ageSeconds: 0`,
/// and the engine writes `engine-status.json` continuously. A freshness check
/// reading any of them would call an unrefreshable snapshot current.
public struct ProducerRefreshState: Equatable, Sendable {
    /// When the producer last executed and decoded successfully.
    public let lastSuccessAt: Date?
    /// The most recent failure, if the last completed attempt failed. Cleared on success.
    public let latestFailure: ProducerRefreshFailure?
    /// Consecutive failed attempts since the last success.
    ///
    /// Exists so a transient failure can be masked briefly without that mask
    /// becoming unbounded. A producer that times out forever reports every
    /// attempt as transient, so "retrying" would otherwise look normal
    /// indefinitely — the same silence the fail-loud work exists to remove.
    public let consecutiveFailures: Int

    public init(
        lastSuccessAt: Date? = nil,
        latestFailure: ProducerRefreshFailure? = nil,
        consecutiveFailures: Int = 0
    ) {
        self.lastSuccessAt = lastSuccessAt
        self.latestFailure = latestFailure
        self.consecutiveFailures = consecutiveFailures
    }

    public static let neverAttempted = ProducerRefreshState()

    /// Whether a transient failure may still be presented as a quiet retry.
    /// True only for the very first failure after a success.
    public var isWithinTransientGrace: Bool {
        consecutiveFailures <= 1
    }

    func recordingSuccess(at date: Date) -> ProducerRefreshState {
        ProducerRefreshState(lastSuccessAt: date, latestFailure: nil, consecutiveFailures: 0)
    }

    func recordingFailure(_ failure: ProducerRefreshFailure) -> ProducerRefreshState {
        ProducerRefreshState(
            lastSuccessAt: lastSuccessAt,
            latestFailure: failure,
            consecutiveFailures: consecutiveFailures + 1
        )
    }
}

/// Context attached to anything the app is showing that it cannot vouch for.
public struct LastKnownContext: Equatable, Sendable {
    public let lastSuccessAt: Date?
    public let failure: ProducerRefreshFailure?

    public init(lastSuccessAt: Date?, failure: ProducerRefreshFailure?) {
        self.lastSuccessAt = lastSuccessAt
        self.failure = failure
    }

    /// How long the displayed data has been unverifiable, or nil if the producer never succeeded.
    public func age(relativeTo date: Date) -> TimeInterval? {
        guard let lastSuccessAt else { return nil }
        return max(0, date.timeIntervalSince(lastSuccessAt))
    }
}

/// Compact age rendering, matching the panel's existing `compactAgeLabel` format.
public enum SnapshotAgeFormatter {
    public static func compact(_ interval: TimeInterval) -> String {
        let seconds = Int(max(0, interval))
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3600)h" }
        return "\(seconds / 86_400)d"
    }
}

/// How much the app trusts what it is currently displaying.
///
/// The invariant: data may be presented as current only when the configured
/// producer command has executed and decoded successfully within the refresh
/// deadline. Everything else is explicitly last-known.
public enum DataTrust: Equatable, Sendable {
    /// The producer succeeded on its most recent completed attempt.
    case current
    /// The producer is failing or has gone quiet. Everything shown is last-known.
    case lastKnown(LastKnownContext)
    /// The producer has never succeeded. There is nothing trustworthy to show.
    case neverLoaded(failure: ProducerRefreshFailure?)

    public var isCurrent: Bool {
        if case .current = self { return true }
        return false
    }

    /// The failure to surface to the user, if any.
    public var failure: ProducerRefreshFailure? {
        switch self {
        case .current: return nil
        case let .lastKnown(context): return context.failure
        case let .neverLoaded(failure): return failure
        }
    }
}

extension ProducerRefreshState {
    /// Resolve trust.
    ///
    /// Primary mechanism: the outcome of the most recent completed attempt. A
    /// failed refresh drops trust immediately rather than after a grace period —
    /// the transient-retry path already covers a single blip via `isRecovering`,
    /// so a grace period here would only delay honesty.
    ///
    /// Backstop: if the producer has simply gone quiet without reporting a
    /// failure (a wedged refresh task, a source that never returns), trust still
    /// decays once `deadline` has passed since the last success.
    public func trust(relativeTo date: Date, deadline: TimeInterval) -> DataTrust {
        guard let lastSuccessAt else {
            return .neverLoaded(failure: latestFailure)
        }
        if let latestFailure {
            return .lastKnown(LastKnownContext(lastSuccessAt: lastSuccessAt, failure: latestFailure))
        }
        if date.timeIntervalSince(lastSuccessAt) > deadline {
            return .lastKnown(LastKnownContext(lastSuccessAt: lastSuccessAt, failure: nil))
        }
        return .current
    }
}
