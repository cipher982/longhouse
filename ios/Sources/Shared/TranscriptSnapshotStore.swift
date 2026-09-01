import CryptoKit
import Foundation

/// The only transcript cache. Two tiers, one snapshot, one key.
///
/// The in-process tier answers a reopen inside the same launch from RAM. The
/// on-disk tier — one JSON file per `(serverURL|sessionId)` under Application
/// Support, written atomically, excluded from iCloud/iTunes backup, protected
/// until first unlock — answers a relaunch after iOS evicted the app, so a cold
/// open into a session renders the last-seen transcript instead of a blank
/// screen with a lone warning triangle.
///
/// This is a *derived* cache. The Runtime Host is authoritative; any snapshot
/// can be discarded and rebuilt from `/api/.../mobile-tail`.
///
/// Reads are synchronous (a dictionary lookup, or one small file on a cold
/// open). Disk writes are dispatched to a private serial queue so persistence
/// never blocks the UI.
struct TranscriptSnapshotStore: Sendable {
    /// Bump when the on-disk shape changes; mismatched files are ignored and
    /// pruned so a stale schema can never crash decode or render.
    static let schemaVersion = 3

    /// A restored snapshot and which tier served it, so the session-open
    /// waterfall can still tell a warm reopen from a cold relaunch.
    struct Restored: Sendable {
        enum Tier: String, Sendable {
            case memory
            case disk
        }

        let snapshot: TranscriptSnapshot
        let tier: Tier
    }

    /// On-disk envelope: the identity the file is keyed by, plus the snapshot.
    private struct StoredSnapshot: Codable, Sendable {
        var schemaVersion: Int
        var serverURL: String
        var sessionId: String
        var transcript: TranscriptSnapshot
    }

    static let shared = TranscriptSnapshotStore()

    private let directory: URL
    private let ttl: TimeInterval
    private let maxFiles: Int
    private let maxBytesPerFile: Int
    private let memory: MemoryTier
    private let io: DispatchQueue

    /// `FileManager.default` is thread-safe for the file operations used here,
    /// so we reference it inline rather than storing it (storing the non-Sendable
    /// instance would break this struct's `Sendable` conformance under strict
    /// concurrency).

    /// - Parameters:
    ///   - directory: storage root. Defaults to
    ///     `Application Support/TranscriptSnapshots`. Tests inject a temp dir.
    ///   - ttl: snapshots older than this are treated as absent and pruned.
    ///   - maxFiles: LRU cap on retained sessions on disk.
    ///   - maxBytesPerFile: oversized snapshots are dropped rather than stored,
    ///     so one pathological session can't blow up the cache.
    ///   - memoryMaxBytes: LRU cap on the in-process tier. Zero disables it,
    ///     which is how a test forces the cold-relaunch path.
    ///   - memoryTTL: how long a snapshot stays warm. Shorter than `ttl`: RAM
    ///     is for the reopen you just navigated away from.
    init(
        directory: URL? = nil,
        ttl: TimeInterval = 14 * 24 * 60 * 60,
        maxFiles: Int = 40,
        maxBytesPerFile: Int = 6 * 1024 * 1024,
        memoryMaxBytes: Int = 12 * 1024 * 1024,
        memoryTTL: TimeInterval = 60 * 60
    ) {
        self.ttl = ttl
        self.maxFiles = maxFiles
        self.maxBytesPerFile = maxBytesPerFile
        self.memory = MemoryTier(maxBytes: memoryMaxBytes, ttl: memoryTTL)
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

    /// Returns the snapshot for a session, or nil if absent, expired,
    /// schema-mismatched, or unreadable. Expired/garbage files are removed.
    func load(serverURL: String, sessionId: String, now: Date = Date()) -> Restored? {
        let key = TranscriptSnapshot.cacheKey(serverURL: serverURL, sessionId: sessionId)
        if let warm = memory.snapshot(forKey: key, now: now) {
            return Restored(snapshot: warm, tier: .memory)
        }
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let stored = try? Self.decoder.decode(StoredSnapshot.self, from: data) else {
            // Corrupt or old-schema file: drop it so it never trips us again.
            removeFile(at: url)
            return nil
        }
        guard stored.schemaVersion == Self.schemaVersion else {
            removeFile(at: url)
            return nil
        }
        guard now.timeIntervalSince(stored.transcript.savedAt) <= ttl else {
            removeFile(at: url)
            return nil
        }
        // A relaunch pays for the decode once; the next reopen comes from RAM.
        memory.store(stored.transcript, forKey: key, now: now)
        return Restored(snapshot: stored.transcript, tier: .disk)
    }

    // MARK: - Write

    /// Keep a snapshot warm and persist it. The disk write is asynchronous;
    /// oversized payloads are dropped rather than stored.
    func save(serverURL: String, sessionId: String, snapshot: TranscriptSnapshot) {
        let key = TranscriptSnapshot.cacheKey(serverURL: serverURL, sessionId: sessionId)
        memory.store(snapshot, forKey: key, now: snapshot.savedAt)
        let stored = StoredSnapshot(
            schemaVersion: Self.schemaVersion,
            serverURL: TranscriptSnapshot.normalizedServerURL(serverURL),
            sessionId: sessionId,
            transcript: snapshot
        )
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        let maxBytes = maxBytesPerFile
        let limit = maxFiles
        io.async {
            guard let data = try? Self.encoder.encode(stored) else { return }
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
        memory.remove(forKey: TranscriptSnapshot.cacheKey(serverURL: serverURL, sessionId: sessionId))
        let url = fileURL(serverURL: serverURL, sessionId: sessionId)
        io.async { try? FileManager.default.removeItem(at: url) }
    }

    /// Remove every snapshot belonging to a server (sign-out / server switch).
    func clear(serverURL: String) {
        let target = TranscriptSnapshot.normalizedServerURL(serverURL)
        memory.removeAll(withKeyPrefix: "\(target)|")
        io.async {
            let files = (try? FileManager.default.contentsOfDirectory(
                at: self.directory,
                includingPropertiesForKeys: nil
            )) ?? []
            for file in files where file.pathExtension == "json" {
                guard
                    let data = try? Data(contentsOf: file),
                    let stored = try? Self.decoder.decode(StoredSnapshot.self, from: data)
                else {
                    try? FileManager.default.removeItem(at: file)
                    continue
                }
                if stored.serverURL == target {
                    try? FileManager.default.removeItem(at: file)
                }
            }
        }
    }

    func clearAll() {
        memory.removeAll()
        io.async {
            try? FileManager.default.removeItem(at: self.directory)
            self.ensureDirectory()
        }
    }

    /// Flush pending writes. Tests call this to make async writes observable.
    func waitForPendingWrites() {
        io.sync {}
    }

    // MARK: - In-process tier

    /// Bounded LRU over snapshots kept for the life of the process. Guarded by
    /// a lock rather than an actor because the session-open paint path reads it
    /// synchronously.
    private final class MemoryTier: @unchecked Sendable {
        private struct Entry {
            let snapshot: TranscriptSnapshot
            let estimatedBytes: Int
            var lastAccessedAt: Date
        }

        private let lock = NSLock()
        private let maxBytes: Int
        private let ttl: TimeInterval
        private var entries: [String: Entry] = [:]
        private var totalBytes = 0

        init(maxBytes: Int, ttl: TimeInterval) {
            self.maxBytes = maxBytes
            self.ttl = ttl
        }

        func snapshot(forKey key: String, now: Date) -> TranscriptSnapshot? {
            lock.lock()
            defer { lock.unlock() }
            pruneExpired(now: now)
            guard var entry = entries[key] else { return nil }
            entry.lastAccessedAt = now
            entries[key] = entry
            return entry.snapshot
        }

        func store(_ snapshot: TranscriptSnapshot, forKey key: String, now: Date) {
            guard maxBytes > 0 else { return }
            let estimatedBytes = snapshot.estimatedBytes
            lock.lock()
            defer { lock.unlock() }
            remove(key)
            guard estimatedBytes <= maxBytes else { return }
            entries[key] = Entry(snapshot: snapshot, estimatedBytes: estimatedBytes, lastAccessedAt: now)
            totalBytes += estimatedBytes
            while totalBytes > maxBytes,
                  let victim = entries.min(by: { $0.value.lastAccessedAt < $1.value.lastAccessedAt })?.key {
                remove(victim)
            }
        }

        func remove(forKey key: String) {
            lock.lock()
            defer { lock.unlock() }
            remove(key)
        }

        func removeAll(withKeyPrefix prefix: String) {
            lock.lock()
            defer { lock.unlock() }
            for key in entries.keys where key.hasPrefix(prefix) {
                remove(key)
            }
        }

        func removeAll() {
            lock.lock()
            defer { lock.unlock() }
            entries.removeAll()
            totalBytes = 0
        }

        /// Callers hold `lock`.
        private func pruneExpired(now: Date) {
            for (key, entry) in entries where now.timeIntervalSince(entry.snapshot.savedAt) >= ttl {
                remove(key)
            }
        }

        /// Callers hold `lock`.
        private func remove(_ key: String) {
            guard let removed = entries.removeValue(forKey: key) else { return }
            totalBytes = max(0, totalBytes - removed.estimatedBytes)
        }
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
        let key = TranscriptSnapshot.cacheKey(serverURL: serverURL, sessionId: sessionId)
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
