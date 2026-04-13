import SwiftUI

public struct HarnessRootView: View {
    @ObservedObject private var store: SnapshotStore
    private let actionSink: any HealthActionSink
    private let refreshIntervalSeconds: TimeInterval?

    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?
    ) {
        self.store = store
        self.actionSink = actionSink
        self.refreshIntervalSeconds = refreshIntervalSeconds
    }

    public var body: some View {
        Group {
            if let snapshot = store.snapshot {
                MenuBarPanelView(
                    snapshot: snapshot,
                    history: store.history,
                    actionSink: actionSink,
                    isRefreshing: store.isLoading
                ) {
                    store.refresh()
                }
            } else if store.isLoading {
                MenuBarLoadingView()
            } else {
                MenuBarFailureView(message: store.loadError ?? "Unknown load failure") {
                    store.refresh()
                }
            }
        }
        .task {
            guard let refreshIntervalSeconds else {
                return
            }
            while true {
                try? await Task.sleep(for: .seconds(refreshIntervalSeconds))
                store.refresh()
            }
        }
    }
}
