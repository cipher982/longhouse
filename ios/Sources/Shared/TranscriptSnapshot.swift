import Foundation

/// The transcript state a session screen restores from, independent of where it
/// was kept. `TranscriptSnapshotStore` holds this in memory for a reopen inside
/// the same launch and on disk for a relaunch, and the session view model has
/// exactly one path for applying it.
struct TranscriptSnapshot: Codable, Sendable {
    var detail: SessionDetail
    var events: [SessionEvent]
    var projectionItems: [SessionProjectionItem]?
    var loadedProjectionItemCount: Int
    var totalProjectionItemCount: Int
    var tailSnapshotEventId: String?
    /// Storage-v2 cursor for the page older than this tail. Without it a
    /// restored session's first older-page fetch goes out cursor-less and
    /// gets the latest window again. Optional so older caches still decode.
    var tailNextCursor: String?
    /// Last realtime pubsub seq rendered, so a reopen can seed the SSE
    /// reconnect cursor instead of replaying cold.
    var lastPubsubSeq: Int?
    /// Durable viewport revision this snapshot rendered. Optional so snapshots
    /// written before the handshake rollout still decode.
    var workspaceRevisionFingerprint: String?
    var savedAt: Date

    init(
        detail: SessionDetail,
        events: [SessionEvent],
        projectionItems: [SessionProjectionItem]? = nil,
        loadedProjectionItemCount: Int,
        totalProjectionItemCount: Int,
        tailSnapshotEventId: String?,
        tailNextCursor: String? = nil,
        lastPubsubSeq: Int? = nil,
        workspaceRevisionFingerprint: String? = nil,
        savedAt: Date = Date()
    ) {
        self.detail = detail
        self.events = events
        self.projectionItems = projectionItems
        self.loadedProjectionItemCount = loadedProjectionItemCount
        self.totalProjectionItemCount = totalProjectionItemCount
        self.tailSnapshotEventId = tailSnapshotEventId
        self.tailNextCursor = tailNextCursor
        self.lastPubsubSeq = lastPubsubSeq
        self.workspaceRevisionFingerprint = workspaceRevisionFingerprint
        self.savedAt = savedAt
    }
}

extension TranscriptSnapshot {
    /// Both tiers key on the same `(server, session)` pair. They used to
    /// normalize it separately and disagreed about leading slashes.
    static func cacheKey(serverURL: String, sessionId: String) -> String {
        "\(normalizedServerURL(serverURL))|\(sessionId)"
    }

    static func normalizedServerURL(_ serverURL: String) -> String {
        var value = serverURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }

    /// Rough retained size, used only to bound the in-memory tier. Cheap
    /// because it never encodes: Swift's native strings are UTF-8 backed, so
    /// `utf8.count` reads a stored length rather than walking the text.
    var estimatedBytes: Int {
        let eventBytes = events.reduce(0) { $0 + Self.estimatedBytes(of: $1) }
        let projectionBytes = (projectionItems ?? []).reduce(0) { partial, item in
            partial + 128 + (item.event.map(Self.estimatedBytes(of:)) ?? 0)
        }
        return max(1024, 1024 + eventBytes + projectionBytes)
    }

    private static func estimatedBytes(of event: SessionEvent) -> Int {
        256
            + (event.contentText?.utf8.count ?? 0)
            + (event.toolName?.utf8.count ?? 0)
            + (event.toolOutputText?.utf8.count ?? 0)
            + event.timestamp.utf8.count
    }
}
