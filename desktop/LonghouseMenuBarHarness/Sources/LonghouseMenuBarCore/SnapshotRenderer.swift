import AppKit
import SwiftUI

@MainActor
public enum SnapshotRenderer {
    public static func renderPNG(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        outputURL: URL,
        presentationDate: Date = Date(),
        headerSummaryVariant: HeaderSummaryVariant = .default
    ) throws {
        let rootView = MenuBarPanelView(
            snapshot: snapshot,
            history: [],
            presentationDate: presentationDate,
            feedback: nil,
            setFeedback: { _ in },
            actionSink: actionSink,
            isManualRefreshing: false,
            headerSummaryVariant: headerSummaryVariant,
            refresh: {}
        )
        .environment(\.colorScheme, .dark)
        .background(Color.black)

        let hostingView = NSHostingView(rootView: rootView)
        let renderSize = MenuBarPanelSizing.measuredSize(for: hostingView)
        hostingView.frame = NSRect(origin: .zero, size: renderSize)
        hostingView.layoutSubtreeIfNeeded()

        guard let rep = hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds) else {
            throw SnapshotSourceError.commandFailed("Failed to render snapshot image")
        }
        rep.size = renderSize
        hostingView.cacheDisplay(in: hostingView.bounds, to: rep)
        guard let pngData = rep.representation(using: NSBitmapImageRep.FileType.png, properties: [:]) else {
            throw SnapshotSourceError.commandFailed("Failed to encode PNG snapshot")
        }

        try pngData.write(to: outputURL)
    }
}
