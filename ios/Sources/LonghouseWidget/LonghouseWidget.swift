import ActivityKit
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
            SessionSummary(
                id: "1",
                title: "Debugging Codex Launch Path Bug",
                presenceState: "thinking",
                provider: "codex",
                project: "zerg",
                lastActivityAt: nil,
                status: "working",
                runtimeDisplay: SessionRuntimeDisplay.widgetPlaceholder(state: "thinking", phase: "Thinking", tone: "thinking")
            ),
            SessionSummary(
                id: "2",
                title: "Simple Arithmetic Calculation",
                presenceState: "idle",
                provider: "antigravity",
                project: "antigravity",
                lastActivityAt: nil,
                status: "completed",
                runtimeDisplay: SessionRuntimeDisplay.widgetPlaceholder(state: nil, phase: "Closed", tone: "closed", lifecycle: "closed")
            ),
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
        SessionWatchLiveActivityWidget()
        #if compiler(>=6.2)
        if #available(iOSApplicationExtension 26.0, *) {
            PushSessionsWidget()
        }
        #endif
    }
}

struct SessionsWidget: Widget {
    let kind = LonghouseWidgetConstants.sessionsKind

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SessionProvider()) { entry in
            SessionsWidgetView(entry: entry)
                // Always the hearth at night, like the Live Activity: adaptive
                // colors are not trusted to resolve against the Home Screen.
                .environment(\.colorScheme, .dark)
                .containerBackground(for: .widget) {
                    // Keep the widget background on APIs available in the CI toolchain.
                    // Restore glass-specific styling once the build fleet supports that SDK.
                    EmberWidgetGround()
                }
        }
        .configurationDisplayName("Timeline")
        .description("Most recent active sessions")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct SessionWatchLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SessionWatchAttributes.self) { context in
            SessionWatchLiveActivityView(context: context)
                .activityBackgroundTint(Ember.uiHex(0x0B0908).opacity(0.86))
                .activitySystemActionForegroundColor(Ember.uiHex(0xE9B949))
                .widgetURL(sessionURL(context.attributes.sessionId))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Text(context.state.displayPhase)
                        .font(.headline)
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.attributes.project ?? context.attributes.provider)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(context.attributes.title)
                        .font(.caption)
                        .lineLimit(1)
                }
            } compactLeading: {
                Image(systemName: context.state.isAttention ? "exclamationmark.circle.fill" : "dot.radiowaves.left.and.right")
                    .foregroundStyle(context.state.isAttention ? Ember.uiHex(0xE4572E) : Ember.uiHex(0xF08A24))
            } compactTrailing: {
                Text(shortState(context.state.presenceState))
                    .font(.caption2.weight(.semibold))
            } minimal: {
                Image(systemName: context.state.isAttention ? "exclamationmark.circle.fill" : "dot.radiowaves.left.and.right")
                    .foregroundStyle(context.state.isAttention ? Ember.uiHex(0xE4572E) : Ember.uiHex(0xF08A24))
            }
            .widgetURL(sessionURL(context.attributes.sessionId))
        }
    }
}

private struct SessionWatchLiveActivityView: View {
    let context: ActivityViewContext<SessionWatchAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(context.state.isAttention ? Ember.uiHex(0xE4572E) : liveActivityStateColor(context.state.presenceState))
                    .frame(width: 9, height: 9)
                Text(context.state.displayPhase)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(Ember.uiHex(0xF6EBD6))
                    .lineLimit(1)
                Spacer(minLength: 8)
                ProviderGlyph(provider: context.attributes.provider, size: 16)
                Text(providerDisplayLabel(context.attributes.provider))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Ember.uiHex(0xC4B096))
            }
            Text(context.attributes.title)
                .font(Ember.serif(17, relativeTo: .subheadline))
                .foregroundStyle(Ember.uiHex(0xF6EBD6))
                .lineLimit(1)
            if let project = context.attributes.project {
                Text(project)
                    .font(.caption)
                    .foregroundStyle(Ember.uiHex(0xC4B096))
                    .lineLimit(1)
            }
        }
        .padding(14)
    }
}

private func sessionURL(_ sessionId: String) -> URL? {
    URL(string: "ai.longhouse.ios://session/\(sessionId)")
}

private func shortState(_ state: String) -> String {
    switch state {
    case "needs_user":
        return "Idle"
    case "unknown":
        return "Inactive"
    case "blocked":
        return "Hold"
    case "running":
        return "Run"
    case "thinking":
        return "Think"
    case "idle":
        return "Idle"
    default:
        return "?"
    }
}

private func liveActivityStateColor(_ state: String) -> Color {
    // The fire ramp: live work is flame, a hold is ember, rest cools to ash.
    switch state {
    case "running", "thinking", "executing":
        return Ember.uiHex(0xF08A24)
    case "blocked", "stalled":
        return Ember.uiHex(0xE4572E)
    default:
        return Ember.uiHex(0x7C8790)
    }
}

#if compiler(>=6.2)
// WidgetKit push APIs are introduced with the iOS 26 SDK. Runtime availability
// alone is not enough because older CI SDKs cannot type-check these symbols.
@available(iOSApplicationExtension 26.0, *)
struct PushSessionsWidget: Widget {
    let kind = LonghouseWidgetConstants.pushSessionsKind

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SessionProvider()) { entry in
            SessionsWidgetView(entry: entry)
                .environment(\.colorScheme, .dark)
                .containerBackground(for: .widget) {
                    // Keep the widget background on APIs available in the CI toolchain.
                    // Restore glass-specific styling once the build fleet supports that SDK.
                    EmberWidgetGround()
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
        guard let serverURL = SharedAuthStore.loadServerURL(),
              let api = LonghouseAPI(host: serverURL, allowsAuthRefresh: false) else {
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
#endif

/// Widget ground: soot under the hearth light, fixed dark like the Live Activity.
private struct EmberWidgetGround: View {
    var body: some View {
        ZStack {
            Ember.uiHex(0x0B0908)
            RadialGradient(
                colors: [Ember.uiHex(0xE9B949).opacity(0.14), .clear],
                center: UnitPoint(x: 0.2, y: -0.1),
                startRadius: 0,
                endRadius: 240
            )
        }
    }
}
