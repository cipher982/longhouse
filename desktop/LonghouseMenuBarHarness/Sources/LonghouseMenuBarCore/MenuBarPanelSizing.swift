import AppKit

@MainActor
public enum MenuBarPanelSizing {
    public static func defaultSize() -> NSSize {
        NSSize(width: MenuBarPanelLayout.panelWidth, height: MenuBarPanelLayout.defaultWindowHeight)
    }

    public static func measuredSize(
        for hostingView: NSView,
        width: CGFloat = MenuBarPanelLayout.panelWidth,
        fallbackHeight: CGFloat = MenuBarPanelLayout.defaultWindowHeight,
        maximumHeight: CGFloat = MenuBarPanelLayout.maximumWindowHeight
    ) -> NSSize {
        hostingView.invalidateIntrinsicContentSize()
        hostingView.frame = NSRect(
            x: 0,
            y: 0,
            width: width,
            height: max(hostingView.frame.height, fallbackHeight)
        )
        hostingView.layoutSubtreeIfNeeded()

        let fittingSize = hostingView.fittingSize
        let measuredHeight = min(max(ceil(fittingSize.height), 1), maximumHeight)

        return NSSize(
            width: width,
            height: measuredHeight > 1 ? measuredHeight : fallbackHeight
        )
    }
}
