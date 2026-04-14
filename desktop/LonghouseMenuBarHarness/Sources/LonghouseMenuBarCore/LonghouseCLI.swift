import Foundation

enum LonghouseCLI {
    private static let executableName = "longhouse"
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
            homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
            pathEnvironment: ProcessInfo.processInfo.environment["PATH"]
        )
    }

    static func resolveExecutable(homeDirectory: URL, pathEnvironment: String?) -> URL? {
        let fileManager = FileManager.default

        for candidate in candidateURLs(homeDirectory: homeDirectory, pathEnvironment: pathEnvironment) {
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
        snapshot: HealthSnapshot,
        homeDirectory: URL,
        pathEnvironment: String?
    ) -> (launchPath: String, arguments: [String])? {
        guard let executable = resolveExecutable(homeDirectory: homeDirectory, pathEnvironment: pathEnvironment) else {
            return nil
        }

        let machineName = preferredMachineName(snapshot: snapshot)
        return (
            executable.path,
            [
                "connect",
                "--install",
                "--machine-name",
                machineName,
                "--menubar",
            ]
        )
    }

    static func setupInvocation() -> (launchPath: String, arguments: [String])? {
        setupInvocation(resourceBundle: .module)
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

    private static func preferredMachineName(snapshot: HealthSnapshot) -> String {
        let candidates = [
            snapshot.launchReadiness?.machineName,
            snapshot.launchReadiness?.serviceMachineName,
            ProcessInfo.processInfo.hostName,
        ]

        for candidate in candidates {
            let trimmed = candidate?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !trimmed.isEmpty {
                return trimmed
            }
        }

        return "this-mac"
    }

    private static func candidateURLs(homeDirectory: URL, pathEnvironment: String?) -> [URL] {
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
