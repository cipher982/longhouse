import AppKit
import SwiftUI

@MainActor
public enum MenuBarBrandIcon {
    public static func panelImage(severity: HarnessSeverity) -> Image {
        Image(nsImage: panelBaseImage(for: severity))
    }

    public static let statusItemBaseImage: NSImage = {
        guard let url = LonghouseResourceLocator.url(forResource: "LonghouseMenuIcon", withExtension: "png"),
              let image = NSImage(contentsOf: url) else {
            return NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Longhouse") ?? NSImage()
        }

        image.size = NSSize(width: 18, height: 18)
        return image
    }()

    public static func image(attentionColor: NSColor? = nil) -> NSImage {
        let image = statusItemBaseImage.copy() as? NSImage ?? statusItemBaseImage
        guard let attentionColor else {
            image.isTemplate = false
            return image
        }

        let rendered = NSImage(size: image.size)
        rendered.lockFocus()
        image.draw(in: NSRect(origin: .zero, size: image.size))

        let badgeSize: CGFloat = 6
        let badgeRect = NSRect(
            x: image.size.width - badgeSize - 1,
            y: image.size.height - badgeSize - 1,
            width: badgeSize,
            height: badgeSize
        )
        let haloRect = badgeRect.insetBy(dx: -1.2, dy: -1.2)
        NSColor(calibratedWhite: 0.08, alpha: 0.92).setFill()
        NSBezierPath(ovalIn: haloRect).fill()
        attentionColor.setFill()
        NSBezierPath(ovalIn: badgeRect).fill()
        rendered.unlockFocus()
        rendered.isTemplate = false
        return rendered
    }

    private static func panelBaseImage(for severity: HarnessSeverity) -> NSImage {
        let resourceName = panelResourceName(for: severity)
        if let nsImage = NSImage(named: resourceName) {
            return nsImage
        }
        if let url = LonghouseResourceLocator.url(forResource: resourceName, withExtension: "png"),
           let nsImage = NSImage(contentsOf: url) {
            return nsImage
        }
        return NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Longhouse") ?? NSImage()
    }

    private static func panelResourceName(for severity: HarnessSeverity) -> String {
        switch severity {
        case .green:
            return "LonghousePanelIconGreen"
        case .yellow:
            return "LonghousePanelIconYellow"
        case .red:
            return "LonghousePanelIconRed"
        case .gray:
            return "LonghousePanelIconGray"
        }
    }
}
