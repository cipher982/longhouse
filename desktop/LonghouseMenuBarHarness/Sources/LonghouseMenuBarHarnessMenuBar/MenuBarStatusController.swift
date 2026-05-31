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
        refreshIntervalSeconds: TimeInterval?,
        headerSummaryVariant: HeaderSummaryVariant = .default
    ) {
        self.store = store
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.panelController = MenuBarPanelWindowController(
            rootView: HarnessRootView(
                store: store,
                actionSink: actionSink,
                refreshIntervalSeconds: nil,
                managePresentationUpdates: false,
                headerSummaryVariant: headerSummaryVariant
            )
        )

        super.init()

        configureStatusItem()
        configureRefreshTimer(refreshIntervalSeconds)
        observeStore()
        DispatchQueue.main.async { [weak self] in
            self?.refreshPanelLayout()
        }
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

        button.imagePosition = .imageOnly
        button.imageScaling = .scaleNone
        button.target = self
        button.action = #selector(togglePanel(_:))
        button.sendAction(on: [.leftMouseUp])
        refreshStatusItemAppearance()
    }

    private func configureRefreshTimer(_ refreshIntervalSeconds: TimeInterval?) {
        guard let refreshIntervalSeconds else {
            return
        }

        let timer = Timer(timeInterval: refreshIntervalSeconds, repeats: true) { [weak store] _ in
            Task { @MainActor in
                store?.refresh(reason: .background)
            }
        }
        timer.tolerance = max(1.0, refreshIntervalSeconds * 0.2)
        refreshTimer = timer

        RunLoop.main.add(timer, forMode: .common)
    }

    private func observeStore() {
        Publishers.Merge3(
            store.$snapshot.map { _ in () },
            store.$isInitialLoading.map { _ in () },
            store.$loadError.map { _ in () }
        )
            .sink { [weak self] _ in
                DispatchQueue.main.async {
                    self?.refreshStatusItemAppearance()
                    self?.refreshPanelLayout()
                }
            }
            .store(in: &cancellables)
    }

    private func openPanel(relativeTo button: NSStatusBarButton) {
        panelGeneration &+= 1
        store.beginPresentationUpdates()
        store.refresh(reason: .background)
        panelController.show(relativeTo: button)
        installEventMonitors(for: panelGeneration)
    }

    private func closePanel() {
        panelGeneration &+= 1
        store.endPresentationUpdates()
        store.clearFeedback()
        panelController.hide()
        removeEventMonitors()
    }

    private func refreshPanelLayout() {
        panelController.updateContentSizeToFit()
        if panelController.isPresented, let button = statusItem.button {
            panelController.reposition(relativeTo: button)
        }
    }

    private func refreshStatusItemAppearance() {
        guard let button = statusItem.button else {
            return
        }

        button.image = MenuBarBrandIcon.image(attentionColor: statusItemAttentionColor())
        let tooltip = statusItemAttentionLabel() ?? store.snapshot?.statusItemSummaryLabel ?? "Longhouse"
        button.toolTip = tooltip
        button.setAccessibilityLabel(tooltip)
    }

    private func statusItemAttentionColor() -> NSColor? {
        if store.staleCachedSnapshotFailureMessage(relativeTo: Date()) != nil {
            return .systemRed
        }
        if store.loadError != nil && store.snapshot == nil {
            return .systemRed
        }
        guard let snapshot = store.snapshot else {
            return nil
        }
        if snapshot.isInstallLocationBlocked || snapshot.isSetupRequired {
            return .systemRed
        }
        if let managedSeverity = snapshot.managedAttentionSeverity {
            switch managedSeverity {
            case .yellow:
                return .systemOrange
            case .red:
                return .systemRed
            case .green, .gray:
                break
            }
        }
        switch snapshot.menuBarAttentionSeverity {
        case .yellow?:
            return .systemOrange
        case .red?:
            return .systemRed
        case .green?, .gray?, nil:
            return nil
        }
    }

    private func statusItemAttentionLabel() -> String? {
        if store.staleCachedSnapshotFailureMessage(relativeTo: Date()) != nil {
            return "Longhouse status is stale"
        }
        if store.loadError != nil && store.snapshot == nil {
            return "Longhouse needs attention"
        }
        guard let snapshot = store.snapshot, snapshot.needsMenuBarAttention else {
            return nil
        }
        return snapshot.statusItemSummaryLabel
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
}
