import AppKit
import Combine
import LonghouseMenuBarCore
import SwiftUI

@MainActor
final class MenuBarStatusController: NSObject, NSPopoverDelegate {
    private let store: SnapshotStore
    private let statusItem: NSStatusItem
    private let popover: NSPopover
    private let hostingController: NSHostingController<HarnessRootView>
    private var refreshTimer: Timer?
    private var cancellables: Set<AnyCancellable> = []

    init(
        store: SnapshotStore,
        actionSink: SpyHealthActionSink,
        refreshIntervalSeconds: TimeInterval?
    ) {
        self.store = store
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        self.popover = NSPopover()
        self.hostingController = NSHostingController(
            rootView: HarnessRootView(
                store: store,
                actionSink: actionSink,
                refreshIntervalSeconds: nil
            )
        )

        super.init()

        configureStatusItem()
        configurePopover()
        configureRefreshTimer(refreshIntervalSeconds)
        observeStore()
    }
    @objc
    private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
            return
        }

        guard let button = statusItem.button else {
            return
        }

        updatePopoverSize()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        store.refresh()
    }

    private func configureStatusItem() {
        guard let button = statusItem.button else {
            return
        }

        let icon = MenuBarBrandIcon.image.copy() as? NSImage ?? MenuBarBrandIcon.image
        icon.isTemplate = true
        button.image = icon
        button.imagePosition = .imageOnly
        button.target = self
        button.action = #selector(togglePopover(_:))
        button.sendAction(on: [.leftMouseUp])
        button.toolTip = "Longhouse"
        button.setAccessibilityLabel("Longhouse")
    }

    private func configurePopover() {
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self
        popover.contentViewController = hostingController
        updatePopoverSize()
    }

    private func configureRefreshTimer(_ refreshIntervalSeconds: TimeInterval?) {
        guard let refreshIntervalSeconds else {
            return
        }

        refreshTimer = Timer.scheduledTimer(withTimeInterval: refreshIntervalSeconds, repeats: true) { [weak store] _ in
            Task { @MainActor in
                store?.refresh()
            }
        }
        if let refreshTimer {
            RunLoop.main.add(refreshTimer, forMode: .common)
        }
    }

    private func observeStore() {
        store.$snapshot
            .sink { [weak self] _ in
                self?.updatePopoverSize()
            }
            .store(in: &cancellables)

        store.$isLoading
            .sink { [weak self] _ in
                self?.updatePopoverSize()
            }
            .store(in: &cancellables)

        store.$loadError
            .sink { [weak self] _ in
                self?.updatePopoverSize()
            }
            .store(in: &cancellables)
    }

    private func updatePopoverSize() {
        let size = preferredPopoverSize()
        popover.contentSize = size
        hostingController.view.frame = NSRect(origin: .zero, size: size)
        hostingController.preferredContentSize = size
    }

    private func preferredPopoverSize() -> NSSize {
        let width = MenuBarPanelLayout.panelWidth
        if let snapshot = store.snapshot {
            return NSSize(width: width, height: MenuBarPanelLayout.preferredHeight(for: snapshot))
        }
        if store.isLoading {
            return NSSize(width: width, height: MenuBarPanelLayout.loadingHeight)
        }
        return NSSize(width: width, height: MenuBarPanelLayout.failureHeight)
    }
}
