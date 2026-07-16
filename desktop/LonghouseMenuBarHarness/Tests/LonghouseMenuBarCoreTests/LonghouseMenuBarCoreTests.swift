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

private actor ChangeCounter {
    private(set) var value = 0
    func increment() { value += 1 }
}

private final class CountingHealthSnapshotSource: HealthSnapshotSource, @unchecked Sendable {
    private let lock = NSLock()
    private var snapshots: [HealthSnapshot]
    private var index = 0
    private var count = 0

    init(snapshots: [HealthSnapshot]) {
        self.snapshots = snapshots
    }

    var loadCount: Int {
        lock.withLock {
            count
        }
    }

    func load() throws -> HealthSnapshot {
        lock.withLock {
            count += 1
            let snapshot = snapshots[min(index, snapshots.count - 1)]
            index += 1
            return snapshot
        }
    }
}

private func watchingAttentionSnapshot() -> HealthSnapshot {
    HealthSnapshot(
        schemaVersion: 1,
        collectedAt: "2026-04-08T01:52:00Z",
        healthState: "degraded",
        severity: "yellow",
        headline: "Longhouse local status is aging",
        reasons: ["engine_status_stale"],
        suggestedActions: [],
        attention: AttentionSnapshot(
            state: "watching",
            headline: "Longhouse is retrying quietly",
            summary: "Recent local shipping retries are recorded in diagnostics, but there is no durable backlog or repair step yet.",
            reasons: ["engine_status_stale"],
            suggestedActions: []
        ),
        service: ServiceSnapshot(
            platform: "macos",
            status: "running",
            serviceName: "com.longhouse.shipper",
            serviceFile: nil,
            logPath: nil
        ),
        engineStatus: EngineStatusSnapshot(
            path: nil,
            exists: true,
            fresh: false,
            ageSeconds: 3600,
            payload: EngineStatusPayload(
                version: "0.1.16",
                daemonPid: 123,
                lastShipAt: "2026-04-07T01:51:00Z",
                spoolPendingCount: 0,
                spoolDeadCount: 0,
                parseErrorCount1H: 0,
                consecutiveShipFailures: 0,
                diskFreeBytes: nil,
                isOffline: false,
                recentDeadLetters: nil,
                lastUpdated: "2026-04-07T01:52:00Z"
            ),
            error: nil
        ),
        outbox: OutboxSnapshot(path: nil, fileCount: 0, oldestAgeSeconds: nil),
        activitySummary: nil,
        launchReadiness: nil
    )
}

struct LonghouseMenuBarCoreTests {
    @Test
    func archiveScanningStaysBackgroundAndDoesNotPromoteAttention() {
        let snapshot = presentationSnapshot(
            sessions: [presentationSession(phase: "running tools")],
            archive: ArchiveBacklogStatus(
                state: "scanning", mode: "trickle", pendingRanges: 2,
                pendingPaths: 2, pendingSessions: 2, pendingBytes: 1_503_238_554,
                deadRanges: 0, deadBytes: 0
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.headline == "1 session active")
        #expect(presentation.backgroundActivity == "Archive projection scanning 1.4 GB · 2 ranges")
        #expect(!presentation.needsStatusItemBadge)
    }

    @Test
    func sessionInputPromotesBlueNeedsUserState() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "needs permission")])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .needsUser)
        #expect(presentation.headline == "1 session needs you")
        #expect(presentation.needsStatusItemBadge)
    }

    @Test
    func externalBlockIsVisibleWithoutClaimingUserAction() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "blocked on network")])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.subheadline.contains("1 blocked"))
        #expect(!presentation.headline.contains("needs you"))
    }

    @Test
    func unknownPhaseIsVisibleWithoutGlobalFailure() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "future provider phase")])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.subheadline.contains("1 phase unavailable"))
        #expect(!presentation.needsStatusItemBadge)
    }

    @Test
    func durableConflictOutranksSessionInput() {
        let snapshot = presentationSnapshot(
            reasons: ["storage_v2_sources_blocked"],
            sessions: [presentationSession(phase: "needs permission")],
            storageBlocked: 1
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
        #expect(presentation.headline == "Durable upload blocked for 1 source")
    }

    @Test
    func youngImmutablePendingWorkStaysNormal() {
        let snapshot = presentationSnapshot(sessions: [], storagePending: 2)

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "2 pending")
    }

    @Test
    func transientOfflineRetryPreservesNormalHeadlineAndNamesTransportFact() {
        let snapshot = presentationSnapshot(reasons: ["reported_offline"], sessions: [], isOffline: true)

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Offline")
    }

    @Test
    func staleStatusPromotesUnknownInsteadOfRepair() {
        let snapshot = presentationSnapshot(reasons: ["engine_status_stale"], sessions: [], engineFresh: false)

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .unavailable)
        #expect(presentation.headline == "Current local status unavailable")
    }

    @Test
    func stoppedAgentWithRetainedWorkPromotesRepair() {
        let snapshot = presentationSnapshot(
            reasons: ["service_stopped"], sessions: [], storagePending: 1,
            serviceStatus: "stopped"
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
        #expect(presentation.facts.first(where: { $0.id == "local-agent" })?.value == "Stopped")
    }

    @Test
    func archiveDeadLettersAreInspectableNotRepair() {
        let snapshot = presentationSnapshot(reasons: ["archive_dead_lettered"], sessions: [])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Historical archive needs review")
    }

    @Test
    func archivePendingWhileIdleDoesNotBadge() {
        let snapshot = presentationSnapshot(
            reasons: ["archive_backlog_pending"], sessions: [],
            archive: ArchiveBacklogStatus(
                state: "blocked", mode: "trickle", pendingRanges: 2,
                pendingPaths: 2, pendingSessions: 2, pendingBytes: 4096,
                deadRanges: 0, deadBytes: 0
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(presentation.backgroundActivity?.contains("2 ranges") == true)
        #expect(!presentation.needsStatusItemBadge)
    }

    @Test
    func localRefreshPreservesRealtimeTitleProjection() {
        let titled = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "zerg",
            timelineTitle: "Fix wake recovery", summaryTitle: "Fix wake recovery",
            firstUserMessage: "Fix the menu bar after wake", titleState: "ready",
            titleSource: "prompt", titleProvenance: "remote_sse", branch: "main",
            state: "attached", phase: "idle", lastActivityAt: nil,
            bridgeStatus: "ready", bridgePid: 42, bridgeHeartbeatAt: nil,
            reasonCodes: []
        )
        let localOnly = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "zerg",
            branch: "main", state: "attached", phase: "thinking", lastActivityAt: nil,
            bridgeStatus: "ready", bridgePid: 42, bridgeHeartbeatAt: nil,
            reasonCodes: []
        )
        let previous = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-14T17:00:00Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [titled],
            launchReadiness: nil
        )
        let refreshed = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-14T17:00:01Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [localOnly],
            launchReadiness: nil
        ).preservingSessionTitles(from: previous)

        #expect(refreshed.managedSessions?.first?.timelineTitle == "Fix wake recovery")
        #expect(refreshed.managedSessions?.first?.titleProvenance == "remote_sse")
        #expect(refreshed.managedSessions?.first?.phase == "thinking")
    }

    @Test
    func realtimeProjectionUpdatesTitleAndPhaseWithoutReloadingSnapshot() {
        let session = ManagedSessionSnapshot(
            sessionId: "session-1",
            provider: "codex",
            workspaceLabel: "zerg",
            timelineTitle: "Naming session…",
            branch: "main",
            state: "attached",
            phase: "idle",
            lastActivityAt: nil,
            bridgeStatus: nil,
            bridgePid: nil,
            bridgeHeartbeatAt: nil,
            reasonCodes: []
        )
        let updated = session.applying(
            SessionProjection(
                sessionId: "session-1",
                timelineTitle: "Make titles immediate",
                summaryTitle: "Make titles immediate",
                firstUserMessage: "Make titles immediate please",
                titleState: "ready",
                titleSource: "prompt",
                runtimePhase: "thinking",
                displayPhase: "Thinking",
                lastActivityAt: "2026-07-14T05:00:00Z",
                source: "runtime_host"
            )
        )

        #expect(updated.resolvedTitleText == "Make titles immediate")
        #expect(updated.phase == "idle")
        #expect(updated.titleSource == "prompt")
        #expect(updated.titleProvenance == "runtime_host")
        #expect(updated.phaseProvenance == "machine_agent")
    }

    @Test
    func localStatusMonitorIgnoresPulseOnlyWritesAndWakesForReconciliationOrSessionChanges() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-status-monitor-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let statusURL = directory.appendingPathComponent("engine-status.json")
        try Data(#"{"last_updated":"one","sessions_sequence":1,"local_projection":{"version":1,"engine_pulse_at":"one","reconciliation":{"state":"idle"}}}"#.utf8).write(to: statusURL, options: .atomic)
        let changed = ChangeCounter()
        let monitor = LocalStatusMonitor(statusPath: statusURL.path) { _ in
            Task { await changed.increment() }
        }
        monitor.start()
        try await Task.sleep(for: .milliseconds(75))

        try Data(#"{"last_updated":"two","sessions_sequence":1,"local_projection":{"version":1,"engine_pulse_at":"two","reconciliation":{"state":"idle"}}}"#.utf8).write(to: statusURL, options: .atomic)
        try await Task.sleep(for: .milliseconds(100))
        #expect(await changed.value == 0)

        try Data(#"{"last_updated":"three","sessions_sequence":1,"local_projection":{"version":1,"engine_pulse_at":"three","reconciliation":{"state":"reconciling","reason":"wake"}}}"#.utf8).write(to: statusURL, options: .atomic)
        try await Task.sleep(for: .milliseconds(150))
        #expect(await changed.value == 1)

        try Data(#"{"last_updated":"four","sessions_sequence":2,"local_projection":{"version":2,"engine_pulse_at":"four","reconciliation":{"state":"idle"}}}"#.utf8).write(to: statusURL, options: .atomic)
        try await Task.sleep(for: .milliseconds(150))
        monitor.stop()
        #expect(await changed.value == 2)
    }

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

        #expect(config.refreshIntervalSeconds == nil)
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
    func spyActionSinkBuildsManagedSessionURLs() throws {
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

        #expect(
            sink.resolveLonghouseURL(snapshot: snapshot, sessionID: "session-123")?.absoluteString ==
                "https://demo.longhouse.test/timeline/session-123"
        )

        let feedback = sink.handleOpenManagedSession(sessionID: "session-123", title: "Menu title", snapshot: snapshot)
        #expect(feedback?.title == "Open session dry run recorded")
        #expect(feedback?.detail.contains("Menu title") == true)
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
            provider: "codex",
            workspaceLabel: "zerg",
            snapshot: snapshot
        )

        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Stop dry run recorded")
        #expect(feedback?.detail.contains("zerg") == true)
    }

    @Test
    func stopCommandDescriptionRoutesByProvider() throws {
        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)

        let opencode = sink.stopCommandDescriptionForTesting(sessionID: "s1", provider: "opencode")
        #expect(opencode == "longhouse opencode-channel stop --session-id s1")

        let cursor = sink.stopCommandDescriptionForTesting(sessionID: "s1b", provider: "cursor")
        #expect(cursor == "longhouse-engine cursor-helm stop --session-id s1b")

        let codex = sink.stopCommandDescriptionForTesting(sessionID: "s2", provider: "codex")
        #expect(codex == "longhouse-engine codex-bridge stop --session-id s2")

        // Unknown/nil provider falls back to the engine codex-bridge path.
        let unknown = sink.stopCommandDescriptionForTesting(sessionID: "s3", provider: nil)
        #expect(unknown == "longhouse-engine codex-bridge stop --session-id s3")
    }

    @Test
    func stopTransportRoutesKnownProvidersAndRejectsUnsupported() throws {
        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        #expect(sink.stopTransportForTesting(provider: "opencode") == "opencode")
        #expect(sink.stopTransportForTesting(provider: "cursor") == "cursor")
        #expect(sink.stopTransportForTesting(provider: "codex") == "codex")
        // Legacy rows with no provider stay on the codex bridge path.
        #expect(sink.stopTransportForTesting(provider: nil) == "codex")
        #expect(sink.stopTransportForTesting(provider: "") == "codex")
        // Claude / Antigravity must NOT silently run codex-bridge stop.
        #expect(sink.stopTransportForTesting(provider: "claude") == "unsupported")
        #expect(sink.stopTransportForTesting(provider: "antigravity") == "unsupported")
    }

    @Test
    func bulkStopManagedBridgesDryRunDeduplicatesTargets() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Managed sessions are running in background",
            reasons: ["managed_session_detached"],
            suggestedActions: ["Reattach or stop detached managed sessions from Longhouse.app"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handleStopManagedBridges(
            targets: [
                ManagedStopTarget(sessionID: " sess-a ", provider: "codex"),
                ManagedStopTarget(sessionID: "", provider: "codex"),
                ManagedStopTarget(sessionID: "sess-b", provider: "opencode"),
                ManagedStopTarget(sessionID: "sess-a", provider: "codex"),
            ],
            label: "background managed sessions",
            snapshot: snapshot
        )

        #expect(feedback?.style == .warning)
        #expect(feedback?.title == "Bulk stop dry run recorded")
        #expect(feedback?.detail.contains("2 background managed sessions") == true)
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
    func cliSourceLoadsLargeSnapshotWithoutPipeDeadlock() throws {
        let python = "/usr/bin/python3"
        guard FileManager.default.isExecutableFile(atPath: python) else {
            return
        }

        let code = """
        import json
        print(json.dumps({
            "schema_version": 1,
            "collected_at": "2026-05-05T12:00:00Z",
            "health_state": "healthy",
            "severity": "green",
            "headline": "Longhouse shipping healthy",
            "reasons": ["x" * 200000],
            "suggested_actions": []
        }))
        """
        let source = CLIHealthSnapshotSource(launchPath: python, arguments: ["-c", code])

        let snapshot = try source.load()

        #expect(snapshot.headline == "Longhouse shipping healthy")
        #expect(snapshot.reasons.first?.count == 200000)
    }

    @Test
    func cliSourceTimesOutHungCommand() throws {
        let source = CLIHealthSnapshotSource(
            launchPath: "/bin/zsh",
            arguments: ["-lc", "sleep 3"],
            commandTimeoutSeconds: 0.1
        )

        #expect(throws: SnapshotSourceError.self) {
            _ = try source.load()
        }
    }

    @Test
    @MainActor
    func snapshotStoreRetriesWakeTimeoutAsRecovery() async throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let marker = tempDir.appendingPathComponent("attempted")
        let payload = tempDir.appendingPathComponent("health.json")
        try #"{"health_state":"healthy","severity":"green","headline":"Recovered","reasons":[],"suggested_actions":[]}"#
            .write(to: payload, atomically: true, encoding: .utf8)
        let command = """
        if [[ ! -e \(marker.path) ]]; then
          touch \(marker.path)
          sleep 1
        else
          cat \(payload.path)
        fi
        """
        let source = CLIHealthSnapshotSource(
            launchPath: "/bin/zsh",
            arguments: ["-lc", command],
            commandTimeoutSeconds: 0.05
        )
        let store = SnapshotStore(
            source: source,
            cacheURL: tempDir.appendingPathComponent("last-good.json"),
            transientRetryDelay: 0.02
        )

        for _ in 0..<200 where store.snapshot == nil {
            try? await Task.sleep(for: .milliseconds(10))
        }
        #expect(FileManager.default.fileExists(atPath: marker.path))
        #expect(store.snapshot?.headline == "Recovered")
        #expect(!store.isRecovering)
        #expect(store.loadError == nil)
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
    func snapshotStoreSkipsPresentationRefreshWhenSnapshotIsFresh() async throws {
        let collectedAt = "2026-05-05T12:00:00Z"
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: collectedAt,
            healthState: "healthy",
            severity: "green",
            headline: "Fresh enough Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let source = CountingHealthSnapshotSource(snapshots: [snapshot])
        let store = SnapshotStore(source: source, cacheURL: nil)
        let referenceDate = try #require(HealthSnapshot.parseISO8601("2026-05-05T12:00:05Z"))

        store.refreshForPresentation(maxSnapshotAge: 10, referenceDate: referenceDate)
        try? await Task.sleep(for: .milliseconds(50))

        #expect(source.loadCount == 1)
        #expect(store.snapshot?.headline == "Fresh enough Longhouse status")
    }

    @Test
    @MainActor
    func snapshotStoreRefreshesForPresentationWhenSnapshotIsStale() async throws {
        let oldSnapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:00Z",
            healthState: "healthy",
            severity: "green",
            headline: "Old Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let refreshedSnapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-05-05T12:00:12Z",
            healthState: "healthy",
            severity: "green",
            headline: "Refreshed Longhouse status",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let source = CountingHealthSnapshotSource(snapshots: [oldSnapshot, refreshedSnapshot])
        let store = SnapshotStore(source: source, cacheURL: nil)
        let referenceDate = try #require(HealthSnapshot.parseISO8601("2026-05-05T12:00:12Z"))

        store.refreshForPresentation(maxSnapshotAge: 10, referenceDate: referenceDate)
        for _ in 0..<40 {
            if store.snapshot?.headline == "Refreshed Longhouse status" {
                break
            }
            try? await Task.sleep(for: .milliseconds(50))
        }

        #expect(source.loadCount == 2)
        #expect(store.snapshot?.headline == "Refreshed Longhouse status")
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
    func legacyYellowAndRedSnapshotsRequestMenuBarAttentionWhenAttentionIsAbsent() {
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
    func watchingAttentionSuppressesMenuBarAttention() {
        let snapshot = watchingAttentionSnapshot()

        #expect(snapshot.needsMenuBarAttention == false)
        #expect(snapshot.menuBarAttentionSeverity == nil)
        #expect(snapshot.effectiveHeadline == "Longhouse is retrying quietly")
        #expect(snapshot.attentionSummaryLabel.contains("no durable backlog"))
    }

    @Test
    func needsAttentionStateRequestsMenuBarAttention() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse shipping is degraded",
            reasons: ["outbox_stuck"],
            suggestedActions: ["Inspect logs"],
            attention: AttentionSnapshot(state: "needs_attention"),
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        #expect(snapshot.needsMenuBarAttention == true)
        #expect(snapshot.menuBarAttentionSeverity == .yellow)
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
            attention: AttentionSnapshot(
                state: "needs_attention",
                summary: "Longhouse is still running, but this state is persistent or actionable enough to inspect."
            ),
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
    func attentionSummaryNamesBlockedArchiveRetryAndError() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse archive repair is draining",
            reasons: ["archive_repair_draining"],
            suggestedActions: ["Inspect archive backlog: longhouse archive status"],
            attention: AttentionSnapshot(
                state: "watching",
                summary: nil
            ),
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
                    spoolPendingCount: 6375,
                    spoolDeadCount: 0,
                    archiveBacklog: ArchiveBacklogStatus(
                        state: "draining",
                        mode: "drain",
                        pendingRanges: 6375,
                        pendingPaths: 6374,
                        pendingSessions: 6306,
                        pendingBytes: 16_699_227_012,
                        deadRanges: 0,
                        deadBytes: 0,
                        maxRetryCount: 3,
                        latestError: "storage lane busy"
                    ),
                    parseErrorCount1H: 0,
                    consecutiveShipFailures: 0,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: "2026-04-08T01:52:00Z"
                ),
                error: nil
            ),
            outbox: OutboxSnapshot(path: nil, fileCount: 0, oldestAgeSeconds: nil),
            activitySummary: nil,
            launchReadiness: nil
        )

        #expect(snapshot.attentionSummaryLabel.contains("6375 transcript ranges blocked after 3 failed attempts"))
        #expect(snapshot.attentionSummaryLabel.contains("Last error: storage lane busy"))

        let drained = snapshot.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: EngineStatusPayload(
                    version: "0.1.16",
                    daemonPid: 123,
                    lastShipAt: "2026-04-08T01:52:01Z",
                    spoolPendingCount: 0,
                    spoolDeadCount: 0,
                    archiveBacklog: ArchiveBacklogStatus(
                        state: "idle", mode: "drain", pendingRanges: 0, pendingPaths: 0,
                        pendingSessions: 0, pendingBytes: 0, deadRanges: 0, deadBytes: 0,
                        maxRetryCount: 0, latestError: nil
                    ),
                    parseErrorCount1H: 0,
                    consecutiveShipFailures: 0,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: "2026-04-08T01:52:01Z"
                )
            )
        )
        #expect(!drained.attentionSummaryLabel.contains("transcript range"))
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
            attention: AttentionSnapshot(
                state: "needs_attention",
                summary: "Longhouse is still running, but this state is persistent or actionable enough to inspect."
            ),
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
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .inspect)
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
    func decodesRecentTouchesAndRecentCounts() throws {
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
        #expect(snapshot.recentActivitySummaryLabel == "4 active in 15m")
        #expect(snapshot.recentTouches.count == 2)
        #expect(snapshot.recentTouches.first?.provider == "claude")
        #expect(snapshot.recentTouches.first?.workspaceLabel == "zerg")
        #expect(snapshot.recentTouches.first?.lastUpdated == "2026-04-11T10:00:00Z")
        #expect(snapshot.recentTouchTitle(snapshot.recentTouches[0]) == "zerg · Claude")
        #expect(snapshot.recentTouchTitle(snapshot.recentTouches[1]) == "crims · Codex")
    }

    @Test
    func decodesAttentionAndKeepsLegacyFallbackWhenAbsent() throws {
        let watchingData = Data("""
        {
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Longhouse shipping is degraded",
          "reasons": ["consecutive_failures"],
          "suggested_actions": [],
          "attention": {
            "state": "watching",
            "headline": "Longhouse is retrying quietly",
            "summary": "Recent local shipping retries are recorded in diagnostics, but there is no durable backlog or repair step yet.",
            "reasons": ["consecutive_failures"],
            "suggested_actions": []
          }
        }
        """.utf8)
        let legacyData = Data("""
        {
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Longhouse shipping is degraded",
          "reasons": ["consecutive_failures"],
          "suggested_actions": []
        }
        """.utf8)

        let watching = try HealthSnapshotDecoder.decode(data: watchingData)
        let legacy = try HealthSnapshotDecoder.decode(data: legacyData)

        #expect(watching.attention?.normalizedState == "watching")
        #expect(watching.needsMenuBarAttention == false)
        #expect(watching.effectiveHeadline == "Longhouse is retrying quietly")
        #expect(legacy.attention == nil)
        #expect(legacy.needsMenuBarAttention == true)
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
                        workspaceLabel: "acme",
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
                    workspaceLabel: "project",
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
    func managedUIPresenceSeparatesBackgroundFromRuntimeAttached() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "managed_summary": {
            "attached_count": 2,
            "detached_count": 0,
            "degraded_count": 0,
            "orphan_bridge_count": 0,
            "latest_activity_at": "2026-05-13T23:59:39Z"
          },
          "managed_sessions": [
            {
              "session_id": "sess-terminal",
              "provider": "codex",
              "workspace_label": "zerg",
              "state": "attached",
              "phase": "idle",
              "last_activity_at": "2026-05-13T23:59:39Z",
              "launch_mode": "tui",
              "ui_attached": true,
              "ui_presence": "foreground_tui"
            },
            {
              "session_id": "sess-background",
              "provider": "codex",
              "workspace_label": "zerg",
              "state": "attached",
              "phase": "idle",
              "last_activity_at": "2026-05-13T23:59:39Z",
              "launch_mode": "detached_ui",
              "ui_attached": false,
              "ui_presence": "background"
            }
          ]
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)
        let terminal = try #require(snapshot.currentManagedSessions.first)
        let background = try #require(snapshot.currentManagedSessions.last)

        #expect(snapshot.attachedManagedCount == 2)
        #expect(snapshot.foregroundManagedCount == 1)
        #expect(snapshot.backgroundManagedCount == 1)
        #expect(snapshot.managedSummaryLabel == "1 terminal · 1 background")
        #expect(terminal.launchMode == "tui")
        #expect(terminal.uiAttached == true)
        #expect(terminal.normalizedUIPresence == "foreground_tui")
        #expect(terminal.isConsoleManagedSession == false)
        #expect(terminal.needsManagedSessionAttention == false)
        #expect(terminal.isBackgroundManagedSession == false)
        #expect(terminal.canStopFromMenuBar == false)
        #expect(background.launchMode == "detached_ui")
        #expect(background.uiAttached == false)
        #expect(background.normalizedUIPresence == "background")
        #expect(background.isConsoleManagedSession == true)
        #expect(background.needsManagedSessionAttention == false)
        #expect(background.isBackgroundManagedSession == true)
        #expect(background.canStopFromMenuBar == true)
    }

    @Test
    func decodesManagedSessionTitles() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "managed_sessions": [
            {
              "session_id": "sess-title",
              "provider": "codex",
              "workspace_label": "zerg",
              "timeline_title": "Fix menu bar links",
              "summary_title": "Fix menu bar links",
              "first_user_message": "Can we open rows?",
              "title_state": "degraded",
              "state": "attached",
              "phase": "idle",
              "last_activity_at": "2026-05-13T23:59:39Z"
            }
          ]
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)
        let session = try #require(snapshot.currentManagedSessions.first)

        #expect(session.timelineTitle == "Fix menu bar links")
        #expect(session.summaryTitle == "Fix menu bar links")
        #expect(session.firstUserMessage == "Can we open rows?")
        #expect(session.resolvedTitleText == "Fix menu bar links")
    }

    @Test
    func managedSessionTitleFallbackDoesNotDependOnEnrichmentState() {
        let session = ManagedSessionSnapshot(
            sessionId: "session-title-fallback",
            provider: "codex",
            workspaceLabel: "zerg",
            timelineTitle: nil,
            summaryTitle: nil,
            firstUserMessage: "Repair storage title projection",
            titleState: "degraded",
            branch: nil,
            state: "attached",
            phase: "idle",
            lastActivityAt: nil,
            bridgeStatus: nil,
            bridgePid: nil,
            bridgeHeartbeatAt: nil,
            reasonCodes: []
        )

        #expect(session.resolvedTitleText == "Repair storage title projection")
    }

    @Test
    func cursorManagedSessionProjectsForegroundPresenceAndStopTransport() throws {
        let data = Data("""
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "managed_sessions": [
            {
              "session_id": "sess-cursor",
              "provider": "cursor",
              "workspace_label": "zerg",
              "timeline_title": "Fix Reattach Row Alignment",
              "state": "attached",
              "phase": "idle",
              "last_activity_at": "2026-07-09T00:49:49Z",
              "launch_mode": "tui",
              "ui_attached": true,
              "ui_presence": "foreground_tui"
            }
          ]
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)
        let session = try #require(snapshot.currentManagedSessions.first)

        #expect(session.provider == "cursor")
        #expect(session.workspaceLabel == "zerg")
        #expect(session.timelineTitle == "Fix Reattach Row Alignment")
        #expect(session.normalizedUIPresence == "foreground_tui")
        #expect(session.isConsoleManagedSession == false)
        #expect(session.needsManagedSessionAttention == false)
        #expect(HealthSnapshot.providerDisplayName("cursor") == "Cursor")
    }

    @Test
    func orphanBridgeFixtureCarriesStoppableCodexSessionIDs() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/orphan-bridges.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.orphanBridgeCount == 2)
        #expect(snapshot.currentOrphanBridges.allSatisfy { bridge in
            bridge.provider == "codex" && bridge.sessionId?.isEmpty == false
        })
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
        #expect(snapshot.managedAttentionSeverity == nil)
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .normal)
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
                  "workspace_label": "acme",
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
        #expect(session.normalizedUIPresence == nil)
        #expect(session.isConsoleManagedSession == false)
        #expect(session.needsManagedSessionAttention == false)
        #expect(session.canStopFromMenuBar == false)
    }

    @Test
    func consoleManagedSessionIsHealthyRemotePresenceNotAttention() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-console",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "attached",
            phase: "idle",
            phaseObservedAt: "2026-04-22T02:43:47Z",
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "ready",
            bridgePid: 95434,
            bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
            launchMode: "detached_ui",
            uiAttached: false,
            uiPresence: "background",
            reasonCodes: []
        )

        #expect(session.isConsoleManagedSession == true)
        #expect(session.needsManagedSessionAttention == false)
        #expect(session.isBackgroundManagedSession == true)
        #expect(session.menuBarAttentionKind == .idle)
    }

    @Test
    func backgroundPresenceStaysConsoleEvenWithUnknownState() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-future-console",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "future_state",
            phase: nil,
            phaseObservedAt: "2026-04-22T02:43:47Z",
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "ready",
            bridgePid: 95434,
            bridgeHeartbeatAt: "2026-04-22T02:43:47Z",
            launchMode: "detached_ui",
            uiAttached: false,
            uiPresence: "background",
            reasonCodes: []
        )

        #expect(session.isConsoleManagedSession == true)
        #expect(session.needsManagedSessionAttention == false)
        #expect(session.menuBarAttentionKind == .unknown("future_state"))
    }

    @Test
    func detachedManagedSessionNeedsAttentionEvenWithoutBackgroundPresence() {
        let session = ManagedSessionSnapshot(
            sessionId: "sess-detached",
            provider: "codex",
            workspaceLabel: "assistants-service",
            branch: nil,
            state: "detached",
            phase: "idle",
            phaseObservedAt: "2026-04-22T02:43:47Z",
            lastActivityAt: "2026-04-22T02:43:47Z",
            bridgeStatus: "missing",
            bridgePid: nil,
            bridgeHeartbeatAt: nil,
            launchMode: "detached_ui",
            uiAttached: false,
            uiPresence: nil,
            reasonCodes: ["bridge_missing"]
        )

        #expect(session.isConsoleManagedSession == false)
        #expect(session.needsManagedSessionAttention == true)
        #expect(session.isBackgroundManagedSession == true)
        #expect(session.menuBarAttentionKind == .detached)
        #expect(session.canStopFromMenuBar == true)
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
    func unknownManagedPhaseStaysRowLevelInformation() {
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

        #expect(snapshot.managedAttentionSeverity == nil)
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .normal)
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
                    timelineTitle: "phase-\(item.rawPhase.replacingOccurrences(of: "_", with: "-"))",
                    titleState: "ready",
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

private func presentationSession(phase: String) -> ManagedSessionSnapshot {
    ManagedSessionSnapshot(
        sessionId: UUID().uuidString, provider: "codex", workspaceLabel: "longhouse",
        timelineTitle: "Review menu bar state", branch: "main", state: "attached",
        phase: phase, lastActivityAt: "1970-01-01T00:00:00Z", bridgeStatus: "ready",
        bridgePid: 42, bridgeHeartbeatAt: "1970-01-01T00:00:00Z", reasonCodes: []
    )
}

private func presentationSnapshot(
    reasons: [String] = [],
    sessions: [ManagedSessionSnapshot],
    archive: ArchiveBacklogStatus? = nil,
    storageBlocked: Int = 0,
    storagePending: Int = 0,
    isOffline: Bool = false,
    engineFresh: Bool = true,
    serviceStatus: String = "running"
) -> HealthSnapshot {
    HealthSnapshot(
        schemaVersion: 1, collectedAt: "1970-01-01T00:00:00Z",
        healthState: "healthy", severity: "green", headline: "Healthy",
        reasons: reasons, suggestedActions: [],
        service: ServiceSnapshot(
            platform: "macos", status: serviceStatus, serviceName: "com.longhouse.shipper",
            serviceFile: nil, logPath: nil
        ),
        engineStatus: EngineStatusSnapshot(
            path: nil, exists: true, fresh: engineFresh, ageSeconds: engineFresh ? 1 : 600,
            payload: EngineStatusPayload(
                version: "test", daemonPid: 1, lastShipAt: "1970-01-01T00:00:00Z",
                spoolPendingCount: 0, spoolDeadCount: 0, archiveBacklog: archive,
                storageV2Outbox: StorageV2OutboxStatus(
                    pendingCount: storagePending, pendingBytes: 0, blockedSourceCount: storageBlocked,
                    blockedBytes: 0, latestBlockKind: nil, latestBlockDetail: nil,
                    byteLimit: 1_073_741_824, error: nil
                ),
                parseErrorCount1H: 0, consecutiveShipFailures: 0, diskFreeBytes: nil,
                isOffline: isOffline, recentDeadLetters: [], lastUpdated: "1970-01-01T00:00:00Z"
            ),
            error: nil
        ),
        outbox: OutboxSnapshot(path: nil, fileCount: 0, oldestAgeSeconds: nil),
        activitySummary: nil, managedSessions: sessions, launchReadiness: nil
    )
}
