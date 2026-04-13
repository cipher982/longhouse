import SwiftUI

public struct HarnessRootView: View {
    @ObservedObject private var store: SnapshotStore
    private let actionSink: any HealthActionSink
    private let refreshIntervalSeconds: TimeInterval?
    private let managePresentationUpdates: Bool

    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?,
        managePresentationUpdates: Bool = true
    ) {
        self.store = store
        self.actionSink = actionSink
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.managePresentationUpdates = managePresentationUpdates
    }

    public var body: some View {
        Group {
            if let snapshot = store.snapshot {
                MenuBarPanelView(
                    snapshot: snapshot,
                    history: store.history,
                    presentationDate: store.presentationDate,
                    actionSink: actionSink,
                    isManualRefreshing: store.isManualRefreshActive
                ) {
                    store.refresh(reason: .manual)
                }
            } else if store.isInitialLoading {
                MenuBarLoadingView()
            } else {
                MenuBarFailureView(message: store.loadError ?? "Unknown load failure") {
                    store.refresh(reason: .manual)
                }
            }
        }
        .onAppear {
            guard managePresentationUpdates else {
                return
            }
            store.beginPresentationUpdates()
        }
        .onDisappear {
            guard managePresentationUpdates else {
                return
            }
            store.endPresentationUpdates()
        }
        .task {
            guard let refreshIntervalSeconds else {
                return
            }
            while true {
                try? await Task.sleep(for: .seconds(refreshIntervalSeconds))
                store.refresh(reason: .background)
            }
        }
    }
}
