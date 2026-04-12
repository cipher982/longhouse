import AppKit
import Foundation

public enum HarnessAction: String, Codable {
    case refresh
    case runDoctor
    case repairInstall
    case openLogs
    case openLonghouse
    case copyDiagnostics
    case upgradeNow
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
        let record = ActionRecord(
            action: action.rawValue,
            headline: snapshot.headline,
            collectedAt: snapshot.collectedAt ?? "",
            loggedAt: ISO8601DateFormatter().string(from: Date())
        )
        append(record: record)

        guard effectMode == .live else {
            return dryRunFeedback(for: action)
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
            if openTerminal(command: "longhouse connect --install") {
                return feedback(
                    for: action,
                    style: .warning,
                    title: "Repair opened in Terminal",
                    detail: "Repair may update the app, service wiring, and automatic imports on this Mac."
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Repair could not open",
                detail: "Longhouse could not open Terminal to run `longhouse connect --install`."
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
            if let logPath = snapshot.service?.logPath {
                let trimmed = logPath.replacingOccurrences(of: ".*", with: "")
                let directoryURL = URL(fileURLWithPath: trimmed).deletingLastPathComponent()
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
                detail: "Longhouse has not reported an engine log location yet."
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
                    detail: "The current local-health snapshot is now on your clipboard."
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Diagnostics copy failed",
                detail: "Longhouse could not encode the current local-health snapshot."
            )
        case .upgradeNow:
            let command = upgradeCommand(for: snapshot)
            if openTerminal(command: command) {
                return feedback(
                    for: action,
                    style: .success,
                    title: "Upgrade opened in Terminal",
                    detail: command
                )
            }
            return feedback(
                for: action,
                style: .failure,
                title: "Upgrade could not open",
                detail: command
            )
        case .refresh:
            return feedback(
                for: action,
                style: .info,
                title: "Refreshing local health",
                detail: "Longhouse is reloading the latest local status snapshot."
            )
        }
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

    private func upgradeCommand(for snapshot: HealthSnapshot) -> String {
        let command = snapshot.updateInfo?.upgradeCommand.trimmingCharacters(in: .whitespacesAndNewlines)
        if let command, !command.isEmpty {
            return command
        }
        return "longhouse upgrade"
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

    private func dryRunFeedback(for action: HarnessAction) -> HealthActionFeedback {
        switch action {
        case .runDoctor:
            return feedback(
                for: action,
                style: .info,
                title: "Doctor dry run recorded",
                detail: "The harness logged `longhouse doctor` without opening Terminal."
            )
        case .repairInstall:
            return feedback(
                for: action,
                style: .warning,
                title: "Repair dry run recorded",
                detail: "The harness logged `longhouse connect --install` without changing your machine."
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
        case .upgradeNow:
            return feedback(
                for: action,
                style: .info,
                title: "Upgrade dry run recorded",
                detail: "The harness logged the upgrade action without opening Terminal."
            )
        case .refresh:
            return feedback(
                for: action,
                style: .info,
                title: "Refreshing local health",
                detail: "Longhouse is reloading the latest local status snapshot."
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
    let headline: String
    let collectedAt: String
    let loggedAt: String
}
