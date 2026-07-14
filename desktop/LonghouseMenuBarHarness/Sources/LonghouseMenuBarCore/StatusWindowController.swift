import AppKit
import Combine
import SwiftUI

public final class StatusWindowController: NSWindowController {
    private let hostingController: NSHostingController<HarnessRootView>
    private var cancellables: Set<AnyCancellable> = []

    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?,
        headerSummaryVariant: HeaderSummaryVariant = .default
    ) {
        let rootView = HarnessRootView(
            store: store,
            actionSink: actionSink,
            refreshIntervalSeconds: refreshIntervalSeconds,
            headerSummaryVariant: headerSummaryVariant
        )
        self.hostingController = NSHostingController(rootView: rootView)
        if #available(macOS 13.0, *) {
            self.hostingController.sizingOptions = [.preferredContentSize, .intrinsicContentSize]
        }
        let window = NSWindow(
            contentRect: NSRect(
                x: 0,
                y: 0,
                width: MenuBarPanelLayout.panelWidth,
                height: MenuBarPanelLayout.defaultWindowHeight
            ),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Longhouse"
        window.center()
        window.isReleasedWhenClosed = false
        window.setContentSize(MenuBarPanelSizing.defaultSize())
        window.contentViewController = self.hostingController

        super.init(window: window)

        observe(store: store)
        DispatchQueue.main.async { [weak self] in
            self?.updateContentSizeToFit()
        }
    }

    @available(*, unavailable)
    public required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    public func showWindow() {
        guard let window else {
            return
        }
        updateContentSizeToFit()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func observe(store: SnapshotStore) {
        Publishers.Merge4(
            store.$snapshot.map { _ in () },
            store.$isInitialLoading.map { _ in () },
            store.$loadError.map { _ in () },
            store.$isRecovering.map { _ in () }
        )
        .sink { [weak self] _ in
            DispatchQueue.main.async {
                self?.updateContentSizeToFit()
            }
        }
        .store(in: &cancellables)
    }

    private func updateContentSizeToFit() {
        guard let window else {
            return
        }

        let size = MenuBarPanelSizing.measuredSize(for: hostingController.view)
        if window.contentRect(forFrameRect: window.frame).size != size {
            window.setContentSize(size)
        }
    }
}
