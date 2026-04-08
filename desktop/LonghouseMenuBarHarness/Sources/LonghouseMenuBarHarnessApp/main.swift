import AppKit
import LonghouseMenuBarCore
import SwiftUI

@main
struct LonghouseMenuBarHarnessApp: App {
    @StateObject private var store: SnapshotStore

    private let config: HarnessRuntimeConfig
    private let actionSink: SpyHealthActionSink

    init() {
        let parsed: HarnessRuntimeConfig
        do {
            parsed = try HarnessRuntimeConfig.parse(arguments: Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("LonghouseMenuBarHarnessApp: \(error.localizedDescription)\n", stderr)
            exit(2)
        }

        self.config = parsed
        self.actionSink = SpyHealthActionSink(logURL: parsed.actionLogURL, uiURL: parsed.uiURL, effectMode: parsed.effectMode)
        let snapshotStore = SnapshotStore(source: parsed.source)
        _store = StateObject(wrappedValue: snapshotStore)
        HarnessAutomationCoordinator.schedule(
            store: snapshotStore,
            actionSink: actionSink,
            exerciseActions: parsed.exerciseActions,
            quitAfterSeconds: parsed.quitAfterSeconds,
            quit: { NSApplication.shared.terminate(nil) }
        )
    }

    var body: some Scene {
        WindowGroup("Longhouse Menu Bar Harness") {
            HarnessRootView(
                store: store,
                actionSink: actionSink,
                refreshIntervalSeconds: config.refreshIntervalSeconds
            )
        }
        .windowResizability(.contentSize)
        .defaultSize(width: 460, height: 640)
    }
}
