import Foundation

@MainActor
public enum HarnessAutomationCoordinator {
    public struct ToggleProfileConfiguration {
        public let logURL: URL
        public let count: Int
        public let intervalMilliseconds: Int

        public init(logURL: URL, count: Int, intervalMilliseconds: Int) {
            self.logURL = logURL
            self.count = count
            self.intervalMilliseconds = intervalMilliseconds
        }
    }

    public static func schedule(
        store: SnapshotStore,
        actionSink: any HealthActionSink,
        exerciseActions: [HarnessAction],
        toggleProfile: ToggleProfileConfiguration? = nil,
        triggerToggle: (@MainActor () -> Void)? = nil,
        panelPresented: (@MainActor () -> Bool)? = nil,
        quitAfterSeconds: TimeInterval?,
        quit: @escaping @MainActor () -> Void
    ) {
        guard !exerciseActions.isEmpty || toggleProfile != nil || quitAfterSeconds != nil else {
            return
        }

        Task { @MainActor in
            if !exerciseActions.isEmpty || toggleProfile != nil {
                let deadline = Date().addingTimeInterval(5)
                while store.snapshot == nil, store.isInitialLoading, Date() < deadline {
                    try? await Task.sleep(for: .milliseconds(50))
                }
            }

            if !exerciseActions.isEmpty {
                if let initialSnapshot = store.snapshot {
                    for action in exerciseActions {
                        let snapshot = store.snapshot ?? initialSnapshot
                        actionSink.handle(action, snapshot: snapshot)
                        if action == .refresh {
                            store.refresh(reason: .manual)
                        }
                    }
                }
            }

            if let toggleProfile,
               let triggerToggle,
               let panelPresented {
                await runToggleProfile(
                    config: toggleProfile,
                    triggerToggle: triggerToggle,
                    panelPresented: panelPresented
                )
            }

            guard let quitAfterSeconds else {
                return
            }
            try? await Task.sleep(for: .seconds(quitAfterSeconds))
            quit()
        }
    }

    private static func runToggleProfile(
        config: ToggleProfileConfiguration,
        triggerToggle: @escaping @MainActor () -> Void,
        panelPresented: @escaping @MainActor () -> Bool
    ) async {
        guard config.count > 0 else {
            return
        }

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        var lines: [String] = []

        for index in 0..<config.count {
            let initialState = panelPresented()
            let expectedState = !initialState
            let start = ContinuousClock.now
            triggerToggle()

            var didReachExpectedState = false
            let deadline = Date().addingTimeInterval(1.0)
            while Date() < deadline {
                if panelPresented() == expectedState {
                    didReachExpectedState = true
                    break
                }
                try? await Task.sleep(for: .milliseconds(5))
            }

            let duration = start.duration(to: ContinuousClock.now)
            let durationMilliseconds = Double(duration.components.seconds) * 1000
                + Double(duration.components.attoseconds) / 1_000_000_000_000_000

            let record = ToggleProfileRecord(
                iteration: index + 1,
                opened: expectedState,
                durationMilliseconds: durationMilliseconds,
                timedOut: !didReachExpectedState
            )

            if let data = try? encoder.encode(record),
               let line = String(data: data, encoding: .utf8) {
                lines.append(line)
            }

            if config.intervalMilliseconds > 0 {
                try? await Task.sleep(for: .milliseconds(config.intervalMilliseconds))
            }
        }

        let payload = lines.joined(separator: "\n") + "\n"
        try? payload.write(to: config.logURL, atomically: true, encoding: .utf8)
    }
}

private struct ToggleProfileRecord: Codable {
    let iteration: Int
    let opened: Bool
    let durationMilliseconds: Double
    let timedOut: Bool
}
