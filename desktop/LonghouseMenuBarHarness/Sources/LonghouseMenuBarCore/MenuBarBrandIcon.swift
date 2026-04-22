import AppKit
import SwiftUI

@MainActor
public enum MenuBarBrandIcon {
    public static func panelImage(severity: HarnessSeverity) -> Image {
        Image(nsImage: baseImage(for: severity))
    }

    public static func statusItemImage(severity: HarnessSeverity) -> NSImage {
        let image = baseImage(for: severity).copy() as? NSImage ?? baseImage(for: severity)
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = false
        return image
    }

    private static func baseImage(for severity: HarnessSeverity) -> NSImage {
        let resourceName = resourceName(for: severity)
        if let nsImage = NSImage(named: resourceName) {
            return nsImage
        }
        if let url = LonghouseResourceLocator.url(forResource: resourceName, withExtension: "png"),
           let nsImage = NSImage(contentsOf: url) {
            return nsImage
        }
        return NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Longhouse") ?? NSImage()
    }

    private static func resourceName(for severity: HarnessSeverity) -> String {
        switch severity {
        case .green:
            return "LonghouseMenuIcon"
        case .yellow:
            return "LonghouseMenuIconYellow"
        case .red:
            return "LonghouseMenuIconRed"
        case .gray:
            return "LonghouseMenuIconGray"
        }
    }
}
