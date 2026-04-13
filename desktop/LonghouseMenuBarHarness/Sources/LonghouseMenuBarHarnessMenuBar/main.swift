import AppKit
import LonghouseMenuBarCore

@MainActor
private enum HarnessMenuBarLaunchState {
    static var appDelegate: HarnessMenuBarAppDelegate?
}

@MainActor
private final class HarnessMenuBarAppDelegate: NSObject, NSApplicationDelegate {
    private let config: HarnessRuntimeConfig
    private let actionSink: SpyHealthActionSink
    private let store: SnapshotStore

    private var menuBarController: MenuBarStatusController?
    private var statusWindowController: StatusWindowController?

    init(
        config: HarnessRuntimeConfig,
        actionSink: SpyHealthActionSink,
        store: SnapshotStore
    ) {
        self.config = config
        self.actionSink = actionSink
        self.store = store
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)

        statusWindowController = StatusWindowController(
            store: store,
            actionSink: actionSink,
            refreshIntervalSeconds: config.refreshIntervalSeconds
        )
        menuBarController = MenuBarStatusController(
            store: store,
            actionSink: actionSink,
            refreshIntervalSeconds: config.refreshIntervalSeconds
        )

        if config.showStatusWindowOnLaunch {
            statusWindowController?.showWindow()
        }

        HarnessAutomationCoordinator.schedule(
            store: store,
            actionSink: actionSink,
            exerciseActions: config.exerciseActions,
            quitAfterSeconds: config.quitAfterSeconds,
            quit: { NSApplication.shared.terminate(nil) }
        )
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        statusWindowController?.showWindow()
        return true
    }
}

let parsed: HarnessRuntimeConfig
do {
    parsed = try HarnessRuntimeConfig.parse(arguments: Array(CommandLine.arguments.dropFirst()))
} catch {
    fputs("LonghouseLocalHealthMenuBar: \(error.localizedDescription)\n", stderr)
    exit(2)
}

let actionSink = SpyHealthActionSink(
    logURL: parsed.actionLogURL,
    uiURL: parsed.uiURL,
    effectMode: parsed.effectMode
)
let snapshotStore = SnapshotStore(source: parsed.source)
private let delegate = HarnessMenuBarAppDelegate(config: parsed, actionSink: actionSink, store: snapshotStore)

HarnessMenuBarLaunchState.appDelegate = delegate

let app = NSApplication.shared
app.delegate = delegate
app.run()
