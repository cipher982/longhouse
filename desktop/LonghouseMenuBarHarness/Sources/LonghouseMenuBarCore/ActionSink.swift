import AppKit
import Foundation

public enum HarnessAction: String, Codable {
    case refresh
    case runDoctor
    case repairInstall
    case openLogs
    case openLonghouse
    case copyDiagnostics
}

public enum HarnessEffectMode: String {
    case live
    case logOnly = "log-only"
}

public protocol HealthActionSink {
    func handle(_ action: HarnessAction, snapshot: HealthSnapshot)
}

public struct SpyHealthActionSink: HealthActionSink {
    public let logURL: URL?
    public let uiURL: URL?
    public let effectMode: HarnessEffectMode

    public init(logURL: URL?, uiURL: URL?, effectMode: HarnessEffectMode = .live) {
        self.logURL = logURL
        self.uiURL = uiURL
        self.effectMode = effectMode
    }

    public func handle(_ action: HarnessAction, snapshot: HealthSnapshot) {
        let record = ActionRecord(
            action: action.rawValue,
            headline: snapshot.headline,
            collectedAt: snapshot.collectedAt ?? "",
            loggedAt: ISO8601DateFormatter().string(from: Date())
        )
        append(record: record)

        guard effectMode == .live else {
            return
        }

        switch action {
        case .runDoctor:
            runDetachedShell("longhouse doctor")
        case .repairInstall:
            runDetachedShell("longhouse connect --install")
        case .openLonghouse:
            if let uiURL {
                NSWorkspace.shared.open(uiURL)
            }
        case .openLogs:
            if let logPath = snapshot.service?.logPath {
                let trimmed = logPath.replacingOccurrences(of: ".*", with: "")
                NSWorkspace.shared.open(URL(fileURLWithPath: trimmed).deletingLastPathComponent())
            }
        case .copyDiagnostics:
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            if let data = try? JSONEncoder().encode(snapshot),
               let string = String(data: data, encoding: .utf8) {
                pasteboard.setString(string, forType: .string)
            }
        case .refresh:
            break
        }
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

    private func runDetachedShell(_ command: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", command]
        process.standardOutput = nil
        process.standardError = nil
        try? process.run()
    }
}

private struct ActionRecord: Codable {
    let action: String
    let headline: String
    let collectedAt: String
    let loggedAt: String
}
