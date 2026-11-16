import Foundation

/// Full tool bodies the session has loaded for rows a lite page sent as
/// previews, plus which ones are in flight or no longer exist. Bodies are
/// immutable for a cursor, so they stay for the life of the session screen.
struct LiteBodyState: Sendable, Equatable {
    var bodies: [String: SessionEventBody] = [:]
    var loading: Set<String> = []
    /// Cursors the server reported missing: the session was re-rendered since
    /// the page loaded. Those rows keep their preview and say so.
    var unavailable: Set<String> = []
    /// Cursors whose last read failed, and when. The row says so; opening it
    /// again after `retryCooldown` retries (a re-render reopening the row
    /// fires the same toggle, so an immediate retry would loop).
    var failed: Set<String> = []
    var failedAt: [String: Date] = [:]
    static let retryCooldown: TimeInterval = 5

    func mayRetry(_ cursor: String, now: Date = Date()) -> Bool {
        guard let at = failedAt[cursor] else { return true }
        return now.timeIntervalSince(at) >= Self.retryCooldown
    }
}
