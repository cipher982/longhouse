import SwiftUI

/// Whether one host's panel is actually on screen. Hosts that order their
/// window in and out (the menu bar panel, the status window) own one and set
/// it; a SwiftUI window host passes none and the root view follows its own
/// appear/disappear. Store-wide presentation counts cannot answer this: a
/// hidden window's hosting view also "appears" when AppKit sizes it.
@MainActor
public final class PanelVisibility: ObservableObject {
    @Published public var isOnScreen: Bool

    public init(isOnScreen: Bool = false) {
        self.isOnScreen = isOnScreen
    }

    /// Placeholder for self-managed hosts; never published to.
    fileprivate static let selfManaged = PanelVisibility()
}

public struct HarnessRootView: View {
    @ObservedObject private var store: SnapshotStore
    @ObservedObject private var visibility: PanelVisibility
    @State private var appeared = false
    /// Held, not observed: only the flames observe its ticks. Observing it
    /// here would re-evaluate the whole panel every frame.
    @State private var hearthClock = HearthClock()
    private let actionSink: any HealthActionSink
    private let refreshIntervalSeconds: TimeInterval?
    private let managePresentationUpdates: Bool

    /// `visibility` is required when the host manages presentation itself
    /// (`managePresentationUpdates: false`).
    public init(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        refreshIntervalSeconds: TimeInterval?,
        managePresentationUpdates: Bool = true,
        visibility: PanelVisibility? = nil
    ) {
        self.store = store
        self.actionSink = actionSink
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.managePresentationUpdates = managePresentationUpdates
        self.visibility = visibility ?? .selfManaged
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
                let projectionTrust = store.projectionTrustForPresentation(relativeTo: store.presentationDate)
                let displayedSnapshot = projectionTrust.isCurrent
                    ? snapshot
                    : snapshot.markingRuntimeHostProjectionUnavailable()
                MenuBarPanelView(
                    snapshot: displayedSnapshot,
                    history: store.history,
                    presentationDate: store.snapshotPresentationDate,
                    feedback: store.feedback,
                    setFeedback: store.setFeedback,
                    actionSink: actionSink,
                    isManualRefreshing: store.isManualRefreshActive || store.isBrieflyRecovering,
                    // Recomputed against presentationDate so the banner appears
                    // and its age advances while the panel stays open.
                    dataTrust: store.dataTrust(relativeTo: store.presentationDate),
                    projectionTrust: projectionTrust
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
        // Flames animate only while this host is on screen.
        .environment(\.hearthClock, isOnScreen ? hearthClock : nil)
        .onChange(of: isOnScreen, initial: true) { _, onScreen in
            hearthClock.isRunning = onScreen
        }
        .onAppear {
            guard managePresentationUpdates else {
                return
            }
            store.beginPresentationUpdates()
            appeared = true
        }
        .onDisappear {
            guard managePresentationUpdates else {
                return
            }
            store.endPresentationUpdates()
            appeared = false
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

    private var isOnScreen: Bool {
        managePresentationUpdates ? appeared : visibility.isOnScreen
    }
}
