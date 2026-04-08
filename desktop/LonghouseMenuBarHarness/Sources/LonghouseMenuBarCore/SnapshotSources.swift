import Foundation

public protocol HealthSnapshotSource {
    func load() throws -> HealthSnapshot
}

public enum SnapshotSourceError: Error, LocalizedError {
    case invalidArguments(String)
    case commandFailed(String)

    public var errorDescription: String? {
        switch self {
        case let .invalidArguments(message):
            return message
        case let .commandFailed(message):
            return message
        }
    }
}

public struct FixtureHealthSnapshotSource: HealthSnapshotSource {
    public let fileURL: URL

    public init(fileURL: URL) {
        self.fileURL = fileURL
    }

    public func load() throws -> HealthSnapshot {
        try HealthSnapshotDecoder.decode(data: Data(contentsOf: fileURL))
    }
}

public struct CLIHealthSnapshotSource: HealthSnapshotSource {
    public let launchPath: String
    public let arguments: [String]

    public init(launchPath: String = "/bin/zsh", arguments: [String] = ["-lc", "longhouse local-health --json"]) {
        self.launchPath = launchPath
        self.arguments = arguments
    }

    public func load() throws -> HealthSnapshot {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        try process.run()
        process.waitUntilExit()

        let output = stdout.fileHandleForReading.readDataToEndOfFile()
        let errorOutput = stderr.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0 else {
            let message = String(data: errorOutput, encoding: .utf8) ?? "longhouse local-health failed"
            throw SnapshotSourceError.commandFailed(message)
        }
        return try HealthSnapshotDecoder.decode(data: output)
    }
}

public enum HealthSnapshotDecoder {
    public static func decode(data: Data) throws -> HealthSnapshot {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(HealthSnapshot.self, from: data)
    }
}

public struct HarnessRuntimeConfig {
    public let outputURL: URL?
    public let source: any HealthSnapshotSource
    public let actionLogURL: URL?
    public let uiURL: URL?
    public let effectMode: HarnessEffectMode
    public let exerciseActions: [HarnessAction]
    public let quitAfterSeconds: TimeInterval?
    public let refreshIntervalSeconds: TimeInterval?
    public let healthCommand: String?

    public init(
        outputURL: URL?,
        source: any HealthSnapshotSource,
        actionLogURL: URL?,
        uiURL: URL?,
        effectMode: HarnessEffectMode,
        exerciseActions: [HarnessAction],
        quitAfterSeconds: TimeInterval?,
        refreshIntervalSeconds: TimeInterval?,
        healthCommand: String?
    ) {
        self.outputURL = outputURL
        self.source = source
        self.actionLogURL = actionLogURL
        self.uiURL = uiURL
        self.effectMode = effectMode
        self.exerciseActions = exerciseActions
        self.quitAfterSeconds = quitAfterSeconds
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.healthCommand = healthCommand
    }

    public static func parse(arguments: [String]) throws -> HarnessRuntimeConfig {
        var outputURL: URL?
        var inputURL: URL?
        var useLive = false
        var actionLogURL: URL?
        var uiURL: URL?
        var effectMode: HarnessEffectMode = .live
        var exerciseActions: [HarnessAction] = []
        var quitAfterSeconds: TimeInterval?
        var refreshIntervalSeconds: TimeInterval?
        var healthCommand: String?

        var index = 0
        while index < arguments.count {
            let arg = arguments[index]
            switch arg {
            case "--input":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected file path after --input")
                }
                inputURL = URL(fileURLWithPath: arguments[index])
            case "--output":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected file path after --output")
                }
                outputURL = URL(fileURLWithPath: arguments[index])
            case "--action-log":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected file path after --action-log")
                }
                actionLogURL = URL(fileURLWithPath: arguments[index])
            case "--ui-url":
                index += 1
                guard index < arguments.count, let parsed = URL(string: arguments[index]) else {
                    throw SnapshotSourceError.invalidArguments("Expected URL after --ui-url")
                }
                uiURL = parsed
            case "--effect-mode":
                index += 1
                guard index < arguments.count, let parsed = HarnessEffectMode(rawValue: arguments[index]) else {
                    throw SnapshotSourceError.invalidArguments("Expected --effect-mode live|log-only")
                }
                effectMode = parsed
            case "--exercise-actions":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected comma-separated actions after --exercise-actions")
                }
                let tokens = arguments[index]
                    .split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { !$0.isEmpty }
                var parsedActions: [HarnessAction] = []
                for token in tokens {
                    guard let action = HarnessAction(rawValue: token) else {
                        throw SnapshotSourceError.invalidArguments("Unknown harness action: \(token)")
                    }
                    parsedActions.append(action)
                }
                exerciseActions = parsedActions
            case "--quit-after":
                index += 1
                guard index < arguments.count, let parsed = TimeInterval(arguments[index]) else {
                    throw SnapshotSourceError.invalidArguments("Expected numeric seconds after --quit-after")
                }
                quitAfterSeconds = parsed
            case "--live":
                useLive = true
            case "--refresh-seconds":
                index += 1
                guard index < arguments.count, let parsed = TimeInterval(arguments[index]) else {
                    throw SnapshotSourceError.invalidArguments("Expected numeric seconds after --refresh-seconds")
                }
                refreshIntervalSeconds = parsed
            case "--health-command":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected shell command after --health-command")
                }
                healthCommand = arguments[index]
            case "-ApplePersistenceIgnoreState":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected value after -ApplePersistenceIgnoreState")
                }
            default:
                throw SnapshotSourceError.invalidArguments("Unknown argument: \(arg)")
            }
            index += 1
        }

        let source: any HealthSnapshotSource
        if let inputURL {
            source = FixtureHealthSnapshotSource(fileURL: inputURL)
        } else if useLive {
            let liveArguments = healthCommand.map { ["-lc", $0] } ?? ["-lc", "longhouse local-health --json"]
            source = CLIHealthSnapshotSource(arguments: liveArguments)
        } else {
            throw SnapshotSourceError.invalidArguments("Pass either --input <file> or --live")
        }

        return HarnessRuntimeConfig(
            outputURL: outputURL,
            source: source,
            actionLogURL: actionLogURL,
            uiURL: uiURL,
            effectMode: effectMode,
            exerciseActions: exerciseActions,
            quitAfterSeconds: quitAfterSeconds,
            refreshIntervalSeconds: refreshIntervalSeconds,
            healthCommand: healthCommand
        )
    }
}
