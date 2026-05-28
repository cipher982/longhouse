import Foundation
import Dispatch

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
    public static let defaultCommandTimeoutSeconds: TimeInterval = 20

    public let launchPath: String
    public let arguments: [String]
    public let commandTimeoutSeconds: TimeInterval
    let currentBundlePath: String?

    public init() {
        let invocation = LonghouseCLI.defaultHealthInvocation()
        self.launchPath = invocation.launchPath
        self.arguments = invocation.arguments
        self.commandTimeoutSeconds = Self.defaultCommandTimeoutSeconds
        self.currentBundlePath = nil
    }

    public init(
        launchPath: String,
        arguments: [String],
        commandTimeoutSeconds: TimeInterval = Self.defaultCommandTimeoutSeconds,
        currentBundlePath: String? = nil
    ) {
        self.launchPath = launchPath
        self.arguments = arguments
        self.commandTimeoutSeconds = commandTimeoutSeconds
        self.currentBundlePath = currentBundlePath
    }

    public func load() throws -> HealthSnapshot {
        let bundlePath = currentBundlePath ?? Bundle.main.bundleURL.path
        if let unsupportedBundlePath = AppBundleLocation.unsupportedBundlePath(currentBundlePath: bundlePath) {
            return HealthSnapshot.installLocationBlockedSnapshot(currentPath: unsupportedBundlePath)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        process.environment = LonghouseCLI.environment(prependingExecutablePath: launchPath)

        let stdoutURL = Self.temporaryOutputURL(suffix: "stdout")
        let stderrURL = Self.temporaryOutputURL(suffix: "stderr")
        FileManager.default.createFile(atPath: stdoutURL.path, contents: nil)
        FileManager.default.createFile(atPath: stderrURL.path, contents: nil)

        let stdout = try FileHandle(forWritingTo: stdoutURL)
        let stderr = try FileHandle(forWritingTo: stderrURL)
        defer {
            try? FileManager.default.removeItem(at: stdoutURL)
            try? FileManager.default.removeItem(at: stderrURL)
        }

        process.standardOutput = stdout
        process.standardError = stderr
        let didExit = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            didExit.signal()
        }
        do {
            try process.run()
        } catch {
            try? stdout.close()
            try? stderr.close()
            throw error
        }
        if didExit.wait(timeout: .now() + commandTimeoutSeconds) == .timedOut {
            process.terminate()
            _ = didExit.wait(timeout: .now() + 2)
            try? stdout.close()
            try? stderr.close()
            throw SnapshotSourceError.commandFailed("Longhouse status snapshot timed out after \(Int(commandTimeoutSeconds))s")
        }

        try? stdout.close()
        try? stderr.close()

        let output = try Data(contentsOf: stdoutURL)
        let errorOutput = try Data(contentsOf: stderrURL)
        guard process.terminationStatus == 0 else {
            let message = String(data: errorOutput, encoding: .utf8) ?? "Longhouse status snapshot failed"
            if shouldSynthesizeSetupRequiredSnapshot(message: message, terminationStatus: process.terminationStatus) {
                return HealthSnapshot.setupRequiredSnapshot(detail: message)
            }
            throw SnapshotSourceError.commandFailed(message)
        }
        return try HealthSnapshotDecoder.decode(data: output)
    }

    private static func temporaryOutputURL(suffix: String) -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-health-\(UUID().uuidString)-\(suffix)")
    }

    private func shouldSynthesizeSetupRequiredSnapshot(message: String, terminationStatus: Int32) -> Bool {
        guard terminationStatus == 127 else {
            return false
        }

        let attemptedCommand = ([launchPath] + arguments).joined(separator: " ").lowercased()
        guard attemptedCommand.contains("longhouse") else {
            return false
        }

        return message.lowercased().contains("command not found")
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
    public static let defaultRefreshIntervalSeconds: TimeInterval = 30

    public let outputURL: URL?
    public let source: any HealthSnapshotSource
    public let actionLogURL: URL?
    public let uiURL: URL?
    public let effectMode: HarnessEffectMode
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
    public let headerSummaryVariant: HeaderSummaryVariant

    public init(
        outputURL: URL?,
        source: any HealthSnapshotSource,
        actionLogURL: URL?,
        uiURL: URL?,
        effectMode: HarnessEffectMode,
        exerciseActions: [HarnessAction],
        quitAfterSeconds: TimeInterval?,
        refreshIntervalSeconds: TimeInterval?,
        toggleProfileLogURL: URL?,
        toggleProfileCount: Int,
        toggleProfileIntervalMilliseconds: Int,
        healthCommand: String?,
        healthExecutablePath: String?,
        healthArguments: [String],
        showStatusWindowOnLaunch: Bool,
        headerSummaryVariant: HeaderSummaryVariant
    ) {
        self.outputURL = outputURL
        self.source = source
        self.actionLogURL = actionLogURL
        self.uiURL = uiURL
        self.effectMode = effectMode
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
        self.headerSummaryVariant = headerSummaryVariant
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
        var toggleProfileLogURL: URL?
        var toggleProfileCount = 0
        var toggleProfileIntervalMilliseconds = 120
        var healthCommand: String?
        var healthExecutablePath: String?
        var healthArguments: [String] = []
        var explicitLiveMode = false
        var headerSummaryVariant = HeaderSummaryVariant.default

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
            case "--header-variant":
                index += 1
                guard index < arguments.count, let parsed = HeaderSummaryVariant(rawValue: arguments[index]) else {
                    let allowed = HeaderSummaryVariant.allCases.map(\.rawValue).joined(separator: ", ")
                    throw SnapshotSourceError.invalidArguments("Expected --header-variant \(allowed)")
                }
                headerSummaryVariant = parsed
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
                if let healthCommand {
                    source = CLIHealthSnapshotSource(launchPath: "/bin/zsh", arguments: ["-lc", healthCommand])
                } else {
                    source = CLIHealthSnapshotSource()
                }
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
            exerciseActions: exerciseActions,
            quitAfterSeconds: quitAfterSeconds,
            refreshIntervalSeconds: refreshIntervalSeconds,
            toggleProfileLogURL: toggleProfileLogURL,
            toggleProfileCount: toggleProfileCount,
            toggleProfileIntervalMilliseconds: toggleProfileIntervalMilliseconds,
            healthCommand: healthCommand,
            healthExecutablePath: healthExecutablePath,
            healthArguments: healthArguments,
            showStatusWindowOnLaunch: showStatusWindowOnLaunch,
            headerSummaryVariant: headerSummaryVariant
        )
    }
}
