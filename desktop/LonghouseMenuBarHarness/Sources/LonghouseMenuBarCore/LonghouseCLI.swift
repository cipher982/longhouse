import Foundation

enum LonghouseCLI {
    private static let executableName = "longhouse"
    private static let engineExecutableName = "longhouse-engine"
    private static let setupScriptName = "desktop-app-setup"
    private static let standardPathEntries = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]

    static func resolveExecutable() -> URL? {
        resolveExecutable(
            named: executableName,
            homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"]
        )
    }

    static func resolveEngineExecutable() -> URL? {
        resolveExecutable(
            named: engineExecutableName,
            homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"]
        )
    }

    static func resolveExecutable(homeDirectory: URL, pathEnvironment: String?) -> URL? {
        resolveExecutable(
            named: executableName,
            homeDirectory: homeDirectory,
            pathEnvironment: pathEnvironment
        )
    }

    private static func resolveExecutable(
        named executableName: String,
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> URL? {
        let fileManager = FileManager.default

        for candidate in candidateURLs(
            named: executableName,
            homeDirectory: homeDirectory,
            pathEnvironment: pathEnvironment
        ) {
            if fileManager.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }

        return nil
    }

    static func defaultHealthInvocation() -> (launchPath: String, arguments: [String]) {
        defaultHealthInvocation(
            homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"]
        )
    }

    static func defaultHealthInvocation(
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> (launchPath: String, arguments: [String]) {
        if let executable = resolveExecutable(homeDirectory: homeDirectory, pathEnvironment: pathEnvironment) {
            return (executable.path, ["local-health", "--json"])
        }

        return ("/bin/zsh", ["-lc", "longhouse local-health --json"])
    }

    static func repairInstallInvocation(snapshot: HealthSnapshot) -> (launchPath: String, arguments: [String])? {
        repairInstallInvocation(
            snapshot: snapshot,
            homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"]
        )
    }

    static func repairInstallInvocation(
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> (launchPath: String, arguments: [String])? {
        repairInstallInvocation(
            snapshot: nil,
            homeDirectory: homeDirectory,
            pathEnvironment: pathEnvironment
        )
    }

    static func repairInstallInvocation(
        snapshot: HealthSnapshot?,
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> (launchPath: String, arguments: [String])? {
        guard let executable = resolveExecutable(homeDirectory: homeDirectory, pathEnvironment: pathEnvironment) else {
            return nil
        }

        var arguments = [
            "machine",
            "repair",
            "--json",
        ]
        if let snapshot, shouldRepairServiceArtifact(snapshot) {
            arguments.append("--repair-service")
        }
        return (
            executable.path,
            arguments
        )
    }

    private static func shouldRepairServiceArtifact(_ snapshot: HealthSnapshot) -> Bool {
        // These are native observations, not inferred from a stale engine or
        // parsed from the human-readable repair command.
        snapshot.reasons.contains {
            switch $0 {
            case "service_not_installed", "service_artifact_mismatch",
                 "service_generation_mismatch", "service_machine_name_mismatch",
                 "service_state_hash_mismatch", "service_runner_name_mismatch":
                return true
            default:
                return false
            }
        }
    }

    static func setupInvocation() -> (launchPath: String, arguments: [String])? {
        guard let scriptURL = LonghouseResourceLocator.url(forResource: setupScriptName, withExtension: "sh") else {
            return nil
        }
        return ("/bin/zsh", [scriptURL.path])
    }

    static func setupInvocation(resourceBundle: Bundle) -> (launchPath: String, arguments: [String])? {
        guard let scriptURL = resourceBundle.url(forResource: setupScriptName, withExtension: "sh") else {
            return nil
        }

        return ("/bin/zsh", [scriptURL.path])
    }

    static func environment(prependingExecutablePath executablePath: String? = nil) -> [String: String] {
        environment(
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"],
            prependingExecutablePath: executablePath
        )
    }

    static func environment(
        pathEnvironment: String?,
        prependingExecutablePath executablePath: String?
    ) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        var pathEntries: [String] = []

        if let executablePath {
            pathEntries.append(URL(fileURLWithPath: executablePath).deletingLastPathComponent().path)
        }

        if let pathEnvironment, !pathEnvironment.isEmpty {
            pathEntries.append(contentsOf: pathEnvironment.split(separator: ":").map(String.init))
        }

        pathEntries.append(contentsOf: standardPathEntries)

        var seen: Set<String> = []
        environment["PATH"] = pathEntries
            .filter { !$0.isEmpty }
            .filter { seen.insert($0).inserted }
            .joined(separator: ":")
        return environment
    }

    private static func candidateURLs(homeDirectory: URL, pathEnvironment: String?) -> [URL] {
        candidateURLs(
            named: executableName,
            homeDirectory: homeDirectory,
            pathEnvironment: pathEnvironment
        )
    }

    private static func candidateURLs(
        named executableName: String,
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> [URL] {
        var urls: [URL] = [
            homeDirectory.appendingPathComponent(".local/bin/\(executableName)"),
            homeDirectory.appendingPathComponent("bin/\(executableName)"),
            URL(fileURLWithPath: "/opt/homebrew/bin/\(executableName)"),
            URL(fileURLWithPath: "/usr/local/bin/\(executableName)"),
            URL(fileURLWithPath: "/usr/bin/\(executableName)"),
        ]

        if let pathEnvironment {
            for entry in pathEnvironment.split(separator: ":") {
                urls.append(URL(fileURLWithPath: String(entry)).appendingPathComponent(executableName))
            }
        }

        var seen: Set<String> = []
        return urls.filter { seen.insert($0.path).inserted }
    }
}
