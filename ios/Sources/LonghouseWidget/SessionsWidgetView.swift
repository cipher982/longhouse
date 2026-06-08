import SwiftUI
import WidgetKit

struct SessionsWidgetView: View {
    let entry: SessionEntry

    @Environment(\.widgetFamily) var family

    var body: some View {
        switch family {
        case .systemSmall:
            smallView
        default:
            mediumView
        }
    }

    private var smallView: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 4) {
                Image(systemName: "house.lodge.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text("Longhouse")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if !entry.isSignedIn && !entry.isPlaceholder {
                Image(systemName: "person.crop.circle.badge.questionmark")
                    .font(.system(size: 24))
                    .foregroundStyle(.secondary)
                Text(entry.statusTitle ?? "Sign in to get started")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
            } else if entry.sessions.isEmpty && !entry.isPlaceholder {
                Text("No active sessions")
                    .font(.system(size: 20, weight: .semibold))
                Text("Longhouse is caught up")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else {
                Text("\(widgetMetric.count)")
                    .font(.system(size: 36, weight: .bold))
                    .foregroundStyle(widgetMetric.color)
                Text(widgetMetric.label)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }

            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var mediumView: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                HStack(spacing: 4) {
                    Image(systemName: "house.lodge.fill")
                        .font(.system(size: 11))
                    Text("Longhouse")
                        .font(.system(size: 11, weight: .medium))
                }
                .foregroundStyle(.secondary)

                Spacer()

                if !entry.sessions.isEmpty || entry.isPlaceholder {
                    Text("\(widgetMetric.count) \(widgetMetric.shortLabel)")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(widgetMetric.color)
                }
            }
            .padding(.bottom, 8)

            if !entry.isSignedIn && !entry.isPlaceholder {
                Spacer()
                HStack {
                    Spacer()
                    VStack(spacing: 4) {
                        Image(systemName: "person.crop.circle.badge.questionmark")
                            .font(.system(size: 24))
                            .foregroundStyle(.secondary)
                        Text(entry.statusTitle ?? "Not signed in")
                            .font(.system(size: 13, weight: .medium))
                        Text(entry.statusMessage ?? "Open Longhouse to sign in")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    Spacer()
                }
                Spacer()
            } else if entry.sessions.isEmpty && !entry.isPlaceholder {
                Spacer()
                HStack {
                    Spacer()
                    VStack(spacing: 4) {
                        Image(systemName: "checkmark.circle")
                            .font(.system(size: 24))
                            .foregroundStyle(.green)
                        Text("No active sessions")
                            .font(.system(size: 13, weight: .medium))
                        Text("Longhouse is caught up")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                Spacer()
            } else {
                VStack(spacing: 6) {
                    ForEach(entry.sessions) { session in
                        SessionRow(session: session)
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var widgetMetric: WidgetMetric {
        let attentionCount = entry.sessions.filter(\.needsAttention).count
        if attentionCount > 0 {
            return WidgetMetric(
                count: attentionCount,
                label: attentionCount == 1 ? "needs permission" : "need permission",
                shortLabel: "permission",
                color: TimelineSignal.amber
            )
        }
        return WidgetMetric(
            count: entry.totalActive,
            label: entry.totalActive == 1 ? "active session" : "active sessions",
            shortLabel: "active",
            color: .blue
        )
    }
}

struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        // Same 3-stop attention signal as the in-app card (shared TimelineSignal):
        // amber = waiting on you, teal = live work, grey = quiet/closed. Widgets
        // don't animate, so the dot is always static — color carries the signal.
        let signal = TimelineSignal.resolve(for: session)

        HStack(spacing: 8) {
            Circle()
                .fill(signal.dotColor)
                .frame(width: 6, height: 6)

            VStack(alignment: .leading, spacing: 1) {
                Text(session.title)
                    .font(.system(size: 12, weight: .medium))
                    .lineLimit(1)

                HStack(spacing: 4) {
                    if let project = session.project {
                        Text(project)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                    Text(session.displayPhaseLabel)
                        .font(.system(size: 10))
                        .foregroundStyle(signal.statusColor.opacity(0.85))
                }
            }

            Spacer()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(session.title), \(signal.accessibilityState)")
    }
}

private struct WidgetMetric {
    let count: Int
    let label: String
    let shortLabel: String
    let color: Color
}

#Preview("Medium - Sessions", as: .systemMedium) {
    SessionsWidget()
} timeline: {
    SessionEntry.placeholder
}

#Preview("Medium - Empty", as: .systemMedium) {
    SessionsWidget()
} timeline: {
    SessionEntry.empty
}

#Preview("Small - Sessions", as: .systemSmall) {
    SessionsWidget()
} timeline: {
    SessionEntry.placeholder
}

#Preview("Small - Empty", as: .systemSmall) {
    SessionsWidget()
} timeline: {
    SessionEntry.empty
}
