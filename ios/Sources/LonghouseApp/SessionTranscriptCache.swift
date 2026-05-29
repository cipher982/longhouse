import Foundation

@MainActor
final class SessionTranscriptCache {
    struct Snapshot {
        let detail: SessionDetail
        let events: [SessionEvent]
        let loadedProjectionItemCount: Int
        let totalProjectionItemCount: Int
        let tailSnapshotEventId: Int?
        /// Last realtime pubsub seq seen, so a same-process nav-away/back can
        /// seed the SSE reconnect cursor instead of replaying cold.
        let lastPubsubSeq: Int?
        let savedAt: Date
        fileprivate let estimatedBytes: Int
        fileprivate var lastAccessedAt: Date
    }

    static let shared = SessionTranscriptCache()

    private let ttl: TimeInterval
    private let maxBytes: Int
    private let now: () -> Date
    private var entries: [String: Snapshot] = [:]
    private var totalBytes = 0

    init(
        ttl: TimeInterval = 60 * 60,
        maxBytes: Int = 12 * 1024 * 1024,
        now: @escaping () -> Date = Date.init
    ) {
        self.ttl = ttl
        self.maxBytes = maxBytes
        self.now = now
    }

    func snapshot(serverURL: String, sessionId: String) -> Snapshot? {
        let date = now()
        pruneExpired(now: date)
        let key = cacheKey(serverURL: serverURL, sessionId: sessionId)
        guard var entry = entries[key] else { return nil }
        entry.lastAccessedAt = date
        entries[key] = entry
        return entry
    }

    func store(
        serverURL: String,
        sessionId: String,
        detail: SessionDetail,
        events: [SessionEvent],
        loadedProjectionItemCount: Int,
        totalProjectionItemCount: Int,
        tailSnapshotEventId: Int?,
        lastPubsubSeq: Int? = nil
    ) {
        guard maxBytes > 0 else { return }
        let date = now()
        let estimatedBytes = Self.estimateBytes(detail: detail, events: events)
        let key = cacheKey(serverURL: serverURL, sessionId: sessionId)

        remove(key)
        guard estimatedBytes <= maxBytes else { return }

        entries[key] = Snapshot(
            detail: detail,
            events: events,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId,
            lastPubsubSeq: lastPubsubSeq,
            savedAt: date,
            estimatedBytes: estimatedBytes,
            lastAccessedAt: date
        )
        totalBytes += estimatedBytes
        evictIfNeeded()
    }

    func clear() {
        entries.removeAll()
        totalBytes = 0
    }

    private func cacheKey(serverURL: String, sessionId: String) -> String {
        let normalizedServer = serverURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .lowercased()
        return "\(normalizedServer)|\(sessionId)"
    }

    private func pruneExpired(now date: Date) {
        let expiredKeys = entries.compactMap { key, entry in
            date.timeIntervalSince(entry.savedAt) >= ttl ? key : nil
        }
        expiredKeys.forEach(remove)
    }

    private func evictIfNeeded() {
        while totalBytes > maxBytes, let victim = entries.min(by: { left, right in
            left.value.lastAccessedAt < right.value.lastAccessedAt
        })?.key {
            remove(victim)
        }
    }

    private func remove(_ key: String) {
        guard let removed = entries.removeValue(forKey: key) else { return }
        totalBytes = max(0, totalBytes - removed.estimatedBytes)
    }

    private static func estimateBytes(detail: SessionDetail, events: [SessionEvent]) -> Int {
        let encoder = JSONEncoder()
        let detailBytes = (try? encoder.encode(detail).count) ?? 1024
        let encodedEventBytes = try? encoder.encode(events).count
        let fallbackEventBytes = events.reduce(0) { partial, event in
            let contentBytes = event.contentText?.utf8.count ?? 0
            let toolNameBytes = event.toolName?.utf8.count ?? 0
            let toolOutputBytes = event.toolOutputText?.utf8.count ?? 0
            let timestampBytes = event.timestamp.utf8.count
            return partial + 256 + contentBytes + toolNameBytes + toolOutputBytes + timestampBytes
        }
        let eventBytes = encodedEventBytes ?? fallbackEventBytes
        return max(1024, detailBytes + eventBytes)
    }
}
