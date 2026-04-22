import AppKit
import SwiftUI

@MainActor
public enum SnapshotRenderer {
    public static func renderPNG(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        outputURL: URL,
        headerSummaryVariant: HeaderSummaryVariant = .default
    ) throws {
        let view = MenuBarPanelView(
            snapshot: snapshot,
            history: [],
            presentationDate: Date(),
            feedback: nil,
            setFeedback: { _ in },
            actionSink: actionSink,
            isManualRefreshing: false,
            headerSummaryVariant: headerSummaryVariant,
            refresh: {}
        )
        .environment(\.colorScheme, .dark)
        .background(Color.black)

        let renderer = ImageRenderer(content: view)
        renderer.scale = 2

        guard let cgImage = renderer.cgImage else {
            throw SnapshotSourceError.commandFailed("Failed to render snapshot image")
        }

        let rep = NSBitmapImageRep(cgImage: cgImage)
        guard let pngData = rep.representation(using: NSBitmapImageRep.FileType.png, properties: [:]) else {
            throw SnapshotSourceError.commandFailed("Failed to encode PNG snapshot")
        }

        try pngData.write(to: outputURL)
    }
}
