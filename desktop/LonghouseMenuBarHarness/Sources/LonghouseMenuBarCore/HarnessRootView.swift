import SwiftUI

public struct HarnessRootView: View {
    @ObservedObject private var store: SnapshotStore
    private let actionSink: any HealthActionSink
    private let refreshIntervalSeconds: TimeInterval?
    private let managePresentationUpdates: Bool
    private let headerSummaryVariant: HeaderSummaryVariant

    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?,
        managePresentationUpdates: Bool = true,
        headerSummaryVariant: HeaderSummaryVariant = .default
    ) {
        self.store = store
        self.actionSink = actionSink
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.managePresentationUpdates = managePresentationUpdates
        self.headerSummaryVariant = headerSummaryVariant
    }

    public var body: some View {
        Group {
            if store.isBooting && (store.snapshot?.parsedSeverity ?? .gray) != .green {
                MenuBarBootingView()
            } else if store.isTransientEngineStatusSettling {
                MenuBarSettlingView()
            } else if let staleFailureMessage = store.staleCachedSnapshotFailureMessage(relativeTo: store.presentationDate) {
                MenuBarFailureView(message: staleFailureMessage) {
                    store.refresh(reason: .manual)
                }
            } else if let snapshot = store.snapshot {
                MenuBarPanelView(
                    snapshot: snapshot,
                    history: store.history,
                    presentationDate: store.presentationDate,
                    feedback: store.feedback,
                    setFeedback: store.setFeedback,
                    actionSink: actionSink,
                    isManualRefreshing: store.isManualRefreshActive,
                    headerSummaryVariant: headerSummaryVariant
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
