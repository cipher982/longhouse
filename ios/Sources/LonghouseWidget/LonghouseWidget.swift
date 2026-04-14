import SwiftUI
import WidgetKit

struct SessionEntry: TimelineEntry {
    let date: Date
    let sessions: [SessionSummary]
    let totalActive: Int
    let isPlaceholder: Bool

    static let placeholder = SessionEntry(
        date: .now,
        sessions: [
            SessionSummary(id: "1", title: "Fixing auth flow in login", presenceState: "needs_user", provider: "claude", project: "longhouse", lastActivityAt: nil),
            SessionSummary(id: "2", title: "Deploy pipeline stuck", presenceState: "blocked", provider: "claude", project: "zerg", lastActivityAt: nil),
        ],
        totalActive: 2,
        isPlaceholder: true
    )

    static let empty = SessionEntry(date: .now, sessions: [], totalActive: 0, isPlaceholder: false)
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
        guard let serverURL = KeychainHelper.loadServerURL(),
              let authToken = KeychainHelper.loadAuthToken() else {
            return .empty
        }

        let api = LonghouseAPI(host: serverURL)
        do {
            let sessions = try await api.sessionsNeedingAttention(authToken: authToken)
            return SessionEntry(date: .now, sessions: Array(sessions.prefix(3)), totalActive: sessions.count, isPlaceholder: false)
        } catch {
            return .empty
        }
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
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Sessions")
        .description("Sessions needing your attention")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
