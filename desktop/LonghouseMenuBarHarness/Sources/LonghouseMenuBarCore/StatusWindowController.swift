import AppKit
import SwiftUI

public final class StatusWindowController: NSWindowController {
    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?
    ) {
        let rootView = HarnessRootView(
            store: store,
            actionSink: actionSink,
            refreshIntervalSeconds: refreshIntervalSeconds
        )
        let hostingController = NSHostingController(rootView: rootView)
        let window = NSWindow(
            contentRect: NSRect(
                x: 0,
                y: 0,
                width: MenuBarPanelLayout.panelWidth,
                height: MenuBarPanelLayout.attentionHeight
            ),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Longhouse"
        window.center()
        window.isReleasedWhenClosed = false
        window.setContentSize(
            NSSize(
                width: MenuBarPanelLayout.panelWidth,
                height: MenuBarPanelLayout.attentionHeight
            )
        )
        window.contentViewController = hostingController

        super.init(window: window)
    }

    @available(*, unavailable)
    public required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    public func showWindow() {
        guard let window else {
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
}
