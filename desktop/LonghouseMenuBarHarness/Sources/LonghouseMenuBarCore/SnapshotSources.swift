import Foundation

public protocol HealthSnapshotSource: Sendable {
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
    public static let defaultRefreshIntervalSeconds: TimeInterval = 10

    public let outputURL: URL?
    public let source: any HealthSnapshotSource
    public let actionLogURL: URL?
    public let uiURL: URL?
    public let effectMode: HarnessEffectMode
    public let healthyConcept: HealthyPanelConcept
    public let exerciseActions: [HarnessAction]
    public let quitAfterSeconds: TimeInterval?
    public let refreshIntervalSeconds: TimeInterval?
    public let toggleProfileLogURL: URL?
    public let toggleProfileCount: Int
    public let toggleProfileIntervalMilliseconds: Int
    public let healthCommand: String?
    public let healthExecutablePath: String?
    public let healthArguments: [String]
    public let showStatusWindowOnLaunch: Bool

    public init(
        outputURL: URL?,
        source: any HealthSnapshotSource,
        actionLogURL: URL?,
        uiURL: URL?,
        effectMode: HarnessEffectMode,
        healthyConcept: HealthyPanelConcept,
        exerciseActions: [HarnessAction],
        quitAfterSeconds: TimeInterval?,
        refreshIntervalSeconds: TimeInterval?,
        toggleProfileLogURL: URL?,
        toggleProfileCount: Int,
        toggleProfileIntervalMilliseconds: Int,
        healthCommand: String?,
        healthExecutablePath: String?,
        healthArguments: [String],
        showStatusWindowOnLaunch: Bool
    ) {
        self.outputURL = outputURL
        self.source = source
        self.actionLogURL = actionLogURL
        self.uiURL = uiURL
        self.effectMode = effectMode
        self.healthyConcept = healthyConcept
        self.exerciseActions = exerciseActions
        self.quitAfterSeconds = quitAfterSeconds
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.toggleProfileLogURL = toggleProfileLogURL
        self.toggleProfileCount = toggleProfileCount
        self.toggleProfileIntervalMilliseconds = toggleProfileIntervalMilliseconds
        self.healthCommand = healthCommand
        self.healthExecutablePath = healthExecutablePath
        self.healthArguments = healthArguments
        self.showStatusWindowOnLaunch = showStatusWindowOnLaunch
    }

    public static func parse(arguments: [String]) throws -> HarnessRuntimeConfig {
        var outputURL: URL?
        var inputURL: URL?
        var useLive = false
        var actionLogURL: URL?
        var uiURL: URL?
        var effectMode: HarnessEffectMode = .live
        var healthyConcept: HealthyPanelConcept = .production
        var exerciseActions: [HarnessAction] = []
        var quitAfterSeconds: TimeInterval?
        var refreshIntervalSeconds: TimeInterval?
        var toggleProfileLogURL: URL?
        var toggleProfileCount = 0
        var toggleProfileIntervalMilliseconds = 120
        var healthCommand: String?
        var healthExecutablePath: String?
        var healthArguments: [String] = []
        var explicitLiveMode = false

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
            case "--healthy-concept":
                index += 1
                guard index < arguments.count, let parsed = HealthyPanelConcept(rawValue: arguments[index]) else {
                    throw SnapshotSourceError.invalidArguments("Expected --healthy-concept production|launch-horizon|repo-deck|mission-timeline")
                }
                healthyConcept = parsed
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
            case "--toggle-profile-log":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected file path after --toggle-profile-log")
                }
                toggleProfileLogURL = URL(fileURLWithPath: arguments[index])
            case "--toggle-profile-count":
                index += 1
                guard index < arguments.count, let parsed = Int(arguments[index]), parsed >= 0 else {
                    throw SnapshotSourceError.invalidArguments("Expected non-negative integer after --toggle-profile-count")
                }
                toggleProfileCount = parsed
            case "--toggle-profile-interval-ms":
                index += 1
                guard index < arguments.count, let parsed = Int(arguments[index]), parsed >= 0 else {
                    throw SnapshotSourceError.invalidArguments("Expected non-negative integer after --toggle-profile-interval-ms")
                }
                toggleProfileIntervalMilliseconds = parsed
            case "--live":
                useLive = true
                explicitLiveMode = true
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
            case "--health-exec":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected executable path after --health-exec")
                }
                healthExecutablePath = arguments[index]
            case "--health-arg":
                index += 1
                guard index < arguments.count else {
                    throw SnapshotSourceError.invalidArguments("Expected argument value after --health-arg")
                }
                healthArguments.append(arguments[index])
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

        let showStatusWindowOnLaunch = inputURL == nil && !explicitLiveMode
        if inputURL == nil && !useLive {
            useLive = true
        }
        if useLive && refreshIntervalSeconds == nil {
            refreshIntervalSeconds = HarnessRuntimeConfig.defaultRefreshIntervalSeconds
        }

        let source: any HealthSnapshotSource
        if let inputURL {
            source = FixtureHealthSnapshotSource(fileURL: inputURL)
        } else if useLive {
            if let healthExecutablePath {
                source = CLIHealthSnapshotSource(launchPath: healthExecutablePath, arguments: healthArguments)
            } else {
                let liveArguments = healthCommand.map { ["-lc", $0] } ?? ["-lc", "longhouse local-health --json"]
                source = CLIHealthSnapshotSource(arguments: liveArguments)
            }
        } else {
            throw SnapshotSourceError.invalidArguments("Pass either --input <file> or --live")
        }

        return HarnessRuntimeConfig(
            outputURL: outputURL,
            source: source,
            actionLogURL: actionLogURL,
            uiURL: uiURL,
            effectMode: effectMode,
            healthyConcept: healthyConcept,
            exerciseActions: exerciseActions,
            quitAfterSeconds: quitAfterSeconds,
            refreshIntervalSeconds: refreshIntervalSeconds,
            toggleProfileLogURL: toggleProfileLogURL,
            toggleProfileCount: toggleProfileCount,
            toggleProfileIntervalMilliseconds: toggleProfileIntervalMilliseconds,
            healthCommand: healthCommand,
            healthExecutablePath: healthExecutablePath,
            healthArguments: healthArguments,
            showStatusWindowOnLaunch: showStatusWindowOnLaunch
        )
    }
}
