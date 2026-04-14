import Foundation
import OSLog

struct WidgetLoadResult: Sendable {
    let sessions: [SessionSummary]
    let totalActive: Int
    let isSignedIn: Bool
    let statusTitle: String?
    let statusMessage: String?
    let debugState: SharedAuthDebugState

    static func loaded(sessions: [SessionSummary], debugState: SharedAuthDebugState) -> WidgetLoadResult {
        WidgetLoadResult(
            sessions: Array(sessions.prefix(3)),
            totalActive: sessions.count,
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
        guard debugState.cookieCount > 0 else {
            logger.error("Widget auth unavailable: no shared cookies for \(serverURL, privacy: .public)")
            return .unavailable(
                title: "No shared session",
                message: "Open Longhouse to refresh widget auth.",
                debugState: debugState
            )
        }

        let api = LonghouseAPI(host: serverURL)
        do {
            let sessions = try await api.sessionsNeedingAttention()
            logger.log("Widget loaded \(sessions.count, privacy: .public) sessions")
            return .loaded(sessions: sessions, debugState: SharedAuthStore.debugState(for: serverURL))
        } catch LonghouseAPIError.notAuthenticated {
            logger.log("Widget session expired, attempting refresh")
            do {
                try await api.refreshSession()
                let sessions = try await api.sessionsNeedingAttention()
                logger.log("Widget refresh succeeded with \(sessions.count, privacy: .public) sessions")
                return .loaded(sessions: sessions, debugState: SharedAuthStore.debugState(for: serverURL))
            } catch LonghouseAPIError.notAuthenticated {
                SharedAuthStore.clearManagedCookies(for: serverURL)
                logger.error("Widget refresh failed: unauthenticated")
                return .unavailable(
                    title: "Session expired",
                    message: "Open Longhouse to sign in again.",
                    debugState: SharedAuthStore.debugState(for: serverURL)
                )
            } catch {
                logger.error("Widget refresh failed: \(error.localizedDescription, privacy: .public)")
                return .empty(debugState: SharedAuthStore.debugState(for: serverURL))
            }
        } catch {
            logger.error("Widget fetch failed: \(error.localizedDescription, privacy: .public)")
            return .empty(debugState: SharedAuthStore.debugState(for: serverURL))
        }
    }

    static func logProbeResult(_ result: WidgetLoadResult, source: String) {
        let serverURL = result.debugState.serverURL ?? "nil"
        let title = result.statusTitle ?? "nil"
        let message = result.statusMessage ?? "nil"
        let summary = "\(source): appGroup=\(result.debugState.appGroupAvailable) server=\(serverURL) cookies=\(result.debugState.cookieCount) signedIn=\(result.isSignedIn) title=\(title) message=\(message)"
        logger.log("\(summary, privacy: .public)")
    }
}
