import SwiftUI
import AppKit

// Mirrors ios/Sources/LonghouseWidget/SessionsWidgetView.swift
// Keep in sync if widget layout changes.

struct SessionSummary: Identifiable {
    let id: String
    let title: String
    let presenceState: String
    let status: String?
    let provider: String?
    let project: String?
    var isBlocked: Bool { presenceState == "blocked" }
    var isUserActive: Bool { true }
    var needsAttention: Bool { isBlocked && isUserActive }
    var isExecuting: Bool {
        presenceState == "thinking"
            || presenceState == "running"
            || presenceState == "syncing_transcript"
            || status == "working"
            || status == "active"
    }
    var isIdle: Bool { presenceState == "idle" || status == "idle" }
    var displayPhaseLabel: String {
        switch presenceState {
        case "running": return "Running"
        case "thinking": return "Thinking"
        case "syncing_transcript": return "Working"
        case "needs_user": return "Idle"
        case "blocked": return "Needs permission"
        case "idle": return "Idle"
        default:
            if status == "completed" { return "Completed" }
            if status == "working" || status == "active" { return "Progress" }
            return "Inactive"
        }
    }
}

struct WidgetSmallView: View {
    let sessions: [SessionSummary]

    var body: some View {
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
            Text("\(widgetMetric.count)")
                .font(.system(size: 36, weight: .bold))
                .foregroundStyle(widgetMetric.color)
            Text(widgetMetric.label)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .frame(width: 170, height: 170)
        .background(Color.black.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    private var widgetMetric: WidgetMetric {
        buildWidgetMetric(sessions: sessions, totalActive: sessions.count)
    }
}

struct WidgetMediumView: View {
    let sessions: [SessionSummary]

    var body: some View {
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
                Text("\(widgetMetric.count) \(widgetMetric.shortLabel)")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(widgetMetric.color)
            }
            .padding(.bottom, 8)

            VStack(spacing: 6) {
                ForEach(sessions) { session in
                    HStack(spacing: 8) {
                        Circle()
                            .fill(widgetRuntimeColor(session))
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
                                    .foregroundStyle(widgetRuntimeColor(session).opacity(0.85))
                            }
                        }
                        Spacer()
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(width: 364, height: 170)
        .background(Color.black.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    private var widgetMetric: WidgetMetric {
        buildWidgetMetric(sessions: sessions, totalActive: sessions.count)
    }
}

private struct WidgetMetric {
    let count: Int
    let label: String
    let shortLabel: String
    let color: Color
}

private func buildWidgetMetric(sessions: [SessionSummary], totalActive: Int) -> WidgetMetric {
    let attentionCount = sessions.filter(\.needsAttention).count
    if attentionCount > 0 {
        return WidgetMetric(
            count: attentionCount,
            label: attentionCount == 1 ? "needs permission" : "need permission",
            shortLabel: "permission",
            color: .orange
        )
    }
    return WidgetMetric(
        count: totalActive,
        label: totalActive == 1 ? "active session" : "active sessions",
        shortLabel: "active",
        color: .blue
    )
}

private func widgetRuntimeColor(_ session: SessionSummary) -> Color {
    if session.isBlocked { return .orange }
    if session.presenceState == "running" { return .green }
    if session.presenceState == "thinking" { return .orange }
    if session.presenceState == "syncing_transcript" { return .orange }
    if session.isExecuting { return .orange }
    if session.isIdle || session.status == "completed" { return .secondary }
    return .blue
}

let outputDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp"

let sessions = [
    SessionSummary(id: "1", title: "Debugging Codex Launch Path Bug", presenceState: "thinking", status: "working", provider: "codex", project: "zerg"),
    SessionSummary(id: "2", title: "Simple Arithmetic Calculation", presenceState: "idle", status: "completed", provider: "gemini", project: "gemini"),
]

@MainActor
func renderWidget() {
    let smallView = WidgetSmallView(sessions: sessions)
        .environment(\.colorScheme, .dark)
    let smallRenderer = ImageRenderer(content: smallView)
    smallRenderer.scale = 3.0
    if let image = smallRenderer.nsImage {
        let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let png = rep.representation(using: .png, properties: [:])!
        let path = "\(outputDir)/widget-small.png"
        try! png.write(to: URL(fileURLWithPath: path))
        print("Wrote \(path) (\(png.count) bytes)")
    }

    let mediumView = WidgetMediumView(sessions: sessions)
        .environment(\.colorScheme, .dark)
    let mediumRenderer = ImageRenderer(content: mediumView)
    mediumRenderer.scale = 3.0
    if let image = mediumRenderer.nsImage {
        let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let png = rep.representation(using: .png, properties: [:])!
        let path = "\(outputDir)/widget-medium.png"
        try! png.write(to: URL(fileURLWithPath: path))
        print("Wrote \(path) (\(png.count) bytes)")
    }
}

renderWidget()
