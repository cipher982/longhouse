import CryptoKit
import Foundation

/// Durable, on-disk mirror of the most-recent transcript tail per session.
///
/// One JSON file per `(serverURL|sessionId)` under Application Support, written
/// atomically, excluded from iCloud/iTunes backup, and protected until first
/// unlock so a background relaunch can still read it.
///
/// This is a *derived* cache. The Runtime Host is authoritative; any snapshot
/// can be discarded and rebuilt from `/api/.../mobile-tail`. It exists for one
/// reason: when iOS evicts a backgrounded app (e.g. the phone sat locked), a
/// cold relaunch into a session should render the last-seen transcript
/// instantly instead of a blank screen with a lone warning triangle.
///
/// Reads are synchronous (a single small file on session open). Writes are
/// dispatched to a private serial queue so persistence never blocks the UI.
struct TranscriptSnapshotStore: Sendable {
    /// Bump when the on-disk shape changes; mismatched files are ignored and
    /// pruned so a stale schema can never crash decode or render.
    static let schemaVersion = 2

    struct Snapshot: Codable, Sendable {
        var schemaVersion: Int
        var serverURL: String
        var sessionId: String
        var detail: SessionDetail
        var events: [SessionEvent]
        var loadedProjectionItemCount: Int
        var totalProjectionItemCount: Int
        var tailSnapshotEventId: Int?
        /// Last realtime pubsub sequence we rendered. Persisted for M2 cheap
        /// stream replay; unused (nil) in M1.
        var lastPubsubSeq: Int?
        var savedAt: Date
    }

    static let shared = TranscriptSnapshotStore()

    private let directory: URL
    private let ttl: TimeInterval
    private let maxFiles: Int
    private let maxBytesPerFile: Int
    private let io: DispatchQueue

    /// `FileManager.default` is thread-safe for the file operations used here,
    /// so we reference it inline rather than storing it (storing the non-Sendable
    /// instance would break this struct's `Sendable` conformance under strict
    /// concurrency).

    /// - Parameters:
    ///   - directory: storage root. Defaults to
    ///     `Application Support/TranscriptSnapshots`. Tests inject a temp dir.
    ///   - ttl: snapshots older than this are treated as absent and pruned.
    ///   - maxFiles: LRU cap on retained sessions.
    ///   - maxBytesPerFile: oversized snapshots are dropped rather than stored,
    ///     so one pathological session can't blow up the cache.
    init(
        directory: URL? = nil,
        ttl: TimeInterval = 14 * 24 * 60 * 60,
        maxFiles: Int = 40,
        maxBytesPerFile: Int = 6 * 1024 * 1024
    ) {
        self.ttl = ttl
        self.maxFiles = maxFiles
        self.maxBytesPerFile = maxBytesPerFile
        self.io = DispatchQueue(label: "ai.longhouse.transcript-snapshot-store")
        if let directory {
            self.directory = directory
        } else {
            let base = (try? FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )) ?? FileManager.default.temporaryDirectory
            self.directory = base.appendingPathComponent("TranscriptSnapshots", isDirectory: true)
        }
        ensureDirectory()
    }

    // MARK: - Read

    /// Returns the persisted snapshot for a session, or nil if absent, expired,
    /// schema-mismatched, or unreadable. Expired/garbage files are removed.
    func load(serverURL: String, sessionId: String, now: Date = Date()) -> Snapshot? {
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let snapshot = try? Self.decoder.decode(Snapshot.self, from: data) else {
            // Corrupt or old-schema file: drop it so it never trips us again.
            removeFile(at: url)
            return nil
        }
        guard snapshot.schemaVersion == Self.schemaVersion else {
            removeFile(at: url)
            return nil
        }
        guard now.timeIntervalSince(snapshot.savedAt) <= ttl else {
            removeFile(at: url)
            return nil
        }
        return snapshot
    }

    // MARK: - Write

    /// Persist a snapshot asynchronously. Oversized payloads are dropped.
    func save(
        serverURL: String,
        sessionId: String,
        detail: SessionDetail,
        events: [SessionEvent],
        loadedProjectionItemCount: Int,
        totalProjectionItemCount: Int,
        tailSnapshotEventId: Int?,
        lastPubsubSeq: Int?,
        savedAt: Date = Date()
    ) {
        let snapshot = Snapshot(
            schemaVersion: Self.schemaVersion,
            serverURL: Self.normalize(serverURL),
            sessionId: sessionId,
            detail: detail,
            events: events,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId,
            lastPubsubSeq: lastPubsubSeq,
            savedAt: savedAt
        )
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        let maxBytes = maxBytesPerFile
        let limit = maxFiles
        io.async {
            guard let data = try? Self.encoder.encode(snapshot) else { return }
            guard data.count <= maxBytes else {
                try? FileManager.default.removeItem(at: url)
                return
            }
            self.ensureDirectory()
            do {
                try data.write(to: url, options: .atomic)
                Self.excludeFromBackup(url)
            } catch {
                return
            }
            self.evictIfNeeded(limit: limit)
        }
    }

    // MARK: - Eviction / clearing

    func remove(serverURL: String, sessionId: String) {
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        io.async { try? FileManager.default.removeItem(at: url) }
    }

    /// Remove every snapshot belonging to a server (sign-out / server switch).
    func clear(serverURL: String) {
        let target = Self.normalize(serverURL)
        io.async {
            let files = (try? FileManager.default.contentsOfDirectory(
                at: self.directory,
                includingPropertiesForKeys: nil
            )) ?? []
            for file in files where file.pathExtension == "json" {
                guard
                    let data = try? Data(contentsOf: file),
                    let snapshot = try? Self.decoder.decode(Snapshot.self, from: data)
                else {
                    try? FileManager.default.removeItem(at: file)
                    continue
                }
                if snapshot.serverURL == target {
                    try? FileManager.default.removeItem(at: file)
                }
            }
        }
    }

    func clearAll() {
        io.async {
            try? FileManager.default.removeItem(at: self.directory)
            self.ensureDirectory()
        }
    }

    /// Flush pending writes. Tests call this to make async writes observable.
    func waitForPendingWrites() {
        io.sync {}
    }

    // MARK: - Internals

    private func ensureDirectory() {
        guard !FileManager.default.fileExists(atPath: directory.path) else { return }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [
            .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
        ])
        Self.excludeFromBackup(directory)
    }

    private func fileURL(serverURL: String, sessionId: String) -> URL {
        let key = "\(Self.normalize(serverURL))|\(sessionId)"
        let digest = SHA256.hash(data: Data(key.utf8))
        let name = digest.map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent("\(name).json", isDirectory: false)
    }

    /// Enforce the LRU file cap by deleting the oldest-modified snapshots.
    private func evictIfNeeded(limit: Int) {
        guard limit > 0 else { return }
        let keys: [URLResourceKey] = [.contentModificationDateKey]
        let files = (try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: keys
        ))?.filter { $0.pathExtension == "json" } ?? []
        guard files.count > limit else { return }
        let sorted = files.sorted { lhs, rhs in
            let l = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
            let r = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
            return l < r
        }
        for file in sorted.prefix(files.count - limit) {
            try? FileManager.default.removeItem(at: file)
        }
    }

    private func removeFile(at url: URL) {
        io.async { try? FileManager.default.removeItem(at: url) }
    }

    private static func normalize(_ serverURL: String) -> String {
        var value = serverURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }

    private static func excludeFromBackup(_ url: URL) {
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutable = url
        try? mutable.setResourceValues(values)
    }

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}
