import AppKit
import LonghouseMenuBarCore
import SwiftUI

private final class HarnessMenuBarAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
    }
}

@main
struct LonghouseMenuBarHarnessMenuBarApp: App {
    @NSApplicationDelegateAdaptor(HarnessMenuBarAppDelegate.self) private var appDelegate
    @StateObject private var store: SnapshotStore

    private let config: HarnessRuntimeConfig
    private let actionSink: SpyHealthActionSink

    init() {
        let parsed: HarnessRuntimeConfig
        do {
            parsed = try HarnessRuntimeConfig.parse(arguments: Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("LonghouseLocalHealthMenuBar: \(error.localizedDescription)\n", stderr)
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
        MenuBarExtra("Longhouse", systemImage: store.snapshot?.parsedSeverity.symbolName ?? "circle.dotted") {
            HarnessRootView(
                store: store,
                actionSink: actionSink,
                refreshIntervalSeconds: config.refreshIntervalSeconds
            )
        }
        .menuBarExtraStyle(.window)
    }
}
