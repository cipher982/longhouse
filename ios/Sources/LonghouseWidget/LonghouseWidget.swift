import SwiftUI
import WidgetKit

private enum LonghouseWidgetConstants {
    static let sessionsKind = "LonghouseSessionsWidget"
    static let pushSessionsKind = "LonghouseSessionsWidgetLive"
}

struct SessionEntry: TimelineEntry {
    let date: Date
    let sessions: [SessionSummary]
    let totalActive: Int
    let isPlaceholder: Bool
    let isSignedIn: Bool
    let statusTitle: String?
    let statusMessage: String?

    static let placeholder = SessionEntry(
        date: .now,
        sessions: [
            SessionSummary(id: "1", title: "Debugging Codex Launch Path Bug", presenceState: "thinking", provider: "codex", project: "zerg", lastActivityAt: nil, status: "working"),
            SessionSummary(id: "2", title: "Simple Arithmetic Calculation", presenceState: "idle", provider: "gemini", project: "gemini", lastActivityAt: nil, status: "completed"),
        ],
        totalActive: 2,
        isPlaceholder: true,
        isSignedIn: true,
        statusTitle: nil,
        statusMessage: nil
    )

    static let empty = SessionEntry(
        date: .now,
        sessions: [],
        totalActive: 0,
        isPlaceholder: false,
        isSignedIn: true,
        statusTitle: nil,
        statusMessage: nil
    )

    static func unavailable(title: String, message: String) -> SessionEntry {
        SessionEntry(
            date: .now,
            sessions: [],
            totalActive: 0,
            isPlaceholder: false,
            isSignedIn: false,
            statusTitle: title,
            statusMessage: message
        )
    }
}

struct SessionProvider: TimelineProvider {
    func placeholder(in context: Context) -> SessionEntry {
        .placeholder
    }

    func getSnapshot(in context: Context, completion: @escaping @Sendable (SessionEntry) -> Void) {
        if context.isPreview {
            completion(.placeholder)
            return
        }
        Task { @Sendable in
            let entry = await fetchSessions()
            completion(entry)
        }
    }

    func getTimeline(in context: Context, completion: @escaping @Sendable (Timeline<SessionEntry>) -> Void) {
        Task { @Sendable in
            let entry = await fetchSessions()
            let nextUpdate = Calendar.current.date(byAdding: .minute, value: 2, to: .now)!
            completion(Timeline(entries: [entry], policy: .after(nextUpdate)))
        }
    }

    private func fetchSessions() async -> SessionEntry {
        let result = await WidgetSessionLoader.load()
        if result.isSignedIn {
            return SessionEntry(
                date: .now,
                sessions: result.sessions,
                totalActive: result.totalActive,
                isPlaceholder: false,
                isSignedIn: true,
                statusTitle: nil,
                statusMessage: nil
            )
        }

        if let title = result.statusTitle, let message = result.statusMessage {
            return .unavailable(title: title, message: message)
        }

        return .empty
    }
}

@main
struct LonghouseWidgets: WidgetBundle {
    var body: some Widget {
        SessionsWidget()
        if #available(iOSApplicationExtension 26.0, *) {
            PushSessionsWidget()
        }
    }
}

struct SessionsWidget: Widget {
    let kind = LonghouseWidgetConstants.sessionsKind

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SessionProvider()) { entry in
            SessionsWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    // Keep the widget background on APIs available in the CI toolchain.
                    // Restore glass-specific styling once the build fleet supports that SDK.
                    Color(.systemFill).opacity(0.6)
                }
        }
        .configurationDisplayName("Timeline")
        .description("Most recent active sessions")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

@available(iOSApplicationExtension 26.0, *)
struct PushSessionsWidget: Widget {
    let kind = LonghouseWidgetConstants.pushSessionsKind

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SessionProvider()) { entry in
            SessionsWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    // Keep the widget background on APIs available in the CI toolchain.
                    // Restore glass-specific styling once the build fleet supports that SDK.
                    Color(.systemFill).opacity(0.6)
                }
        }
        .configurationDisplayName("Timeline Live")
        .description("Most recent active sessions")
        .supportedFamilies([.systemSmall, .systemMedium])
        .pushHandler(SessionsWidgetPushHandler.self)
    }
}

@available(iOSApplicationExtension 26.0, *)
struct SessionsWidgetPushHandler: WidgetPushHandler {
    func pushTokenDidChange(_ pushInfo: WidgetPushInfo, widgets: [WidgetInfo]) {
        guard widgets.contains(where: { $0.kind == LonghouseWidgetConstants.pushSessionsKind }) else {
            return
        }
        guard let serverURL = SharedAuthStore.loadServerURL(), let api = LonghouseAPI(host: serverURL) else {
            return
        }

        let token = pushInfo.token.map { String(format: "%02x", $0) }.joined()
        Task {
            try? await api.registerAPNSDevice(
                deviceToken: token,
                pushEnvironment: widgetPushEnvironment,
                appBuildId: nil,
                platform: "ios_widget"
            )
        }
    }

    private var widgetPushEnvironment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }
}
