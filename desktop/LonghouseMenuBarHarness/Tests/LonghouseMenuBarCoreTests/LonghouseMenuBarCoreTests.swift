import AppKit
import Foundation
import Testing

@testable import LonghouseMenuBarCore

private struct ManagedPhaseContractFile: Decodable {
    let phases: [ManagedPhaseContractCase]
}

private struct ManagedPhaseContractCase: Decodable {
    let rawPhase: String
    let displayLabel: String
    let toolDisplayFormat: String?
    let attention: String
}

private struct StaticHealthSnapshotSource: HealthSnapshotSource {
    let snapshot: HealthSnapshot

    func load() throws -> HealthSnapshot {
        snapshot
    }
}

private struct ThrowingHealthSnapshotSource: HealthSnapshotSource {
    func load() throws -> HealthSnapshot {
        throw SnapshotSourceError.commandFailed("boom")
    }
}

struct LonghouseMenuBarCoreTests {
    @Test
    func statusItemSourceIconHasZeroPadding() throws {
        let iconURL = try #require(Bundle.module.url(forResource: "LonghouseMenuIcon", withExtension: "png"))
        let data = try Data(contentsOf: iconURL)
        let rep = try #require(NSBitmapImageRep(data: data))

        var minX = rep.pixelsWide
        var minY = rep.pixelsHigh
        var maxX = -1
        var maxY = -1

        for y in 0..<rep.pixelsHigh {
            for x in 0..<rep.pixelsWide {
                guard let color = rep.colorAt(x: x, y: y), color.alphaComponent > 0.001 else {
                    continue
                }
                minX = min(minX, x)
                minY = min(minY, y)
                maxX = max(maxX, x)
                maxY = max(maxY, y)
            }
        }

        #expect(minX == 0)
        #expect(minY == 0)
        #expect(maxX == rep.pixelsWide - 1)
        #expect(maxY == rep.pixelsHigh - 1)
    }

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
    func decodesRestartPendingFixture() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/restart-pending.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.parsedSeverity == .green)
        #expect(snapshot.displaySeverity == .green)
        #expect(snapshot.hasResolvedInstalledVersion == true)
        #expect(snapshot.installedVersionLabel == "0.1.15-dev+bbbbbbbb.dirty")
        #expect(snapshot.engineRestartPending == true)
        #expect(snapshot.restartPendingChipLabel == "RESTART PENDING")
        #expect(snapshot.needsMenuBarAttention == false)
    }

    @Test
    func parsesRuntimeArguments() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: [
            "--input", "/tmp/example.json",
            "--output", "/tmp/example.png",
            "--action-log", "/tmp/actions.jsonl",
            "--ui-url", "https://longhouse.ai",
            "--header-variant", "telemetry-rail",
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
        #expect(config.headerSummaryVariant == .telemetryRail)
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
                storedURL: "https://demo.longhouse.test",
                machineName: nil,
                serviceMachineName: nil,
                runner: nil
            )
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)

        #expect(sink.resolveLonghouseURL(snapshot: snapshot)?.absoluteString == "https://demo.longhouse.test")
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
            suggestedActions: ["Run: longhouse machine repair"],
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
        #expect(feedback?.detail.contains("longhouse machine repair") == true)
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
    func stopManagedBridgeDryRunReturnsVisibleFeedback() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse lost managed session control",
            reasons: ["managed_session_control_degraded"],
            suggestedActions: ["Inspect degraded managed sessions in Longhouse.app before sending input"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handleStopManagedBridge(
            sessionID: "session-123",
            workspaceLabel: "zerg",
            snapshot: snapshot
        )

        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Stop dry run recorded")
        #expect(feedback?.detail.contains("zerg") == true)
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
        #expect(invocation.arguments == ["local-health", "--fast", "--json"])
    }

    @Test
    @MainActor
    func snapshotStorePersistsLastGoodSnapshot() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let cacheURL = tempDir.appendingPathComponent("last-good.json")
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:00Z",
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

        _ = SnapshotStore(source: StaticHealthSnapshotSource(snapshot: snapshot), cacheURL: cacheURL)

        let cached = try HealthSnapshotDecoder.decode(data: Data(contentsOf: cacheURL))
        #expect(cached.headline == "Longhouse shipping healthy")
    }

    @Test
    @MainActor
    func snapshotStoreLoadsLastGoodSnapshotBeforeRefresh() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let cacheURL = tempDir.appendingPathComponent("last-good.json")
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Cached Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        try encoder.encode(snapshot).write(to: cacheURL)

        let store = SnapshotStore(source: ThrowingHealthSnapshotSource(), cacheURL: cacheURL)

        #expect(store.snapshot?.headline == "Cached Longhouse status")
        #expect(store.loadError == "boom")
    }

    @Test
    @MainActor
    func snapshotStoreTreatsOldCachedSnapshotWithRefreshFailureAsStale() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let cacheURL = tempDir.appendingPathComponent("last-good.json")
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Cached Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        try encoder.encode(snapshot).write(to: cacheURL)

        let store = SnapshotStore(source: ThrowingHealthSnapshotSource(), cacheURL: cacheURL)
        let referenceDate = try #require(HealthSnapshot.parseISO8601("2026-05-05T12:03:00Z"))

        let message = try #require(store.staleCachedSnapshotFailureMessage(relativeTo: referenceDate))
        #expect(message.contains("Longhouse status is stale"))
        #expect(message.contains("3m ago"))
        #expect(message.contains("boom"))
    }

    @Test
    @MainActor
    func snapshotStoreAllowsRecentCachedSnapshotToBridgeRefreshFailure() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let cacheURL = tempDir.appendingPathComponent("last-good.json")
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Cached Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        try encoder.encode(snapshot).write(to: cacheURL)

        let store = SnapshotStore(source: ThrowingHealthSnapshotSource(), cacheURL: cacheURL)
        let referenceDate = try #require(HealthSnapshot.parseISO8601("2026-05-05T12:01:00Z"))

        #expect(store.staleCachedSnapshotFailureMessage(relativeTo: referenceDate) == nil)
        #expect(store.snapshot?.headline == "Cached Longhouse status")
        #expect(store.loadError == "boom")
    }

    @Test
    func repairInstallInvocationUsesResolvedCLI() throws {
        let homeDirectory = try makeFakeHomeDirectory()
        let executableURL = try installFakeLonghouseBinary(homeDirectory: homeDirectory)
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse engine service is stopped",
            reasons: ["service_stopped"],
            suggestedActions: ["Run: longhouse machine repair"],
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
                runner: RunnerSnapshot(
                    path: nil,
                    exists: true,
                    error: nil,
                    runnerName: "ember",
                    runnerID: nil,
                    runnerURLs: ["https://demo.longhouse.test"],
                    installMode: "desktop"
                )
            )
        )

        let invocation = LonghouseCLI.repairInstallInvocation(
            snapshot: snapshot,
            homeDirectory: homeDirectory,
            pathEnvironment: "/usr/bin:/bin"
        )

        #expect(invocation?.launchPath == executableURL.path)
        #expect(invocation?.arguments == [
            "machine",
            "repair",
        ])
    }

    @Test
    func repairInstallInvocationDoesNotDependOnSnapshotURLs() throws {
        let homeDirectory = try makeFakeHomeDirectory()
        let executableURL = try installFakeLonghouseBinary(homeDirectory: homeDirectory)
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse launch config is inconsistent",
            reasons: ["config_url_runner_url_mismatch"],
            suggestedActions: ["Run: longhouse machine repair"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: LaunchReadinessSnapshot(
                state: "repair-required",
                headline: nil,
                reasons: nil,
                suggestedActions: nil,
                storedURL: "https://stored.longhouse.ai",
                machineName: "ember",
                serviceMachineName: nil,
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
            "machine",
            "repair",
        ])
    }

    @Test
    func yellowAndRedSnapshotsRequestMenuBarAttention() {
        let broken = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse launch config is inconsistent",
            reasons: ["config_url_runner_url_mismatch"],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let degraded = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse shipping is degraded",
            reasons: ["spool_pending"],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let healthy = HealthSnapshot(
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

        #expect(broken.needsMenuBarAttention == true)
        #expect(degraded.needsMenuBarAttention == true)
        #expect(healthy.needsMenuBarAttention == false)
    }

    @Test
    func attentionSummaryExplainsShippingBacklogWithCounts() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse shipping is degraded",
            reasons: ["spool_pending", "outbox_backlog"],
            suggestedActions: ["Run: longhouse machine repair"],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: nil,
                exists: true,
                fresh: true,
                ageSeconds: 4,
                payload: EngineStatusPayload(
                    version: "0.1.16",
                    daemonPid: 123,
                    lastShipAt: "2026-04-08T01:51:00Z",
                    spoolPendingCount: 1,
                    spoolDeadCount: 0,
                    parseErrorCount1H: 0,
                    consecutiveShipFailures: 0,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: "2026-04-08T01:52:00Z"
                ),
                error: nil
            ),
            outbox: OutboxSnapshot(path: nil, fileCount: 10, oldestAgeSeconds: 480),
            activitySummary: nil,
            launchReadiness: nil
        )

        #expect(snapshot.attentionSummaryLabel.contains("1 queued transcript range"))
        #expect(snapshot.attentionSummaryLabel.contains("10 local hook events"))
        #expect(snapshot.attentionSummaryLabel.contains("replay backlog"))
    }

    @Test
    func attentionSummaryExplainsConsecutiveShippingFailures() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse shipping is degraded",
            reasons: ["consecutive_failures"],
            suggestedActions: ["Run: longhouse machine repair"],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: nil,
                exists: true,
                fresh: true,
                ageSeconds: 4,
                payload: EngineStatusPayload(
                    version: "0.1.16",
                    daemonPid: 123,
                    lastShipAt: "2026-04-08T01:51:00Z",
                    spoolPendingCount: 0,
                    spoolDeadCount: 0,
                    parseErrorCount1H: 0,
                    consecutiveShipFailures: 3,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: "2026-04-08T01:52:00Z"
                ),
                error: nil
            ),
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        #expect(snapshot.attentionSummaryLabel.contains("3 consecutive shipping failures"))
        #expect(snapshot.attentionSummaryLabel.contains("still failing to connect"))
    }

    @Test
    func managedAttentionOverridesDisplaySeverity() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/managed-degraded.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.parsedSeverity == .green)
        #expect(snapshot.managedAttentionSeverity == .red)
        #expect(snapshot.displaySeverity == .red)
    }

    @Test
    func setupInvocationUsesBundledSetupScript() {
        let invocation = LonghouseCLI.setupInvocation(resourceBundle: .module)

        #expect(invocation?.launchPath == "/bin/zsh")
        #expect(invocation?.arguments.count == 1)
        #expect(invocation?.arguments.first?.hasSuffix("desktop-app-setup.sh") == true)
    }

    @Test
    func bundledSetupScriptPinsReleaseVersionOrOverride() throws {
        let scriptURL = try #require(Bundle.module.url(forResource: "desktop-app-setup", withExtension: "sh"))
        let script = try String(contentsOf: scriptURL, encoding: .utf8)

        #expect(script.contains("LONGHOUSE_PKG_SOURCE"))
        #expect(script.contains("CFBundleShortVersionString"))
        #expect(script.contains("longhouse=="))
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
        #expect(snapshot.recentTouchTitle(snapshot.recentTouches[0]) == "zerg · Claude")
        #expect(snapshot.recentTouchTitle(snapshot.recentTouches[1]) == "crims · Codex")
    }

    @Test
    func recentTouchTitleFallsBackToProviderWhenWorkspaceMissing() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: nil,
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: ActivitySummarySnapshot(
                path: nil,
                exists: true,
                error: nil,
                sessionsToday: 1,
                sessionsRecent: 1,
                providerCountsToday: ["claude": 1],
                providerCountsRecent: ["claude": 1],
                sessionRecencyBands: nil,
                recentTouches: [
                    ActivityTouchSnapshot(
                        provider: "claude",
                        lastUpdated: "2026-04-11T10:00:00Z",
                        workspaceLabel: nil,
                        branch: nil,
                        isSubagent: false
                    )
                ],
                latestActivityAt: "2026-04-11T10:00:00Z",
                recentWindowMinutes: 15
            ),
            launchReadiness: nil
        )

        #expect(snapshot.recentTouches.count == 1)
        #expect(snapshot.recentTouchTitle(snapshot.recentTouches[0]) == "Claude")
    }

    @Test
    func liveUnmanagedProcessSummaryUsesExplicitProcessTruth() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: nil,
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: ActivitySummarySnapshot(
                path: nil,
                exists: true,
                error: nil,
                sessionsToday: 4,
                sessionsRecent: 4,
                providerCountsToday: ["claude": 2, "codex": 2],
                providerCountsRecent: ["claude": 2, "codex": 2],
                sessionRecencyBands: nil,
                recentTouches: [
                    ActivityTouchSnapshot(
                        provider: "claude",
                        lastUpdated: "2026-04-11T10:00:00Z",
                        workspaceLabel: "zeta-athena-horizon",
                        branch: nil,
                        isSubagent: false
                    )
                ],
                latestActivityAt: "2026-04-11T10:00:00Z",
                recentWindowMinutes: 15
            ),
            managedSummary: ManagedSummarySnapshot(
                attachedCount: 2,
                detachedCount: 0,
                degradedCount: 0,
                orphanBridgeCount: 0,
                latestActivityAt: "2026-04-11T10:00:00Z"
            ),
            managedSessions: [
                ManagedSessionSnapshot(
                    sessionId: "managed-claude-1",
                    provider: "claude",
                    workspaceLabel: "athena-horizon",
                    branch: nil,
                    state: "attached",
                    phase: "thinking",
                    rawPhase: "thinking",
                    phaseObservedAt: "2026-04-11T10:00:00Z",
                    lastActivityAt: "2026-04-11T10:00:00Z",
                    bridgeStatus: nil,
                    bridgePid: nil,
                    bridgeHeartbeatAt: nil,
                    reasonCodes: []
                )
            ],
            unmanagedProcesses: [
                UnmanagedProcessSnapshot(
                    provider: "codex",
                    pid: 48047,
                    workspaceLabel: "zerg",
                    cwd: "/Users/test/git/zerg",
                    branch: nil,
                    startedAt: "2026-04-11T09:58:00Z"
                ),
                UnmanagedProcessSnapshot(
                    provider: "codex",
                    pid: 55478,
                    workspaceLabel: "myagents",
                    cwd: "/Users/test/git/me/myagents",
                    branch: nil,
                    startedAt: "2026-04-11T09:55:00Z"
                ),
            ],
            launchReadiness: nil
        )

        #expect(snapshot.currentUnmanagedProcesses.count == 2)
        #expect(snapshot.liveUnmanagedSummaryLabel == "2 live")
        #expect(snapshot.liveUnmanagedProviderMixLabel == "Codex 2")
        #expect(snapshot.currentManagedSessions.count == 1)
        #expect(snapshot.recentActivitySummaryLabel == "4 active in 15m")
    }

    @Test
    func decodesManagedDetachedFixture() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/managed-detached.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.attachedManagedCount == 1)
        #expect(snapshot.detachedManagedCount == 1)
        #expect(snapshot.orphanBridgeCount == 0)
        #expect(snapshot.managedNeedsAttention == true)
        #expect(snapshot.managedSummaryLabel == "1 attached · 1 detached")
        #expect(snapshot.currentManagedSessions.count == 2)
    }

    @Test
    func decodesManagedUnknownPhaseFixture() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/managed-unknown-phase.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()
        let session = try #require(snapshot.currentManagedSessions.first)

        #expect(snapshot.healthState == "broken")
        #expect(snapshot.managedAttentionSeverity == .red)
        #expect(session.menuBarAttentionKind == .unknown("unknown phase"))
        #expect(session.rawPhase == "future_magic")
    }

    @Test
    func orphanBridgesPromoteMenuBarAttention() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "managed_summary": {
            "attached_count": 0,
            "detached_count": 0,
            "degraded_count": 0,
            "orphan_bridge_count": 1,
            "latest_activity_at": "2026-04-17T18:35:00Z"
          },
          "orphan_bridges": [
            {
              "provider": "codex",
              "workspace_label": "zerg",
              "status": "orphan",
              "started_at": "2026-04-17T18:20:00Z",
              "heartbeat_at": "2026-04-17T18:34:40Z",
              "reason_codes": ["no_managed_session_bound"]
            }
          ]
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)

        #expect(snapshot.needsMenuBarAttention == true)
        #expect(snapshot.statusItemSummaryLabel.contains("1 orphan bridge"))
        #expect(snapshot.managedAttentionSeverity == .red)
    }

    @Test
    func managedSessionAttentionMatchesSharedPhaseContract() throws {
        for item in try loadManagedPhaseContract() {
            let displayPhase = contractDisplayPhase(for: item)
            let data = Data("""
            {
              "health_state": "healthy",
              "severity": "green",
              "headline": "Longhouse shipping healthy",
              "reasons": [],
              "suggested_actions": [],
              "managed_sessions": [
                {
                  "session_id": "sess-\(item.rawPhase)",
                  "provider": "claude",
                  "workspace_label": "citi",
                  "state": "attached",
                  "phase": "\(displayPhase)",
                  "phase_observed_at": "2026-04-22T01:56:59Z",
                  "last_activity_at": "2026-04-22T01:56:59Z"
                }
              ]
            }
            """.utf8)
            let snapshot = try HealthSnapshotDecoder.decode(data: data)
            let session = try #require(snapshot.managedSessions?.first)
            #expect(session.menuBarAttentionKind == managedAttentionKind(for: item.attention))
        }
    }

    @Test
    func attachedManagedSessionWithoutPhaseDefaultsToIdleAttention() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-missing-phase",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "attached",
            phase: nil,
            phaseObservedAt: nil,
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "ready",
            bridgePid: 95434,
            bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
            reasonCodes: []
        )

        #expect(session.menuBarAttentionKind == .idle)
    }

    @Test
    func attachedManagedSessionWithUnknownPhaseUsesUnknownAttention() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-unknown-phase",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "attached",
            phase: "unknown phase",
            rawPhase: "future_magic",
            phaseObservedAt: "2026-04-22T02:43:47Z",
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "ready",
            bridgePid: 95434,
            bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
            reasonCodes: []
        )

        #expect(session.menuBarAttentionKind == .unknown("unknown phase"))
    }

    @Test
    func managedSessionWithBlankStateUsesGenericUnknownAttention() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-blank-state",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "",
            phase: nil,
            phaseObservedAt: nil,
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "ready",
            bridgePid: 95434,
            bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
            reasonCodes: []
        )

        #expect(session.normalizedState == "unknown")
        #expect(session.menuBarAttentionKind == .unknown("unknown"))
    }

    @Test
    func unknownManagedPhasePromotesManagedAttentionSeverity() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-22T03:00:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse saw an unknown managed phase",
            reasons: ["managed_unknown_phase"],
            suggestedActions: ["Update the managed phase contract before trusting this managed-session status"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            managedSummary: ManagedSummarySnapshot(
                attachedCount: 1,
                detachedCount: 0,
                degradedCount: 0,
                orphanBridgeCount: 0,
                latestActivityAt: "2026-04-22T02:43:47Z"
            ),
            managedSessions: [
                ManagedSessionSnapshot(
                    sessionId: "sess-unknown-phase",
                    provider: "codex",
                    workspaceLabel: "assistants-service",
                    branch: nil,
                    state: "attached",
                    phase: "unknown phase",
                    rawPhase: "future_magic",
                    phaseObservedAt: "2026-04-22T02:43:47Z",
                    lastActivityAt: "2026-04-22T02:43:47Z",
                    bridgeStatus: "ready",
                    bridgePid: 95434,
                    bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
                    reasonCodes: []
                )
            ],
            orphanBridges: [],
            launchReadiness: nil
        )

        #expect(snapshot.managedAttentionSeverity == .red)
        #expect(snapshot.needsMenuBarAttention == true)
    }

    @Test
    @MainActor
    func managedPhaseContractRendersStablePills() throws {
        let outputDirectory = try makeFakeHomeDirectory()
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let updateFixtures = ProcessInfo.processInfo.environment["LONGHOUSE_UPDATE_PHASE_SNAPSHOTS"] == "1"
        let actionSink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)

        for item in try loadManagedPhaseContract() {
            let expectedURL = managedPhaseSnapshotFixtureURL(for: item.rawPhase)
            let actualURL = outputDirectory.appendingPathComponent(expectedURL.lastPathComponent)
            try SnapshotRenderer.renderPNG(
                snapshot: managedPhaseSnapshot(for: item),
                actionSink: actionSink,
                outputURL: actualURL,
                presentationDate: fixedManagedPhasePresentationDate
            )

            let actualData = try Data(contentsOf: actualURL)
            if updateFixtures || !FileManager.default.fileExists(atPath: expectedURL.path) {
                try FileManager.default.createDirectory(
                    at: expectedURL.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try actualData.write(to: expectedURL)
            }

            try assertPNGsRenderIdentically(actualURL: actualURL, expectedURL: expectedURL)
        }
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

    private func loadManagedPhaseContract() throws -> [ManagedPhaseContractCase] {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("../../server/zerg/config/managed_phase_contract.json")
            .standardizedFileURL
        let data = try Data(contentsOf: fixtureURL)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ManagedPhaseContractFile.self, from: data).phases
    }

    private var fixedManagedPhasePresentationDate: Date {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: "2026-04-22T02:09:59Z")!
    }

    private func managedPhaseSnapshotFixtureURL(for rawPhase: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/managed-phase-snapshots/managed-phase-\(rawPhase).png")
    }

    private func managedPhaseSnapshot(for item: ManagedPhaseContractCase) -> HealthSnapshot {
        HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-22T02:09:59Z",
            healthState: "healthy",
            severity: "green",
            headline: "Longhouse shipping healthy",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            managedSummary: ManagedSummarySnapshot(
                attachedCount: 1,
                detachedCount: 0,
                degradedCount: 0,
                orphanBridgeCount: 0,
                latestActivityAt: "2026-04-22T01:56:59Z"
            ),
            managedSessions: [
                ManagedSessionSnapshot(
                    sessionId: "sess-\(item.rawPhase)",
                    provider: "claude",
                    workspaceLabel: "phase-\(item.rawPhase.replacingOccurrences(of: "_", with: "-"))",
                    branch: nil,
                    state: "attached",
                    phase: contractDisplayPhase(for: item),
                    phaseObservedAt: "2026-04-22T01:56:59Z",
                    lastActivityAt: "2026-04-22T01:56:59Z",
                    bridgeStatus: nil,
                    bridgePid: nil,
                    bridgeHeartbeatAt: nil,
                    reasonCodes: nil
                )
            ],
            orphanBridges: [],
            launchReadiness: nil,
            build: BuildIdentitySnapshot(
                installed: BuildIdentityRecord(
                    version: "0.1.15",
                    commit: "d4d4a106fedcba98765432100123456789abcdef",
                    commitShort: "d4d4a106",
                    dirty: false,
                    builtAt: "2026-04-22T01:40:00Z",
                    channel: "dev",
                    error: nil,
                    detail: nil
                ),
                engine: BuildIdentityRecord(
                    version: "0.1.15",
                    commit: "d4d4a106fedcba98765432100123456789abcdef",
                    commitShort: "d4d4a106",
                    dirty: false,
                    builtAt: "2026-04-22T01:40:00Z",
                    channel: "dev",
                    error: nil,
                    detail: nil
                ),
                engineRestartPending: false
            ),
            updateInfo: nil
        )
    }

    private func contractDisplayPhase(for item: ManagedPhaseContractCase) -> String {
        if let format = item.toolDisplayFormat {
            return format.replacingOccurrences(of: "{tool_name}", with: "Bash")
        }
        return item.displayLabel
    }

    private func managedAttentionKind(for raw: String) -> ManagedAttentionKind {
        switch raw {
        case "working":
            return .working
        case "needsYou":
            return .needsYou
        case "blocked":
            return .blocked
        case "idle":
            return .idle
        case "detached":
            return .detached
        case "degraded":
            return .degraded
        default:
            Issue.record("Unknown attention kind in managed phase contract: \(raw)")
            return .unknown(raw)
        }
    }

    private func assertPNGsRenderIdentically(actualURL: URL, expectedURL: URL) throws {
        let actualRep = try decodedPNGRep(from: actualURL)
        let expectedRep = try decodedPNGRep(from: expectedURL)

        #expect(actualRep.pixelsWide == expectedRep.pixelsWide)
        #expect(actualRep.pixelsHigh == expectedRep.pixelsHigh)
        #expect(actualRep.bytesPerRow == expectedRep.bytesPerRow)
        #expect(actualRep.bitsPerPixel == expectedRep.bitsPerPixel)

        let actualData = try bitmapData(from: actualRep, sourceURL: actualURL)
        let expectedData = try bitmapData(from: expectedRep, sourceURL: expectedURL)
        guard actualData != expectedData else {
            return
        }

        let bytesPerPixel = max(actualRep.bitsPerPixel / 8, 1)
        let mismatchOffset = zip(actualData, expectedData)
            .enumerated()
            .first(where: { (_, pair) in pair.0 != pair.1 })?
            .offset ?? 0
        let pixelIndex = mismatchOffset / bytesPerPixel
        let x = pixelIndex % actualRep.pixelsWide
        let y = pixelIndex / actualRep.pixelsWide
        let actualColor = actualRep.colorAt(x: x, y: y)?.description ?? "unknown"
        let expectedColor = expectedRep.colorAt(x: x, y: y)?.description ?? "unknown"
        Issue.record(
            """
            Rendered snapshot pixels differ for \(expectedURL.lastPathComponent) at (\(x), \(y)).
            expected: \(expectedColor)
            actual:   \(actualColor)
            """
        )
    }

    private func decodedPNGRep(from url: URL) throws -> NSBitmapImageRep {
        let data = try Data(contentsOf: url)
        return try #require(
            NSBitmapImageRep(data: data),
            "Failed to decode PNG at \(url.path)"
        )
    }

    private func bitmapData(from rep: NSBitmapImageRep, sourceURL: URL) throws -> Data {
        guard let bitmapData = rep.bitmapData else {
            throw SnapshotComparisonError.missingBitmapData(sourceURL.path)
        }
        return Data(bytes: bitmapData, count: rep.bytesPerRow * rep.pixelsHigh)
    }

    private enum SnapshotComparisonError: Error {
        case missingBitmapData(String)
    }
}
