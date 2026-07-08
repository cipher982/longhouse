import Foundation
import OSLog

struct WidgetLoadResult: Sendable {
    let sessions: [SessionSummary]
    let totalActive: Int
    let isSignedIn: Bool
    let statusTitle: String?
    let statusMessage: String?
    let debugState: SharedAuthDebugState

    static func loaded(
        sessions: [SessionSummary],
        totalActive: Int? = nil,
        debugState: SharedAuthDebugState
    ) -> WidgetLoadResult {
        WidgetLoadResult(
            sessions: SessionSummary.attentionWidgetOrder(sessions, limit: 3),
            totalActive: totalActive ?? sessions.filter(\.isUserActive).count,
            isSignedIn: true,
            statusTitle: nil,
            statusMessage: nil,
            debugState: debugState
        )
    }

    static func unavailable(
        title: String,
        message: String,
        debugState: SharedAuthDebugState
    ) -> WidgetLoadResult {
        WidgetLoadResult(
            sessions: [],
            totalActive: 0,
            isSignedIn: false,
            statusTitle: title,
            statusMessage: message,
            debugState: debugState
        )
    }

    static func empty(debugState: SharedAuthDebugState) -> WidgetLoadResult {
        WidgetLoadResult(
            sessions: [],
            totalActive: 0,
            isSignedIn: true,
            statusTitle: nil,
            statusMessage: nil,
            debugState: debugState
        )
    }
}

struct WidgetSessionSnapshot: Codable, Sendable {
    let sessions: [SessionSummary]
    let totalActive: Int
    let savedAt: Date
}

enum WidgetSessionSnapshotStore {
    private static let snapshotKey = "longhouse.widget.sessions.snapshot.v2"

    static func save(sessions: [SessionSummary], defaults: UserDefaults? = sharedDefaults) {
        let active = sessions.filter(\.isUserActive)
        let snapshot = WidgetSessionSnapshot(
            sessions: active,
            totalActive: active.count,
            savedAt: Date()
        )
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: snapshotKey)
    }

    static func load(defaults: UserDefaults? = sharedDefaults) -> WidgetSessionSnapshot? {
        guard let data = defaults?.data(forKey: snapshotKey) else { return nil }
        return try? JSONDecoder().decode(WidgetSessionSnapshot.self, from: data)
    }

    static func clear(defaults: UserDefaults? = sharedDefaults) {
        defaults?.removeObject(forKey: snapshotKey)
    }

    private static var sharedDefaults: UserDefaults? {
        UserDefaults(suiteName: SharedAuthStore.appGroupIdentifier)
    }
}

enum WidgetSessionLoader {
    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "WidgetAuth")

    static func load() async -> WidgetLoadResult {
        let initialState = SharedAuthStore.debugState(for: nil)
        guard initialState.appGroupAvailable else {
            logger.error("Widget auth unavailable: app group missing")
            return .unavailable(
                title: "Shared storage unavailable",
                message: "The widget cannot access Longhouse shared data.",
                debugState: initialState
            )
        }

        guard let serverURL = initialState.serverURL else {
            logger.error("Widget auth unavailable: missing shared server URL")
            return .unavailable(
                title: "Missing server",
                message: "Open Longhouse and set your server.",
                debugState: initialState
            )
        }

        let debugState = SharedAuthStore.debugState(for: serverURL)
        guard debugState.hasCredentials else {
            logger.error("Widget auth unavailable: no shared credentials for \(serverURL, privacy: .public)")
            return .unavailable(
                title: "No shared session",
                message: "Open Longhouse to sign in.",
                debugState: debugState
            )
        }

        guard let api = LonghouseAPI(host: serverURL, allowsAuthRefresh: false) else {
            logger.error("Widget auth unavailable: invalid server URL \(serverURL, privacy: .public)")
            return .unavailable(
                title: "Invalid server URL",
                message: "Open Longhouse and update your server.",
                debugState: debugState
            )
        }

        do {
            let sessions = try await api.recentActiveSessions(limit: 8)
            WidgetSessionSnapshotStore.save(sessions: sessions)
            logger.log("Widget loaded \(sessions.count, privacy: .public) sessions")
            return .loaded(sessions: sessions, debugState: SharedAuthStore.debugState(for: serverURL))
        } catch LonghouseAPIError.notAuthenticated {
            logger.log("Widget session unavailable: app refresh required")
            return cachedResult(debugState: SharedAuthStore.debugState(for: serverURL))
        } catch {
            logger.error("Widget fetch failed: \(error.localizedDescription, privacy: .public)")
            return cachedResult(debugState: SharedAuthStore.debugState(for: serverURL))
        }
    }

    private static func cachedResult(debugState: SharedAuthDebugState) -> WidgetLoadResult {
        guard let snapshot = WidgetSessionSnapshotStore.load(), !snapshot.sessions.isEmpty else {
            return .empty(debugState: debugState)
        }
        return .loaded(sessions: snapshot.sessions, totalActive: snapshot.totalActive, debugState: debugState)
    }

    static func logProbeResult(_ result: WidgetLoadResult, source: String) {
        let serverURL = result.debugState.serverURL ?? "nil"
        let title = result.statusTitle ?? "nil"
        let message = result.statusMessage ?? "nil"
        let summary = "\(source): appGroup=\(result.debugState.appGroupAvailable) server=\(serverURL) cookies=\(result.debugState.cookieCount) runtime=\(result.debugState.hasRuntimeToken) nativeRefresh=\(result.debugState.hasNativeRefreshToken) signedIn=\(result.isSignedIn) title=\(title) message=\(message)"
        logger.log("\(summary, privacy: .public)")
    }
}
