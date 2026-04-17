import SwiftUI
import WidgetKit

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
            SessionSummary(id: "1", title: "Fixing auth flow in login", presenceState: "needs_user", provider: "claude", project: "longhouse", lastActivityAt: nil),
            SessionSummary(id: "2", title: "Deploy pipeline stuck", presenceState: "blocked", provider: "claude", project: "zerg", lastActivityAt: nil),
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
            let nextUpdate = Calendar.current.date(byAdding: .minute, value: 5, to: .now)!
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
    }
}

struct SessionsWidget: Widget {
    let kind = "LonghouseSessionsWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SessionProvider()) { entry in
            SessionsWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    // Keep the widget background on APIs available in the CI toolchain.
                    // Restore glass-specific styling once the build fleet supports that SDK.
                    Color(.systemFill).opacity(0.6)
                }
        }
        .configurationDisplayName("Sessions")
        .description("Sessions needing your attention")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
