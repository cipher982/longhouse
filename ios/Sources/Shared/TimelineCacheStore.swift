import Foundation

struct CachedTimelineSnapshot: Sendable {
    let sessions: [SessionSummary]
    let savedAt: Date
}

/// Disk cache of the most-recent timeline rows.
///
/// Rows carry transcript-derived text (`summary`, `firstUserMessage`,
/// `matchSnippet`), so this follows ``TranscriptSnapshotStore`` rather than
/// `UserDefaults`: one JSON file under Application Support, protected until
/// first unlock and excluded from backup. `UserDefaults` rides an encrypted
/// backup and restores onto a different device.
enum TimelineCacheStore {
    private static let directoryName = "TimelineCache"
    private static let fileName = "sessions.json"
    private static let version = 1
    private static let maxSessions = 40
    private static let defaultMaxAge: TimeInterval = 24 * 60 * 60

    private struct Payload: Codable {
        let version: Int
        let serverURL: String
        let identity: String?
        let savedAt: Date
        let sessions: [SessionSummary]
    }

    static func save(
        sessions: [SessionSummary],
        serverURL: String,
        identity: String? = nil,
        directory: URL? = nil,
        now: Date = Date()
    ) {
        let normalizedServer = normalize(serverURL)
        guard !normalizedServer.isEmpty else { return }
        let payload = Payload(
            version: version,
            serverURL: normalizedServer,
            identity: normalizedIdentity(identity),
            savedAt: now,
            sessions: Array(sessions.prefix(maxSessions))
        )
        guard let data = try? JSONEncoder().encode(payload) else { return }
        guard let directoryURL = directoryURL(directory) else { return }
        ensureDirectory(directoryURL)
        let url = directoryURL.appendingPathComponent(fileName, isDirectory: false)
        do {
            try data.write(to: url, options: .atomic)
        } catch {
            return
        }
        excludeFromBackup(url)
    }

    static func load(
        serverURL: String,
        identity: String? = nil,
        directory: URL? = nil,
        now: Date = Date(),
        maxAge: TimeInterval = defaultMaxAge
    ) -> CachedTimelineSnapshot? {
        guard let payload = loadPayload(directory) else { return nil }
        guard payload.version == version else { return nil }
        guard payload.serverURL == normalize(serverURL) else { return nil }
        guard payload.identity == normalizedIdentity(identity) else { return nil }
        guard now.timeIntervalSince(payload.savedAt) <= maxAge else { return nil }
        guard !payload.sessions.isEmpty else { return nil }
        return CachedTimelineSnapshot(sessions: payload.sessions, savedAt: payload.savedAt)
    }

    static func clear(directory: URL? = nil) {
        guard let url = cacheFileURL(directory) else { return }
        try? FileManager.default.removeItem(at: url)
    }

    static func clear(serverURL: String, directory: URL? = nil) {
        guard let payload = loadPayload(directory),
              payload.serverURL == normalize(serverURL) else {
            return
        }
        clear(directory: directory)
    }

    private static func loadPayload(_ directory: URL?) -> Payload? {
        guard let url = cacheFileURL(directory),
              let data = try? Data(contentsOf: url) else {
            return nil
        }
        return try? JSONDecoder().decode(Payload.self, from: data)
    }

    private static func cacheFileURL(_ override: URL?) -> URL? {
        directoryURL(override)?.appendingPathComponent(fileName, isDirectory: false)
    }

    /// Storage root. Tests inject a temp dir.
    private static func directoryURL(_ override: URL?) -> URL? {
        if let override {
            return override
        }
        guard let base = try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ) else {
            return nil
        }
        return base.appendingPathComponent(directoryName, isDirectory: true)
    }

    private static func ensureDirectory(_ url: URL) {
        guard !FileManager.default.fileExists(atPath: url.path) else { return }
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [
            .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
        ])
        excludeFromBackup(url)
    }

    private static func excludeFromBackup(_ url: URL) {
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutable = url
        try? mutable.setResourceValues(values)
    }

    private static func normalize(_ serverURL: String) -> String {
        var value = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") {
            value.removeLast()
        }
        return value
    }

    private static func normalizedIdentity(_ identity: String?) -> String? {
        let value = identity?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }
}
