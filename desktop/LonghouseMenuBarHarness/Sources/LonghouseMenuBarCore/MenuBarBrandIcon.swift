import AppKit

public enum MenuBarBrandIcon {
    public static let image: NSImage = {
        guard let url = Bundle.module.url(forResource: "LonghouseMenuIcon", withExtension: "png"),
              let image = NSImage(contentsOf: url) else {
            let fallback = NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Longhouse") ?? NSImage()
            return fallback
        }

        image.size = NSSize(width: 18, height: 18)
        return image
    }()
}
