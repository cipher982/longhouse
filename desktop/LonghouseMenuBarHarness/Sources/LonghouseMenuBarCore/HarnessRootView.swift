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
                MenuBarPanelView(snapshot: snapshot, actionSink: actionSink) {
                    store.refresh()
                }
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Longhouse harness could not load a snapshot")
                        .font(.headline)
                        .harnessAccessibility(
                            identifier: LonghouseMenuBarAccessibilityID.Error.headline,
                            label: "Longhouse harness could not load a snapshot"
                        )
                    Text(store.loadError ?? "Unknown load failure")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .harnessAccessibility(
                            identifier: LonghouseMenuBarAccessibilityID.Error.message,
                            label: store.loadError ?? "Unknown load failure"
                        )
                    Button("Retry") {
                        store.refresh()
                    }
                    .harnessAccessibilityButton(
                        identifier: LonghouseMenuBarAccessibilityID.Error.retryButton,
                        label: "Retry"
                    )
                }
                .padding(24)
                .frame(width: 420, alignment: .leading)
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

private extension View {
    func harnessAccessibility(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }

    func harnessAccessibilityButton(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }
}
