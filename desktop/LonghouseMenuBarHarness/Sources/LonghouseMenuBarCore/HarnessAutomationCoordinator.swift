import Foundation

@MainActor
public enum HarnessAutomationCoordinator {
    public static func schedule(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        exerciseActions: [HarnessAction],
        quitAfterSeconds: TimeInterval?,
        quit: @escaping @MainActor () -> Void
    ) {
        guard !exerciseActions.isEmpty || quitAfterSeconds != nil else {
            return
        }

        Task { @MainActor in
            if !exerciseActions.isEmpty {
                let deadline = Date().addingTimeInterval(5)
                while store.snapshot == nil, store.isLoading, Date() < deadline {
                    try? await Task.sleep(for: .milliseconds(50))
                }

                if let initialSnapshot = store.snapshot {
                    for action in exerciseActions {
                        let snapshot = store.snapshot ?? initialSnapshot
                        actionSink.handle(action, snapshot: snapshot)
                        if action == .refresh {
                            store.refresh()
                        }
                    }
                }
            }

            guard let quitAfterSeconds else {
                return
            }
            try? await Task.sleep(for: .seconds(quitAfterSeconds))
            quit()
        }
    }
}
