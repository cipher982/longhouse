import Foundation
import Testing

@testable import LonghouseMenuBarCore

struct LonghouseMenuBarCoreTests {
    @Test
    func decodesHealthyFixture() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/healthy.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.headline == "Longhouse shipping healthy")
        #expect(snapshot.parsedSeverity == .green)
        #expect(snapshot.service?.status == "running")
        #expect(snapshot.engineStatus?.payload?.spoolPendingCount == 0)
        #expect(snapshot.launchReadiness?.state == "ready")
        #expect(snapshot.launchReadiness?.runner?.runnerName == "cinder")
    }

    @Test
    func parsesRuntimeArguments() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: [
            "--input", "/tmp/example.json",
            "--output", "/tmp/example.png",
            "--action-log", "/tmp/actions.jsonl",
            "--ui-url", "https://longhouse.ai",
            "--effect-mode", "log-only",
            "--exercise-actions", "refresh,copyDiagnostics",
            "--quit-after", "2.5",
            "--refresh-seconds", "5",
            "--health-command", "python -m zerg.cli.main local-health --json"
        ])

        #expect(config.outputURL?.path == "/tmp/example.png")
        #expect(config.actionLogURL?.path == "/tmp/actions.jsonl")
        #expect(config.uiURL?.absoluteString == "https://longhouse.ai")
        #expect(config.effectMode == .logOnly)
        #expect(config.exerciseActions == [.refresh, .copyDiagnostics])
        #expect(config.quitAfterSeconds == 2.5)
        #expect(config.refreshIntervalSeconds == 5)
        #expect(config.healthCommand == "python -m zerg.cli.main local-health --json")
        #expect(config.showStatusWindowOnLaunch == false)
    }

    @Test
    func parsesDirectHealthExecutableArguments() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: [
            "--live",
            "--health-exec", "/usr/bin/python3",
            "--health-arg", "-m",
            "--health-arg", "zerg.cli.main",
            "--health-arg", "local-health",
            "--health-arg", "--json",
        ])

        #expect(config.healthExecutablePath == "/usr/bin/python3")
        #expect(config.healthArguments == ["-m", "zerg.cli.main", "local-health", "--json"])
    }

    @Test
    func defaultsDirectLaunchToLiveStatusWindow() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: [])

        #expect(config.refreshIntervalSeconds == HarnessRuntimeConfig.defaultRefreshIntervalSeconds)
        #expect(config.showStatusWindowOnLaunch == true)
    }

    @Test
    func resolvesLonghouseURLFromSnapshotWhenUIURLMissing() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: LaunchReadinessSnapshot(
                state: "ready",
                headline: nil,
                reasons: nil,
                suggestedActions: nil,
                storedURL: "https://david010.longhouse.ai",
                machineName: nil,
                serviceMachineName: nil,
                runner: nil
            )
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)

        #expect(sink.resolveLonghouseURL(snapshot: snapshot)?.absoluteString == "https://david010.longhouse.ai")
    }

    @Test
    func spyActionSinkPersistsActions() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let logURL = tempDir.appendingPathComponent("actions.jsonl")

        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: logURL, uiURL: nil, effectMode: .logOnly)
        _ = sink.handle(.refresh, snapshot: snapshot)

        let content = try String(contentsOf: logURL)
        #expect(content.contains("\"action\":\"refresh\""))
        #expect(content.contains("Longhouse shipping healthy"))
    }

    @Test
    func refreshDoesNotReturnVisibleFeedback() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.refresh, snapshot: snapshot)

        #expect(feedback == nil)
    }

    @Test
    func quitDryRunReturnsVisibleFeedback() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.quitApp, snapshot: snapshot)

        #expect(feedback?.style == .info)
        #expect(feedback?.title == "Quit dry run recorded")
    }

    @Test
    func repairDryRunReturnsVisibleFeedback() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse engine service is stopped",
            reasons: ["service_stopped"],
            suggestedActions: ["Run: longhouse connect --install"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.repairInstall, snapshot: snapshot)

        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Repair dry run recorded")
        #expect(feedback?.detail.contains("longhouse connect --install") == true)
    }

    @Test
    func setupDryRunReturnsVisibleFeedback() throws {
        let snapshot = HealthSnapshot.setupRequiredSnapshot(detail: "zsh:1: command not found: longhouse")

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.repairInstall, snapshot: snapshot)

        #expect(feedback?.style == .info)
        #expect(feedback?.title == "Setup dry run recorded")
        #expect(feedback?.detail.contains("built-in Longhouse setup") == true)
    }

    @Test
    func appLocationBlockedDryRunReturnsMoveFeedback() throws {
        let snapshot = HealthSnapshot.installLocationBlockedSnapshot(
            currentPath: "/Users/test/Applications/Longhouse.app"
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.repairInstall, snapshot: snapshot)

        #expect(snapshot.isInstallLocationBlocked == true)
        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Move dry run recorded")
    }

    @Test
    func cliSourceReturnsSetupRequiredSnapshotWhenLonghouseIsMissing() throws {
        let source = CLIHealthSnapshotSource(
            launchPath: "/bin/zsh",
            arguments: ["-lc", "__longhouse_missing_for_test__ local-health --json"]
        )

        let snapshot = try source.load()

        #expect(snapshot.isSetupRequired == true)
        #expect(snapshot.headline == "Longhouse setup required")
        #expect(snapshot.launchReadiness?.state == "setup-required")
    }

    @Test
    func cliSourceReturnsInstallLocationBlockedSnapshotWhenBundlePathIsUnsupported() throws {
        let source = CLIHealthSnapshotSource(
            launchPath: "/bin/zsh",
            arguments: ["-lc", "__longhouse_missing_for_test__ local-health --json"],
            currentBundlePath: "/Users/test/Applications/Longhouse.app"
        )

        let snapshot = try source.load()

        #expect(snapshot.isInstallLocationBlocked == true)
        #expect(snapshot.headline == "Move Longhouse.app to Applications")
        #expect(snapshot.launchReadiness?.state == "move-app")
    }

    @Test
    func appBundleLocationOnlyAllowsApplicationsPath() {
        #expect(AppBundleLocation.unsupportedBundlePath(currentBundlePath: "/Applications/Longhouse.app") == nil)
        #expect(
            AppBundleLocation.unsupportedBundlePath(currentBundlePath: "/Users/test/Applications/Longhouse.app")
            == "/Users/test/Applications/Longhouse.app"
        )
        #expect(AppBundleLocation.unsupportedBundlePath(currentBundlePath: "/tmp/LonghouseMenuBarHarness") == nil)
    }

    @Test
    func defaultHealthInvocationPrefersUserLocalBinary() throws {
        let homeDirectory = try makeFakeHomeDirectory()
        let executableURL = try installFakeLonghouseBinary(homeDirectory: homeDirectory)

        let invocation = LonghouseCLI.defaultHealthInvocation(
            homeDirectory: homeDirectory,
            pathEnvironment: "/usr/bin:/bin"
        )

        #expect(invocation.launchPath == executableURL.path)
        #expect(invocation.arguments == ["local-health", "--json"])
    }

    @Test
    func repairInstallInvocationUsesResolvedCLIAndPreferredMachineName() throws {
        let homeDirectory = try makeFakeHomeDirectory()
        let executableURL = try installFakeLonghouseBinary(homeDirectory: homeDirectory)
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse engine service is stopped",
            reasons: ["service_stopped"],
            suggestedActions: ["Run: longhouse connect --install"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: LaunchReadinessSnapshot(
                state: "repair-required",
                headline: nil,
                reasons: nil,
                suggestedActions: nil,
                storedURL: nil,
                machineName: "ember",
                serviceMachineName: "fallback-name",
                runner: nil
            )
        )

        let invocation = LonghouseCLI.repairInstallInvocation(
            snapshot: snapshot,
            homeDirectory: homeDirectory,
            pathEnvironment: "/usr/bin:/bin"
        )

        #expect(invocation?.launchPath == executableURL.path)
        #expect(invocation?.arguments == [
            "connect",
            "--install",
            "--machine-name",
            "ember",
            "--menubar",
        ])
    }

    @Test
    func setupInvocationUsesBundledSetupScript() {
        let invocation = LonghouseCLI.setupInvocation(resourceBundle: .module)

        #expect(invocation?.launchPath == "/bin/zsh")
        #expect(invocation?.arguments.count == 1)
        #expect(invocation?.arguments.first?.hasSuffix("desktop-app-setup.sh") == true)
    }

    @Test
    func decodesUpdateInfoFromSnakeCaseJSON() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "update_info": {
            "installed_version": "0.1.8",
            "latest_version": "0.1.9",
            "update_available": true,
            "upgrade_command": "uv tool upgrade longhouse",
            "checked_at": "2026-04-11T10:00:00+00:00"
          }
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)

        #expect(snapshot.updateInfo?.installedVersion == "0.1.8")
        #expect(snapshot.updateInfo?.latestVersion == "0.1.9")
        #expect(snapshot.updateInfo?.updateAvailable == true)
        #expect(snapshot.updateInfo?.upgradeCommand == "uv tool upgrade longhouse")
        #expect(snapshot.updateInfo?.checkedAt == "2026-04-11T10:00:00+00:00")
    }

    @Test
    func relativeLabelsAdvanceAgainstPresentationTime() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: nil,
                exists: true,
                fresh: true,
                ageSeconds: 4,
                payload: EngineStatusPayload(
                    version: nil,
                    daemonPid: nil,
                    lastShipAt: "2026-04-08T01:51:20Z",
                    spoolPendingCount: 0,
                    spoolDeadCount: 0,
                    parseErrorCount1H: 0,
                    consecutiveShipFailures: 0,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: nil
                ),
                error: nil
            ),
            outbox: nil,
            activitySummary: ActivitySummarySnapshot(
                path: nil,
                exists: true,
                error: nil,
                sessionsToday: 4,
                sessionsRecent: 2,
                providerCountsToday: ["codex": 4],
                providerCountsRecent: ["codex": 2],
                sessionRecencyBands: nil,
                recentTouches: nil,
                latestActivityAt: "2026-04-08T01:51:30Z",
                recentWindowMinutes: 15
            ),
            launchReadiness: LaunchReadinessSnapshot(
                state: "ready",
                headline: nil,
                reasons: nil,
                suggestedActions: nil,
                storedURL: nil,
                machineName: "cinder",
                serviceMachineName: nil,
                runner: nil
            )
        )

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let referenceDate = formatter.date(from: "2026-04-08T01:53:20Z")!

        #expect(snapshot.snapshotAgeCompactLabel(relativeTo: referenceDate) == "1m")
        #expect(snapshot.lastShipCompactLabel(relativeTo: referenceDate) == "2m")
        #expect(snapshot.engineAgeLabel(relativeTo: referenceDate) == "1m")
        #expect(snapshot.engineFreshnessLabel(relativeTo: referenceDate) == "Aging")
    }

    @Test
    func decodesRecentTouchesAndRecentProviderMix() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "activity_summary": {
            "sessions_today": 7,
            "sessions_recent": 4,
            "provider_counts_today": {
              "codex": 3,
              "claude": 4
            },
            "provider_counts_recent": {
              "codex": 1,
              "claude": 3
            },
            "recent_touches": [
              { "provider": "claude", "workspace_label": "zerg", "last_updated": "2026-04-11T10:00:00Z" },
              { "provider": "codex", "workspace_label": "crims", "last_updated": "2026-04-11T09:57:00Z" }
            ],
            "session_recency_bands": [
              { "label": "0-1m", "session_count": 2 },
              { "label": "1-5m", "session_count": 1 },
              { "label": "5-15m", "session_count": 1 },
              { "label": "15-60m", "session_count": 0 },
              { "label": "1-6h", "session_count": 2 },
              { "label": "6h+", "session_count": 1 }
            ],
            "latest_activity_at": "2026-04-11T10:00:00Z",
            "recent_window_minutes": 15
          }
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)

        #expect(snapshot.providerCountsRecent.map(\.provider) == ["claude", "codex"])
        #expect(snapshot.recentProviderMixLabel == "Claude 3 · Codex 1")
        #expect(snapshot.recentActivitySummaryLabel == "4 active in 15m")
        #expect(snapshot.recentTouches.count == 2)
        #expect(snapshot.recentTouches.first?.provider == "claude")
        #expect(snapshot.recentTouches.first?.workspaceLabel == "zerg")
        #expect(snapshot.recentTouches.first?.lastUpdated == "2026-04-11T10:00:00Z")
    }

    private func makeFakeHomeDirectory() throws -> URL {
        let tempDirectory = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
        return tempDirectory
    }

    private func installFakeLonghouseBinary(homeDirectory: URL) throws -> URL {
        let binDirectory = homeDirectory.appendingPathComponent(".local/bin", isDirectory: true)
        try FileManager.default.createDirectory(at: binDirectory, withIntermediateDirectories: true)
        let executableURL = binDirectory.appendingPathComponent("longhouse", isDirectory: false)
        let contents = Data("#!/bin/sh\nexit 0\n".utf8)
        FileManager.default.createFile(atPath: executableURL.path, contents: contents)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: executableURL.path
        )
        return executableURL
    }
}
