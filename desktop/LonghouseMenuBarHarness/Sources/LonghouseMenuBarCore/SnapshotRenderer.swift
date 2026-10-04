import AppKit
import SwiftUI

@MainActor
public enum SnapshotRenderer {
    public static func renderPNG(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        outputURL: URL,
        presentationDate: Date? = nil,
        appearance: PanelAppearance = .dark
    ) throws {
        // Fixture renders have no SnapshotStore/producer trust context. Anchor
        // their relative labels to the captured snapshot instead of making a
        // fixed historical fixture look like a live, current machine.
        let renderDate = presentationDate ?? snapshot.collectedAtDate ?? Date()
        let rootView = MenuBarPanelView(
            snapshot: snapshot,
            history: [],
            presentationDate: renderDate,
            feedback: nil,
            setFeedback: { _ in },
            actionSink: actionSink,
            isManualRefreshing: false,
            refresh: {}
        )
        .environment(\.colorScheme, appearance.colorScheme)
        .background(appearance == .dark ? Color.black : Color(white: 0.92))

        let hostingView = NSHostingView(rootView: rootView)
        // AppKit-backed controls (glass buttons, menus) read the view's
        // appearance, not SwiftUI's colorScheme; without this they draw for
        // the wrong scheme offscreen.
        hostingView.appearance = NSAppearance(named: appearance == .dark ? .darkAqua : .aqua)
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
