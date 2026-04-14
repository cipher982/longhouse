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

            if entry.sessions.isEmpty && !entry.isPlaceholder {
                Text("All clear")
                    .font(.system(size: 20, weight: .semibold))
                Text("No sessions need attention")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else {
                Text("\(entry.totalActive)")
                    .font(.system(size: 36, weight: .bold))
                    .foregroundStyle(.orange)
                Text(entry.totalActive == 1 ? "session waiting" : "sessions waiting")
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
                    Text("\(entry.totalActive) waiting")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.orange)
                }
            }
            .padding(.bottom, 8)

            if entry.sessions.isEmpty && !entry.isPlaceholder {
                Spacer()
                HStack {
                    Spacer()
                    VStack(spacing: 4) {
                        Image(systemName: "checkmark.circle")
                            .font(.system(size: 24))
                            .foregroundStyle(.green)
                        Text("All clear")
                            .font(.system(size: 13, weight: .medium))
                        Text("No sessions need attention")
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
}

struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(session.isBlocked ? .red : .orange)
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
                    Text(session.attentionLabel)
                        .font(.system(size: 10))
                        .foregroundStyle(session.isBlocked ? .red.opacity(0.8) : .orange.opacity(0.8))
                }
            }

            Spacer()
        }
    }
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
