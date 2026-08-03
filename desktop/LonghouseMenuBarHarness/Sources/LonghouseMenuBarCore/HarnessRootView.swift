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
            // Bounded, not raw `isRecovering`. A producer that times out on
            // every attempt reports transient forever, so the raw flag would
            // pin this on the settling view with nothing else ever shown.
            if store.isBrieflyRecovering && store.snapshot == nil {
                MenuBarSettlingView()
            } else if store.isBooting && (store.snapshot?.parsedSeverity ?? .gray) != .green {
                MenuBarBootingView()
            } else if let snapshot = store.snapshot {
                MenuBarPanelView(
                    snapshot: snapshot,
                    history: store.history,
                    presentationDate: store.presentationDate,
                    feedback: store.feedback,
                    setFeedback: store.setFeedback,
                    actionSink: actionSink,
                    isManualRefreshing: store.isManualRefreshActive || store.isBrieflyRecovering,
                    headerSummaryVariant: headerSummaryVariant,
                    // Recomputed against presentationDate so the banner appears
                    // and its age advances while the panel stays open.
                    dataTrust: store.isBrieflyRecovering
                        ? .current
                        : store.dataTrust(relativeTo: store.presentationDate)
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
