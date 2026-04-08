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
            fputs("LonghouseMenuBarHarnessMenuBar: \(error.localizedDescription)\n", stderr)
            exit(2)
        }

        self.config = parsed
        self.actionSink = SpyHealthActionSink(logURL: parsed.actionLogURL, uiURL: parsed.uiURL)
        _store = StateObject(wrappedValue: SnapshotStore(source: parsed.source))
    }

    var body: some Scene {
        MenuBarExtra("Longhouse", systemImage: store.snapshot?.parsedSeverity.symbolName ?? "circle.dotted") {
            HarnessRootView(store: store, actionSink: actionSink, refreshIntervalSeconds: config.refreshIntervalSeconds)
        }
        .menuBarExtraStyle(.window)
    }
}
