import AppKit
import SwiftUI

@MainActor
public enum MenuBarBrandIcon {
    public static var brandImage: Image {
        if let nsImage = NSImage(named: "LonghouseMenuIcon") {
            return Image(nsImage: nsImage)
        }
        if let url = LonghouseResourceLocator.url(forResource: "LonghouseMenuIcon", withExtension: "png"),
           let nsImage = NSImage(contentsOf: url) {
            return Image(nsImage: nsImage)
        }
        return Image(systemName: "circle.dotted")
    }

    public static let baseImage: NSImage = {
        guard let url = LonghouseResourceLocator.url(forResource: "LonghouseMenuIcon", withExtension: "png"),
              let image = NSImage(contentsOf: url) else {
            let fallback = NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Longhouse") ?? NSImage()
            return fallback
        }

        image.size = NSSize(width: 18, height: 18)
        return image
    }()

    public static func image(attentionColor: NSColor? = nil) -> NSImage {
        let image = baseImage.copy() as? NSImage ?? baseImage
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
}
