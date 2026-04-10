import AppKit
import LonghouseMenuBarCore
import SwiftUI

@MainActor
private enum HarnessMenuBarLaunchState {
    static var showStatusWindowOnLaunch = false
    static var statusWindowController: StatusWindowController?
}

@MainActor
private final class HarnessMenuBarAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        if HarnessMenuBarLaunchState.showStatusWindowOnLaunch {
            HarnessMenuBarLaunchState.statusWindowController?.showWindow()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        HarnessMenuBarLaunchState.statusWindowController?.showWindow()
        return true
    }
}

@main
@MainActor
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
        HarnessMenuBarLaunchState.statusWindowController = StatusWindowController(
            store: snapshotStore,
            actionSink: actionSink,
            refreshIntervalSeconds: parsed.refreshIntervalSeconds
        )
        HarnessMenuBarLaunchState.showStatusWindowOnLaunch = parsed.showStatusWindowOnLaunch
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
