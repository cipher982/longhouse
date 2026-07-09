import AppKit
import Foundation

public enum HarnessAction: String, Codable {
    case refresh
    case runDoctor
    case repairInstall
    case stopManagedBridge
    case openLogs
    case openLonghouse
    case copyDiagnostics
    case quitApp
}

public enum HarnessEffectMode: String {
    case live
    case logOnly = "log-only"
}

public enum HealthActionFeedbackStyle: String, Equatable {
    case info
    case success
    case warning
    case failure
}

public struct HealthActionFeedback: Equatable {
    public let action: HarnessAction
    public let style: HealthActionFeedbackStyle
    public let title: String
    public let detail: String

    public init(
        action: HarnessAction,
        style: HealthActionFeedbackStyle,
        title: String,
        detail: String
    ) {
        self.action = action
        self.style = style
        self.title = title
        self.detail = detail
    }
}

public protocol HealthActionSink {
    @discardableResult
    func handle(_ action: HarnessAction, snapshot: HealthSnapshot) -> HealthActionFeedback?

    @discardableResult
    func handleStopManagedBridge(
        sessionID: String,
        provider: String?,
        workspaceLabel: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback?

    @discardableResult
    func handleStopManagedBridges(
        targets: [ManagedStopTarget],
        label: String,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback?

    @discardableResult
    func handleOpenManagedSession(
        sessionID: String,
        title: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback?
}

/// A managed session to stop, with the provider needed to pick the stop
/// transport (engine `codex-bridge stop` vs CLI `opencode-channel stop`).
public struct ManagedStopTarget: Equatable, Sendable {
    public let sessionID: String
    public let provider: String?

    public init(sessionID: String, provider: String?) {
        self.sessionID = sessionID
        self.provider = provider
    }
}

public extension HealthActionSink {
    @discardableResult
    func handleStopManagedBridge(
        sessionID: String,
        provider: String?,
        workspaceLabel: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        nil
    }

    @discardableResult
    func handleStopManagedBridges(
        targets: [ManagedStopTarget],
        label: String,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        nil
    }

    @discardableResult
    func handleOpenManagedSession(
        sessionID: String,
        title: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        nil
    }
}

public struct SpyHealthActionSink: HealthActionSink {
    public static let defaultLonghouseURL = "http://127.0.0.1:8080"

    public let logURL: URL?
    public let uiURL: URL?
    public let effectMode: HarnessEffectMode

    public init(logURL: URL?, uiURL: URL?, effectMode: HarnessEffectMode = .live) {
        self.logURL = logURL
        self.uiURL = uiURL
        self.effectMode = effectMode
    }

    public func handle(_ action: HarnessAction, snapshot: HealthSnapshot) -> HealthActionFeedback? {
        appendActionRecord(action: action.rawValue, snapshot: snapshot, target: nil)

        guard effectMode == .live else {
            return dryRunFeedback(for: action, snapshot: snapshot)
        }

        switch action {
        case .runDoctor:
            if openTerminal(command: "longhouse doctor") {
                return feedback(
                    for: action,
                    style: .success,
                    title: "Doctor opened in Terminal",
                    detail: "Review the checks there. Doctor is read-only."
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Doctor could not open",
                detail: "Longhouse could not open Terminal to run `longhouse doctor`."
            )
        case .repairInstall:
            return startRepair(snapshot: snapshot)
        case .stopManagedBridge:
            return feedback(
                for: action,
                style: .warning,
                title: "No managed bridge selected",
                detail: "Use the stop control on a specific managed session or background bridge."
            )
        case .openLonghouse:
            if let resolvedURL = resolveLonghouseURL(snapshot: snapshot) {
                if NSWorkspace.shared.open(resolvedURL) {
                    return feedback(
                        for: action,
                        style: .success,
                        title: "Opened Longhouse",
                        detail: resolvedURL.absoluteString
                    )
                }
                return feedback(
                    for: action,
                    style: .failure,
                    title: "Longhouse could not open",
                    detail: resolvedURL.absoluteString
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Longhouse URL is missing",
                detail: "Set a stored Longhouse URL before trying to open the dashboard."
            )
        case .openLogs:
            if let directoryURL = resolvedLogDirectory(snapshot: snapshot) {
                if NSWorkspace.shared.open(directoryURL) {
                    return feedback(
                        for: action,
                        style: .success,
                        title: "Opened log folder",
                        detail: directoryURL.path
                    )
                }
                return feedback(
                    for: action,
                    style: .failure,
                    title: "Log folder could not open",
                    detail: directoryURL.path
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "No log path available",
                detail: "Longhouse has not reported an engine or installer log location yet."
            )
        case .copyDiagnostics:
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            if let data = try? JSONEncoder().encode(snapshot),
               let string = String(data: data, encoding: .utf8) {
                pasteboard.setString(string, forType: .string)
                return feedback(
                    for: action,
                    style: .success,
                    title: "Copied diagnostics JSON",
                    detail: "The current Longhouse status snapshot is now on your clipboard."
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Diagnostics copy failed",
                detail: "Longhouse could not encode the current status snapshot."
            )
        case .quitApp:
            Task { @MainActor in
                NSApplication.shared.terminate(nil)
            }
            return nil
        case .refresh:
            return nil
        }
    }

    public func handleStopManagedBridge(
        sessionID: String,
        provider: String?,
        workspaceLabel: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        let label = (workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        appendActionRecord(
            action: HarnessAction.stopManagedBridge.rawValue,
            snapshot: snapshot,
            target: sessionID
        )

        guard effectMode == .live else {
            return feedback(
                for: .stopManagedBridge,
                style: .warning,
                title: "Stop dry run recorded",
                detail: "The harness logged a stop request for \(label.isEmpty ? sessionID : label) without touching the bridge."
            )
        }

        switch startStopManagedBridge(sessionID: sessionID, provider: provider) {
        case .started:
            return feedback(
                for: .stopManagedBridge,
                style: .info,
                title: "Stop requested",
                detail: "Longhouse asked the local bridge for \(label.isEmpty ? sessionID : label) to stop in the background."
            )
        case .unsupported(let unsupportedProvider):
            return feedback(
                for: .stopManagedBridge,
                style: .warning,
                title: "Stop not supported here",
                detail: "Longhouse cannot stop \(unsupportedProvider) sessions from the menu bar yet. Stop it from its terminal."
            )
        case .failed:
            return feedback(
                for: .stopManagedBridge,
                style: .failure,
                title: "Stop could not start",
                detail: "Longhouse could not start `\(stopCommandDescription(sessionID: sessionID, provider: provider))` on this Mac."
            )
        }
    }

    public func handleStopManagedBridges(
        targets: [ManagedStopTarget],
        label: String,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        let deduped = uniqueStopTargets(targets)
        let count = deduped.count
        let targetLabel = label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "managed bridges"
            : label.trimmingCharacters(in: .whitespacesAndNewlines)

        appendActionRecord(
            action: HarnessAction.stopManagedBridge.rawValue,
            snapshot: snapshot,
            target: deduped.map(\.sessionID).joined(separator: ",")
        )

        guard count > 0 else {
            return feedback(
                for: .stopManagedBridge,
                style: .warning,
                title: "Nothing to stop",
                detail: "Longhouse did not find any stoppable \(targetLabel)."
            )
        }

        guard effectMode == .live else {
            return feedback(
                for: .stopManagedBridge,
                style: .warning,
                title: "Bulk stop dry run recorded",
                detail: "The harness logged stop requests for \(count) \(targetLabel) without touching live bridges."
            )
        }

        var failed: [String] = []
        var started = 0
        for target in deduped {
            switch startStopManagedBridge(sessionID: target.sessionID, provider: target.provider) {
            case .started:
                started += 1
            case .unsupported, .failed:
                failed.append(target.sessionID)
            }
        }

        if failed.isEmpty {
            return feedback(
                for: .stopManagedBridge,
                style: .info,
                title: "Stop requested",
                detail: "Longhouse asked \(count) \(targetLabel) to stop in the background."
            )
        }

        return feedback(
            for: .stopManagedBridge,
            style: .failure,
            title: "Some stops could not start",
            detail: "Started \(started) of \(count) stop requests. Failed: \(failed.joined(separator: ", "))."
        )
    }

    public func handleOpenManagedSession(
        sessionID: String,
        title: String?,
        snapshot: HealthSnapshot
    ) -> HealthActionFeedback? {
        let normalizedSessionID = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalizedTitle = (title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        appendActionRecord(
            action: HarnessAction.openLonghouse.rawValue,
            snapshot: snapshot,
            target: normalizedSessionID
        )

        guard !normalizedSessionID.isEmpty else {
            return feedback(
                for: .openLonghouse,
                style: .failure,
                title: "Session URL is missing",
                detail: "Longhouse did not report a session id for this row."
            )
        }

        guard effectMode == .live else {
            return feedback(
                for: .openLonghouse,
                style: .info,
                title: "Open session dry run recorded",
                detail: "The harness logged \(normalizedTitle.isEmpty ? normalizedSessionID : normalizedTitle) without opening the browser."
            )
        }

        if let resolvedURL = resolveLonghouseURL(snapshot: snapshot, sessionID: normalizedSessionID) {
            if NSWorkspace.shared.open(resolvedURL) {
                return feedback(
                    for: .openLonghouse,
                    style: .success,
                    title: "Opened session",
                    detail: resolvedURL.absoluteString
                )
            }
            return feedback(
                for: .openLonghouse,
                style: .failure,
                title: "Session could not open",
                detail: resolvedURL.absoluteString
            )
        }

        return feedback(
            for: .openLonghouse,
            style: .failure,
            title: "Longhouse URL is missing",
            detail: "Set a stored Longhouse URL before trying to open the session."
        )
    }

    func resolveLonghouseURL(snapshot: HealthSnapshot) -> URL? {
        if let uiURL {
            return uiURL
        }
        if let storedURL = snapshot.launchReadiness?.storedURL,
           let parsedURL = URL(string: storedURL) {
            return parsedURL
        }
        return URL(string: Self.defaultLonghouseURL)
    }

    func resolveLonghouseURL(snapshot: HealthSnapshot, sessionID: String) -> URL? {
        guard let baseURL = resolveLonghouseURL(snapshot: snapshot) else {
            return nil
        }
        return baseURL
            .appendingPathComponent("timeline", isDirectory: true)
            .appendingPathComponent(sessionID, isDirectory: false)
    }

    private func append(record: ActionRecord) {
        guard let logURL else {
            return
        }
        let encoder = JSONEncoder()
        guard let data = try? encoder.encode(record) else {
            return
        }
        let line = (String(data: data, encoding: .utf8) ?? "{}") + "\n"

        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: logURL.path) {
            fileManager.createFile(atPath: logURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: logURL) else {
            return
        }
        defer {
            try? handle.close()
        }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: Data(line.utf8))
    }

    private func appendActionRecord(action: String, snapshot: HealthSnapshot, target: String?) {
        let record = ActionRecord(
            action: action,
            target: target,
            headline: snapshot.headline,
            collectedAt: snapshot.collectedAt ?? "",
            loggedAt: ISO8601DateFormatter().string(from: Date())
        )
        append(record: record)
    }

    private func uniqueStopTargets(_ targets: [ManagedStopTarget]) -> [ManagedStopTarget] {
        var seen = Set<String>()
        var result: [ManagedStopTarget] = []
        for target in targets {
            let sessionID = target.sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !sessionID.isEmpty, !seen.contains(sessionID) else {
                continue
            }
            seen.insert(sessionID)
            result.append(ManagedStopTarget(sessionID: sessionID, provider: target.provider))
        }
        return result
    }

    private func startBundledSetup() -> URL? {
        guard let invocation = LonghouseCLI.setupInvocation() else {
            return nil
        }

        return startBackgroundProcess(
            launchPath: invocation.launchPath,
            arguments: invocation.arguments
        )
    }

    private func startRepairInstall(snapshot: HealthSnapshot) -> URL? {
        guard let invocation = LonghouseCLI.repairInstallInvocation(snapshot: snapshot) else {
            return nil
        }
        return startBackgroundProcess(
            launchPath: invocation.launchPath,
            arguments: invocation.arguments
        )
    }

    enum ManagedStopOutcome {
        case started(URL)
        /// We have no stop transport for this provider — do not silently run the
        /// Codex stop command against, e.g., a Claude session.
        case unsupported(provider: String)
        /// We know how to stop it but launching the command failed.
        case failed
    }

    private func startStopManagedBridge(sessionID: String, provider: String?) -> ManagedStopOutcome {
        let normalizedProvider = (provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch normalizedProvider {
        case "opencode":
            // OpenCode stop is a `longhouse` CLI command, not an engine command.
            guard let executable = LonghouseCLI.resolveExecutable() else {
                return .failed
            }
            let url = startBackgroundProcess(
                launchPath: executable.path,
                arguments: ["opencode-channel", "stop", "--session-id", sessionID]
            )
            return url.map(ManagedStopOutcome.started) ?? .failed
        case "cursor":
            // Cursor Helm terminate goes through the engine control socket client.
            guard let executable = LonghouseCLI.resolveEngineExecutable() else {
                return .failed
            }
            let url = startBackgroundProcess(
                launchPath: executable.path,
                arguments: ["cursor-helm", "stop", "--session-id", sessionID]
            )
            return url.map(ManagedStopOutcome.started) ?? .failed
        case "", "codex":
            // Codex and legacy rows with no provider stop via the engine bridge.
            guard let executable = LonghouseCLI.resolveEngineExecutable() else {
                return .failed
            }
            let url = startBackgroundProcess(
                launchPath: executable.path,
                arguments: ["codex-bridge", "stop", "--session-id", sessionID]
            )
            return url.map(ManagedStopOutcome.started) ?? .failed
        default:
            // Claude, Antigravity, etc. have no menu-bar stop transport yet.
            return .unsupported(provider: normalizedProvider)
        }
    }

    /// Test-only accessor for the provider-routed stop command description.
    func stopCommandDescriptionForTesting(sessionID: String, provider: String?) -> String {
        stopCommandDescription(sessionID: sessionID, provider: provider)
    }

    /// Test-only classifier for which stop transport a provider routes to,
    /// without launching anything. "opencode" / "cursor" / "codex" / "unsupported".
    func stopTransportForTesting(provider: String?) -> String {
        switch (provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "opencode":
            return "opencode"
        case "cursor":
            return "cursor"
        case "", "codex":
            return "codex"
        default:
            return "unsupported"
        }
    }

    /// Human-readable description of the stop command for a provider, used in
    /// failure feedback so the message names the command we actually ran.
    private func stopCommandDescription(sessionID: String, provider: String?) -> String {
        let normalizedProvider = (provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch normalizedProvider {
        case "opencode":
            return "longhouse opencode-channel stop --session-id \(sessionID)"
        case "cursor":
            return "longhouse-engine cursor-helm stop --session-id \(sessionID)"
        default:
            return "longhouse-engine codex-bridge stop --session-id \(sessionID)"
        }
    }

    private func startBackgroundProcess(launchPath: String, arguments: [String]) -> URL? {
        let logURL = installerLogURL()
        guard let handle = prepareInstallerLogHandle(at: logURL) else {
            return nil
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        process.environment = LonghouseCLI.environment(prependingExecutablePath: launchPath)
        process.standardOutput = handle
        process.standardError = handle

        do {
            try process.run()
            try? handle.close()
            return logURL
        } catch {
            try? handle.close()
            return nil
        }
    }

    private func resolvedLogDirectory(snapshot: HealthSnapshot) -> URL? {
        if let logPath = snapshot.service?.logPath {
            let trimmed = logPath.replacingOccurrences(of: ".*", with: "")
            return URL(fileURLWithPath: trimmed).deletingLastPathComponent()
        }

        let installerLog = installerLogURL()
        guard FileManager.default.fileExists(atPath: installerLog.path) else {
            return nil
        }
        return installerLog.deletingLastPathComponent()
    }

    private func installerLogURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Longhouse", isDirectory: true)
            .appendingPathComponent("desktop-installer.log", isDirectory: false)
    }

    private func prepareInstallerLogHandle(at url: URL) -> FileHandle? {
        let fileManager = FileManager.default
        let directoryURL = url.deletingLastPathComponent()
        do {
            try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: true)
            if !fileManager.fileExists(atPath: url.path) {
                fileManager.createFile(atPath: url.path, contents: nil)
            }
            let handle = try FileHandle(forWritingTo: url)
            try handle.truncate(atOffset: 0)
            return handle
        } catch {
            return nil
        }
    }

    private func openTerminal(command: String) -> Bool {
        // Open a visible Terminal window so the user can see upgrade progress and errors.
        let escaped = command.replacingOccurrences(of: "\\", with: "\\\\")
                             .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "tell application \"Terminal\" to do script \"\(escaped)\""
        guard let appleScript = NSAppleScript(source: script) else {
            return false
        }
        var error: NSDictionary?
        appleScript.executeAndReturnError(&error)
        return error == nil
    }

    private func startRepair(snapshot: HealthSnapshot) -> HealthActionFeedback {
        if snapshot.isInstallLocationBlocked {
            return feedback(
                for: .repairInstall,
                style: .warning,
                title: "Move the app first",
                detail: "Longhouse.app only runs from /Applications. Quit, move it there, then relaunch."
            )
        }

        if snapshot.isSetupRequired {
            if startBundledSetup() != nil {
                return feedback(
                    for: .repairInstall,
                    style: .info,
                    title: "Setup running",
                    detail: "Longhouse started its built-in setup in the background. Open Logs for progress or errors."
                )
            }
            return feedback(
                for: .repairInstall,
                style: .failure,
                title: "Setup could not start",
                detail: "Longhouse could not start its built-in setup on this Mac."
            )
        }

        if startRepairInstall(snapshot: snapshot) != nil {
            return feedback(
                for: .repairInstall,
                style: .info,
                title: "Repair running",
                detail: "Longhouse is reconciling the runtime, replaying queued shipping, then collecting health. Open Logs for live progress."
            )
        }

        if startBundledSetup() != nil {
            return feedback(
                for: .repairInstall,
                style: .warning,
                title: "Repair fell back to setup",
                detail: "Longhouse could not find the local CLI, so it started its built-in setup in the background. Open Logs for progress or errors."
            )
        }

        return feedback(
            for: .repairInstall,
            style: .failure,
            title: "Repair could not start",
            detail: "Longhouse could not start `longhouse machine repair` or its built-in setup on this Mac."
        )
    }

    private func dryRunFeedback(for action: HarnessAction, snapshot: HealthSnapshot) -> HealthActionFeedback? {
        switch action {
        case .runDoctor:
            return feedback(
                for: action,
                style: .info,
                title: "Doctor dry run recorded",
                detail: "The harness logged `longhouse doctor` without opening Terminal."
            )
        case .repairInstall:
            if snapshot.isInstallLocationBlocked {
                return feedback(
                    for: action,
                    style: .warning,
                    title: "Move dry run recorded",
                    detail: "The harness logged the wrong-location blocker without starting setup or repair."
                )
            }
            return feedback(
                for: action,
                style: snapshot.isSetupRequired ? .info : .warning,
                title: snapshot.isSetupRequired ? "Setup dry run recorded" : "Repair dry run recorded",
                detail: snapshot.isSetupRequired
                    ? "The harness logged the built-in Longhouse setup command without changing your machine."
                    : "The harness logged `longhouse machine repair` without changing your machine."
            )
        case .openLonghouse:
            return feedback(
                for: action,
                style: .info,
                title: "Open Longhouse dry run recorded",
                detail: "The harness logged the dashboard open action without leaving the app."
            )
        case .openLogs:
            return feedback(
                for: action,
                style: .info,
                title: "Open logs dry run recorded",
                detail: "The harness logged the log-folder open action without leaving the app."
            )
        case .copyDiagnostics:
            return feedback(
                for: action,
                style: .info,
                title: "Copy diagnostics dry run recorded",
                detail: "The harness logged the clipboard action without touching the pasteboard."
            )
        case .quitApp:
            return feedback(
                for: action,
                style: .info,
                title: "Quit dry run recorded",
                detail: "The harness logged the app quit action without terminating the process."
            )
        case .refresh:
            return nil
        case .stopManagedBridge:
            return feedback(
                for: action,
                style: .warning,
                title: "Stop dry run recorded",
                detail: "The harness logged a managed bridge stop request without touching the live bridge."
            )
        }
    }

    private func feedback(
        for action: HarnessAction,
        style: HealthActionFeedbackStyle,
        title: String,
        detail: String
    ) -> HealthActionFeedback {
        HealthActionFeedback(action: action, style: style, title: title, detail: detail)
    }
}

private struct ActionRecord: Codable {
    let action: String
    let target: String?
    let headline: String
    let collectedAt: String
    let loggedAt: String
}
