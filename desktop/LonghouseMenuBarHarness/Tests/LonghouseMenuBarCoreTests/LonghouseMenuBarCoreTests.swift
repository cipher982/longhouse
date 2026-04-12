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
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: logURL, uiURL: nil, effectMode: .logOnly)
        _ = sink.handle(.refresh, snapshot: snapshot)

        let content = try String(contentsOf: logURL)
        #expect(content.contains("\"action\":\"refresh\""))
        #expect(content.contains("Longhouse shipping healthy"))
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
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.repairInstall, snapshot: snapshot)

        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Repair dry run recorded")
        #expect(feedback?.detail.contains("without changing your machine") == true)
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
}
