import SwiftUI
import AppKit

// Mirrors ios/Sources/LonghouseWidget/SessionsWidgetView.swift
// Keep in sync if widget layout changes.

struct SessionSummary: Identifiable {
    let id: String
    let title: String
    let presenceState: String
    let provider: String?
    let project: String?
    var isBlocked: Bool { presenceState == "blocked" }
    var attentionLabel: String { isBlocked ? "Needs permission" : "Waiting on you" }
}

struct WidgetSmallView: View {
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
            Text("2")
                .font(.system(size: 36, weight: .bold))
                .foregroundStyle(.orange)
            Text("sessions waiting")
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
                Text("2 waiting")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.orange)
            }
            .padding(.bottom, 8)

            VStack(spacing: 6) {
                ForEach(sessions) { session in
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
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(width: 364, height: 170)
        .background(Color.black.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

let outputDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp"

let sessions = [
    SessionSummary(id: "1", title: "Fixing auth flow in login", presenceState: "needs_user", provider: "claude", project: "longhouse"),
    SessionSummary(id: "2", title: "Deploy pipeline stuck", presenceState: "blocked", provider: "claude", project: "zerg"),
]

@MainActor
func renderWidget() {
    let smallView = WidgetSmallView()
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
