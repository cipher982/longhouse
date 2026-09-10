import Foundation
import OSLog

/// Answers "why did the app die?" for the next launch.
///
/// When this app was killed on device there was nothing to read afterwards: a
/// watchdog or jetsam kill is a SIGKILL, `devicectl` has no crash-log verb, and
/// copying `systemCrashLogs` returns EPERM for most of the directory. So the
/// app keeps its own record: a run marker that is rewritten on every lifecycle
/// stage and only marked clean on a real termination. The next launch finds a
/// marker that was never cleaned and ships where the previous run was when it
/// stopped, beside the server-side session log, where it is readable from a
/// terminal instead of a cable.
@MainActor
final class RunBreadcrumb {
    static let shared = RunBreadcrumb()

    struct PreviousRun: Sendable {
        let stage: String
        let stageAt: Date
        let startedAt: Date
        let scene: String
        let sessionId: String?
        let build: String?
        let exception: String?

        /// One telemetry line: enough to tell a foreground death from an
        /// ordinary suspended-app kill without opening anything.
        var summary: String {
            var parts = [
                "last_stage=\(stage)",
                "scene=\(scene)",
                "last_stage_at=\(stageAt.formatted(.iso8601))",
                "ran_for_s=\(Int(stageAt.timeIntervalSince(startedAt)))",
            ]
            if let sessionId, !sessionId.isEmpty {
                parts.append("session=\(sessionId.prefix(8))")
            }
            if let build, !build.isEmpty {
                parts.append("build=\(build)")
            }
            if let exception, !exception.isEmpty {
                parts.append("exception=\(exception)")
            }
            return parts.joined(separator: " ")
        }
    }

    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "RunBreadcrumb")
    private let queue = DispatchQueue(label: "ai.longhouse.ios.run-breadcrumb", qos: .utility)
    private var fileURL: URL?
    private var marker: RunMarker?
    private var previousRun: PreviousRun?
    private var writeScheduled = false
    private var didReport = false

    /// At most this often, so a mark storm costs one small write every half
    /// second instead of one per mark.
    private static let writeCoalescing: TimeInterval = 0.5

    private init() {}

    /// Starts a new run. The previous marker is read off the main actor; only
    /// its summary comes back here. Marks that arrive before this finishes are
    /// not recorded, which is why it runs first in the launch task.
    func begin(scene: String) async {
        guard marker == nil else { return }
        // All of the launch-side I/O — resolving the directory, reading the
        // previous marker, loading the build identity — happens off the main
        // actor. This runs first in the launch task, on the path the user waits
        // on, so none of it belongs there.
        let (url, previous, build) = await Task.detached(priority: .utility) {
            let url = runMarkerURL()
            return (
                url,
                url.flatMap(readRunMarker),
                (try? BuildIdentityLoader.loadFromMainBundle().get())?.qualifiedVersion
            )
        }.value
        fileURL = url
        previousRun = previous.flatMap { previous in
            previous.clean ? nil : PreviousRun(
                stage: previous.stage,
                stageAt: previous.stageAt,
                startedAt: previous.startedAt,
                scene: previous.scene,
                sessionId: previous.sessionId,
                build: previous.build,
                exception: previous.exception
            )
        }
        if let previousRun {
            logger.notice("previous run did not terminate cleanly: \(previousRun.summary, privacy: .public)")
        }
        marker = RunMarker(
            runId: UUID().uuidString,
            startedAt: Date(),
            build: build,
            scene: scene,
            stage: "launch",
            stageAt: Date(),
            sessionId: nil,
            clean: false,
            exception: nil
        )
        // Publish before installing the handler and before the first coalesced
        // write: an exception in that window would otherwise have nothing to
        // record itself into.
        publishCurrentMarker()
        installRunMarkerExceptionHandler(url: url)
        scheduleWrite()
    }

    func note(stage: String, sessionId: String?) {
        guard var marker else { return }
        marker.stage = stage
        marker.stageAt = Date()
        if let sessionId { marker.sessionId = sessionId }
        self.marker = marker
        scheduleWrite()
    }

    /// Scene phase is what separates "died in the foreground" from an ordinary
    /// kill while suspended, which is the first question a breadcrumb has to
    /// answer.
    func updateScene(_ scene: String) {
        guard var marker, marker.scene != scene else { return }
        marker.scene = scene
        self.marker = marker
        scheduleWrite()
    }

    /// The one legitimate "we chose to stop" signal. Everything else — a
    /// watchdog kill, jetsam, a force quit — leaves the marker uncleaned, which
    /// is what makes it evidence. The write is synchronous: the process may not
    /// survive long enough for a queued one, and an asynchronous write here
    /// would report honest exits as deaths.
    func markCleanExit() {
        guard var marker else { return }
        marker.clean = true
        marker.stage = "clean_exit"
        marker.stageAt = Date()
        self.marker = marker
        guard let data = publishCurrentMarker() else { return }
        queue.sync {
            guard let fileURL else { return }
            try? data.write(to: fileURL, options: .atomic)
        }
    }

    /// Ships the previous run's outcome once per launch, through the same
    /// diagnostics channel as every other lifecycle mark. The channel has no
    /// sink until a session client exists, so this is called from the point a
    /// client installs one.
    func reportPreviousRunIfNeeded() {
        guard !didReport, let previousRun else { return }
        didReport = true
        ClientDiagnosticsReporter.shared.record(
            stage: "previous_run_unclean",
            detail: previousRun.summary,
            sessionId: previousRun.sessionId
        )
    }

    @discardableResult
    private func publishCurrentMarker() -> Data? {
        guard let marker, let data = try? JSONEncoder().encode(marker) else { return nil }
        publishRunMarker(data)
        return data
    }

    private func scheduleWrite() {
        guard fileURL != nil, !writeScheduled else { return }
        writeScheduled = true
        queue.asyncAfter(deadline: .now() + Self.writeCoalescing) { [weak self] in
            Task { @MainActor in
                self?.writeScheduled = false
                self?.writeNow()
            }
        }
    }

    private func writeNow() {
        guard let data = publishCurrentMarker(), let fileURL else { return }
        queue.async {
            // A missing breadcrumb must never be the reason a launch fails.
            try? data.write(to: fileURL, options: .atomic)
        }
    }
}

/// A run marker as it is written to disk. File scope, not nested inside the
/// main-actor class, because the fatal path below cannot reach into one.
private struct RunMarker: Codable, Sendable {
    var runId: String
    var startedAt: Date
    var build: String?
    var scene: String
    var stage: String
    var stageAt: Date
    var sessionId: String?
    var clean: Bool
    var exception: String?
}

private func runMarkerURL() -> URL? {
    guard let directory = try? FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: true
    ) else { return nil }
    let bundle = Bundle.main.bundleIdentifier ?? "longhouse"
    let root = directory.appendingPathComponent(bundle, isDirectory: true)
    try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return root.appendingPathComponent("run-marker.json")
}

private func readRunMarker(at url: URL) -> RunMarker? {
    guard let data = try? Data(contentsOf: url) else { return nil }
    return try? JSONDecoder().decode(RunMarker.self, from: data)
}

/// The fatal path: records an uncaught Objective-C/Swift exception into the
/// marker before the process aborts. This runs on whichever thread is throwing
/// with the process still healthy, so Foundation is usable here — it is not a
/// signal handler, and none are installed: writing from one is not
/// async-signal-safe, and the kills that actually happen here are SIGKILL.
///
/// File-scope state and a top-level handler rather than members of a type:
/// `NSSetUncaughtExceptionHandler` needs a C function pointer, and referencing a
/// type's static storage from one is a captured context.
nonisolated(unsafe) private var runMarkerLatest: Data?
nonisolated(unsafe) private let runMarkerLock = NSLock()
nonisolated(unsafe) private var runMarkerURLValue: URL?
nonisolated(unsafe) private var runMarkerHandlerInstalled = false

private func installRunMarkerExceptionHandler(url: URL?) {
    runMarkerURLValue = url
    guard !runMarkerHandlerInstalled else { return }
    runMarkerHandlerInstalled = true
    NSSetUncaughtExceptionHandler(runMarkerRecordException)
}

private func runMarkerRecordException(_ exception: NSException) {
    let described = "\(exception.name.rawValue): \(exception.reason ?? "")"
    runMarkerLock.lock()
    let encoded = runMarkerLatest
    runMarkerLock.unlock()
    guard let encoded,
          var marker = try? JSONDecoder().decode(RunMarker.self, from: encoded)
    else { return }
    marker.exception = described
    marker.stageAt = Date()
    guard let updated = try? JSONEncoder().encode(marker),
          let url = runMarkerURLValue
    else { return }
    try? updated.write(to: url, options: .atomic)
}

private func publishRunMarker(_ data: Data) {
    runMarkerLock.lock()
    runMarkerLatest = data
    runMarkerLock.unlock()
}
