import Foundation

struct CachedTimelineSnapshot: Sendable {
    let sessions: [SessionSummary]
    let savedAt: Date
}

enum TimelineCacheStore {
    private static let cacheKey = "longhouse.timeline.sessions.cache.v1"
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
        defaults: UserDefaults = .standard,
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
        defaults.set(data, forKey: cacheKey)
    }

    static func load(
        serverURL: String,
        identity: String? = nil,
        defaults: UserDefaults = .standard,
        now: Date = Date(),
        maxAge: TimeInterval = defaultMaxAge
    ) -> CachedTimelineSnapshot? {
        guard let data = defaults.data(forKey: cacheKey),
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            return nil
        }
        guard payload.version == version else { return nil }
        guard payload.serverURL == normalize(serverURL) else { return nil }
        guard payload.identity == normalizedIdentity(identity) else { return nil }
        guard now.timeIntervalSince(payload.savedAt) <= maxAge else { return nil }
        guard !payload.sessions.isEmpty else { return nil }
        return CachedTimelineSnapshot(sessions: payload.sessions, savedAt: payload.savedAt)
    }

    static func clear(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: cacheKey)
    }

    static func clear(serverURL: String, defaults: UserDefaults = .standard) {
        guard let data = defaults.data(forKey: cacheKey),
              let payload = try? JSONDecoder().decode(Payload.self, from: data),
              payload.serverURL == normalize(serverURL) else {
            return
        }
        defaults.removeObject(forKey: cacheKey)
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
