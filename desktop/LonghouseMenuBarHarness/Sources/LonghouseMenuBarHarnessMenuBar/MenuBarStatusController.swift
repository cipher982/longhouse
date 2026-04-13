import AppKit
import Combine
import LonghouseMenuBarCore
import SwiftUI

@MainActor
final class MenuBarStatusController: NSObject {
    private let store: SnapshotStore
    private let statusItem: NSStatusItem
    private let panelController: MenuBarPanelWindowController
    private var refreshTimer: Timer?
    private var cancellables: Set<AnyCancellable> = []
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private var panelGeneration: UInt64 = 0

    init(
        store: SnapshotStore,
        actionSink: SpyHealthActionSink,
        refreshIntervalSeconds: TimeInterval?
    ) {
        self.store = store
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.panelController = MenuBarPanelWindowController(
            rootView: HarnessRootView(
                store: store,
                actionSink: actionSink,
                refreshIntervalSeconds: nil
            ),
            initialSize: MenuBarStatusController.preferredPanelSize(for: store)
        )

        super.init()

        configureStatusItem()
        configureRefreshTimer(refreshIntervalSeconds)
        observeStore()
    }

    var isPanelPresented: Bool {
        panelController.isPresented
    }

    func performAutomationToggle() {
        statusItem.button?.performClick(nil)
    }

    @objc
    private func togglePanel(_ sender: Any?) {
        guard let button = statusItem.button else {
            return
        }

        if panelController.isPresented {
            closePanel()
            return
        }

        openPanel(relativeTo: button)
    }

    private func configureStatusItem() {
        guard let button = statusItem.button else {
            return
        }

        let icon = MenuBarBrandIcon.image.copy() as? NSImage ?? MenuBarBrandIcon.image
        icon.isTemplate = false
        button.image = icon
        button.imagePosition = .imageOnly
        button.imageScaling = .scaleNone
        button.target = self
        button.action = #selector(togglePanel(_:))
        button.sendAction(on: [.leftMouseUp])
        button.toolTip = "Longhouse"
        button.setAccessibilityLabel("Longhouse")
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
                self?.refreshPanelLayout()
            }
            .store(in: &cancellables)

        store.$isLoading
            .sink { [weak self] _ in
                self?.refreshPanelLayout()
            }
            .store(in: &cancellables)

        store.$loadError
            .sink { [weak self] _ in
                self?.refreshPanelLayout()
            }
            .store(in: &cancellables)
    }

    private func openPanel(relativeTo button: NSStatusBarButton) {
        panelGeneration &+= 1
        panelController.updateContentSize(Self.preferredPanelSize(for: store))
        panelController.show(relativeTo: button)
        installEventMonitors(for: panelGeneration)
    }

    private func closePanel() {
        panelGeneration &+= 1
        panelController.hide()
        removeEventMonitors()
    }

    private func refreshPanelLayout() {
        let size = Self.preferredPanelSize(for: store)
        panelController.updateContentSize(size)
        if panelController.isPresented, let button = statusItem.button {
            panelController.reposition(relativeTo: button)
        }
    }

    private func installEventMonitors(for generation: UInt64) {
        guard localMonitor == nil, globalMonitor == nil else {
            return
        }

        localMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown, .keyDown]
        ) { [weak self] event in
            self?.handleLocalEvent(event, generation: generation) ?? event
        }

        globalMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] _ in
            self?.handleGlobalMouseDown(generation: generation)
        }
    }

    private func removeEventMonitors() {
        if let localMonitor {
            NSEvent.removeMonitor(localMonitor)
            self.localMonitor = nil
        }
        if let globalMonitor {
            NSEvent.removeMonitor(globalMonitor)
            self.globalMonitor = nil
        }
    }

    private func handleLocalEvent(_ event: NSEvent, generation: UInt64) -> NSEvent? {
        guard generation == panelGeneration else {
            return event
        }

        if event.type == .keyDown, event.keyCode == 53 {
            closePanel()
            return nil
        }

        if event.type == .leftMouseDown || event.type == .rightMouseDown || event.type == .otherMouseDown {
            let point = NSEvent.mouseLocation
            if !panelController.containsScreenPoint(point), !statusItemContainsScreenPoint(point) {
                closePanel()
            }
        }

        return event
    }

    private func handleGlobalMouseDown(generation: UInt64) {
        let point = NSEvent.mouseLocation
        if !panelController.containsScreenPoint(point), !statusItemContainsScreenPoint(point) {
            Task { @MainActor in
                guard generation == self.panelGeneration, self.panelController.isPresented else {
                    return
                }
                self.closePanel()
            }
        }
    }

    private func statusItemContainsScreenPoint(_ point: NSPoint) -> Bool {
        guard let button = statusItem.button,
              let window = button.window else {
            return false
        }

        let buttonFrame = button.convert(button.bounds, to: nil)
        let buttonFrameOnScreen = window.convertToScreen(buttonFrame)
        return buttonFrameOnScreen.insetBy(dx: -6, dy: -6).contains(point)
    }

    private static func preferredPanelSize(for store: SnapshotStore) -> NSSize {
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
