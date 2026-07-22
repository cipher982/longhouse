import AppKit
import LonghouseMenuBarCore
import SwiftUI

@MainActor
private final class MenuBarPanelWindow: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class MenuBarPanelWindowController: NSWindowController {
    private let hostingController: NSHostingController<HarnessRootView>

    init(rootView: HarnessRootView, initialSize: NSSize = MenuBarPanelSizing.defaultSize()) {
        self.hostingController = NSHostingController(rootView: rootView)
        if #available(macOS 13.0, *) {
            hostingController.sizingOptions = [.preferredContentSize, .intrinsicContentSize]
        }

        let window = MenuBarPanelWindow(
            contentRect: NSRect(origin: .zero, size: initialSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        window.title = "Longhouse"
        window.isReleasedWhenClosed = false
        window.backgroundColor = .clear
        window.isOpaque = false
        window.hasShadow = true
        window.level = .statusBar
        window.collectionBehavior = [.transient, .moveToActiveSpace]
        window.hidesOnDeactivate = false
        window.ignoresMouseEvents = false
        window.animationBehavior = .none
        window.contentViewController = hostingController
        window.setContentSize(initialSize)

        super.init(window: window)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    var isPresented: Bool {
        window?.isVisible == true
    }

    @discardableResult
    func updateContentSizeToFit() -> NSSize {
        guard let window else {
            return MenuBarPanelSizing.defaultSize()
        }

        let maximumHeight = min(
            MenuBarPanelLayout.maximumWindowHeight,
            max(
                MenuBarPanelLayout.defaultWindowHeight,
                ((window.screen ?? NSScreen.main)?.visibleFrame.height ?? MenuBarPanelLayout.maximumWindowHeight) - 24
            )
        )
        let size = MenuBarPanelSizing.measuredSize(
            for: hostingController.view,
            maximumHeight: maximumHeight
        )
        if window.contentRect(forFrameRect: window.frame).size != size {
            window.setContentSize(size)
        }
        hostingController.view.frame = NSRect(origin: .zero, size: size)
        return size
    }

    func show(relativeTo button: NSStatusBarButton) {
        updateContentSizeToFit()
        reposition(relativeTo: button)
        window?.orderFrontRegardless()
    }

    func reposition(relativeTo button: NSStatusBarButton) {
        guard let window,
              let buttonWindow = button.window else {
            return
        }

        let size = window.frame.size
        let buttonFrame = button.convert(button.bounds, to: nil)
        let buttonFrameOnScreen = buttonWindow.convertToScreen(buttonFrame)
        let screenFrame = buttonWindow.screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero

        var originX = round(buttonFrameOnScreen.midX - (size.width / 2))
        originX = max(screenFrame.minX + 8, min(originX, screenFrame.maxX - size.width - 8))

        let originY = round(buttonFrameOnScreen.minY - size.height - 6)
        let frame = NSRect(x: originX, y: originY, width: size.width, height: size.height)
        window.setFrame(frame, display: true)
    }

    func hide() {
        window?.orderOut(nil)
    }

    func containsScreenPoint(_ point: NSPoint) -> Bool {
        window?.frame.contains(point) == true
    }
}
