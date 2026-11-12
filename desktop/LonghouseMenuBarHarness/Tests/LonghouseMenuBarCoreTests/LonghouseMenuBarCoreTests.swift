import AppKit
import Darwin
import Dispatch
import Combine
import Foundation
import Testing

@testable import LonghouseMenuBarCore

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

/// Succeeds until `startFailing()`, then fails on every subsequent load.
private final class FlakyHealthSnapshotSource: HealthSnapshotSource, @unchecked Sendable {
    private let lock = NSLock()
    private let snapshot: HealthSnapshot
    private var failing = false

    init(snapshot: HealthSnapshot) {
        self.snapshot = snapshot
    }

    var describedCommand: String? { "flaky-test-source" }

    func startFailing() {
        lock.lock()
        defer { lock.unlock() }
        failing = true
    }

    func load() throws -> HealthSnapshot {
        lock.lock()
        let isFailing = failing
        lock.unlock()
        if isFailing {
            throw SnapshotSourceError.commandFailed("producer stopped responding")
        }
        return snapshot
    }
}

private actor ChangeCounter {
    private(set) var value = 0
    private(set) var presentationChanges: [Bool] = []
    private(set) var refreshRequirements: [Bool] = []

    func increment(presentationChanged: Bool? = nil, requiresSnapshotRefresh: Bool? = nil) {
        value += 1
        if let presentationChanged {
            self.presentationChanges.append(presentationChanged)
        }
        if let requiresSnapshotRefresh {
            self.refreshRequirements.append(requiresSnapshotRefresh)
        }
    }
    func waitForChange(after previousValue: Int, timeout: Duration = .seconds(3)) async throws -> Bool {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while value <= previousValue, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        return value > previousValue
    }
}

private final class CountingHealthSnapshotSource: HealthSnapshotSource, @unchecked Sendable {
    private let lock = NSLock()
    private var snapshots: [HealthSnapshot]
    private var index = 0
    private var count = 0
    let supportsLiveMonitoring: Bool

    init(snapshots: [HealthSnapshot], supportsLiveMonitoring: Bool = true) {
        self.snapshots = snapshots
        self.supportsLiveMonitoring = supportsLiveMonitoring
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

private final class DirectoryWriteCounter: @unchecked Sendable {
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "ai.longhouse.menu-bar.test-cache-writes")
    private let source: DispatchSourceFileSystemObject
    private var writes = 0
    private var debounce: DispatchWorkItem?

    init(directoryURL: URL) throws {
        let descriptor = open(directoryURL.path, O_EVTONLY)
        guard descriptor >= 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor,
            eventMask: [.write],
            queue: queue
        )
        self.source = source
        source.setEventHandler { [weak self] in
            guard let self else { return }
            self.debounce?.cancel()
            let item = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.lock.withLock { self.writes += 1 }
            }
            self.debounce = item
            self.queue.asyncAfter(deadline: .now() + .milliseconds(25), execute: item)
        }
        source.setCancelHandler { _ = close(descriptor) }
        source.resume()
    }

    var value: Int {
        lock.withLock { writes }
    }

    func stop() {
        source.cancel()
    }

    deinit {
        source.cancel()
    }
}

private final class StalledStreamRequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var requests = 0

    func increment() {
        lock.withLock { requests += 1 }
    }

    var value: Int {
        lock.withLock { requests }
    }
}

private final class StalledStreamURLProtocol: URLProtocol, @unchecked Sendable {
    static let requestCounter = StalledStreamRequestCounter()

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.scheme == "stalled"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.requestCounter.increment()
        guard let client, let url = request.url else { return }
        let response = HTTPURLResponse(
            url: url,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "text/event-stream"]
        )!
        client.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        // Send one complete server frame, then leave the body open forever.
        client.urlProtocol(self, didLoad: Data(": heartbeat\n".utf8))
    }

    override func stopLoading() {}
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
    func memoizedDateParsingMatchesUncachedFractionalPlainAndInvalidInputs() {
        func parseWithoutMemo(_ value: String) -> Date? {
            let fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let plain = ISO8601DateFormatter()
            plain.formatOptions = [.withInternetDateTime]
            return fractional.date(from: value) ?? plain.date(from: value)
        }

        for value in [
            "2026-10-12T16:20:30.125Z",
            "2026-10-12T16:20:30Z",
            "not-an-iso8601-date",
        ] {
            let uncached = parseWithoutMemo(value)
            #expect(ISO8601DateFormatters.parse(value) == uncached)
            #expect(ISO8601DateFormatters.parse(value) == uncached)
        }
    }

    @Test
    func localHealthDecodesHostLinkClaimAndUnknownStartupState() throws {
        let data = Data("""
        {
          "schema_version": 1,
          "health_state": "healthy",
          "severity": "green",
          "headline": "Healthy",
          "reasons": [],
          "suggested_actions": [],
          "host_link": {
            "state": "updating",
            "since": "2026-10-06T12:00:00Z",
            "claim": {
              "type": "host.lifecycle",
              "state": "updating",
              "runtime_epoch": "runtime-a",
              "attempt_id": "attempt-a",
              "phase": "drain",
              "expected_back_by": "2026-10-06T12:00:30Z",
              "deadline": "2026-10-06T12:06:00Z",
              "cutoff": "2026-10-06T12:16:00Z"
            },
            "claim_started_at": "2026-10-06T12:00:00Z",
            "last_acknowledged_at": null,
            "fresh_horizon_secs": 120,
            "last_serving_at": "2026-10-06T11:59:59Z",
            "runtime_epoch": "runtime-a"
          }
        }
        """.utf8)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        let updating = try decoder.decode(HealthSnapshot.self, from: data)

        #expect(updating.hostLink?.state == "updating")
        #expect(updating.hostLink?.claim?.state == "updating")
        #expect(updating.hostLink?.claim?.expectedBackBy == "2026-10-06T12:00:30Z")
        #expect(updating.hostLink?.claimStartedAt == "2026-10-06T12:00:00Z")
        #expect(updating.hostLink?.lastServingAt == "2026-10-06T11:59:59Z")

        let unknownData = Data("""
        {
          "schema_version": 1,
          "health_state": "healthy",
          "severity": "green",
          "headline": "Healthy",
          "reasons": [],
          "suggested_actions": [],
          "host_link": {
            "state": "unknown",
            "since": "2026-10-06T12:00:00Z",
            "last_serving_at": "2026-10-06T11:59:59Z"
          }
        }
        """.utf8)
        let unknown = try decoder.decode(HealthSnapshot.self, from: unknownData)
        #expect(unknown.hostLink?.state == "unknown")
        #expect(unknown.hostLink?.lastServingAt == "2026-10-06T11:59:59Z")
    }

    @Test
    func servingAndUnknownHostLinkStatesKeepTheExistingPresentation() {
        let expected = presentationSnapshot(sessions: [])
            .menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100))

        for state in ["serving", "unknown"] {
            let snapshot = presentationSnapshot(
                sessions: [],
                hostLink: presentationHostLink(state: state)
            )
            #expect(snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100)) == expected)
        }
    }

    @Test
    @MainActor
    func updatingUnderTwoSecondsIsUnchangedAndHidesStaleHeartbeatTrouble() {
        let expected = presentationSnapshot(sessions: [])
            .menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100))
        let snapshot = presentationSnapshot(
            reasons: ["heartbeat_post_failed", "host_updating"],
            sessions: [],
            severity: "red",
            suggestedActionIds: ["inspect_shipping"],
            heartbeatTransport: presentationHeartbeatFailure(),
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: "1970-01-01T00:01:39Z"
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100))
        let status = presentation.facts.first { $0.id == "heartbeat" }

        #expect(presentation.headline == expected.headline)
        #expect(presentation.promotion == expected.promotion)
        #expect(presentation.systemPromotion == expected.systemPromotion)
        #expect(status?.value != "POST failed")
        #expect(!panel(snapshot).showsTroubleCard)
    }

    @Test
    @MainActor
    func updatingAfterTwoSecondsIsNeutralAndHasNoActionCard() {
        let snapshot = presentationSnapshot(
            reasons: ["heartbeat_post_failed", "host_updating"],
            sessions: [],
            severity: "red",
            suggestedActionIds: ["inspect_shipping"],
            heartbeatTransport: presentationHeartbeatFailure(),
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: "1970-01-01T00:01:37Z"
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100))
        let status = presentation.facts.first { $0.id == "heartbeat" }

        #expect(presentation.headline == "Longhouse is updating")
        #expect(presentation.promotion == .unavailable)
        #expect(presentation.promotion.iconSeverity == HarnessSeverity.gray)
        #expect(presentation.systemPromotion == .unavailable)
        #expect(presentation.detail == "Your agents keep running on this Mac. Nothing is lost; updates resume in a few seconds.")
        #expect(status?.value == "Paused · updating")
        #expect(status?.promotion == .unavailable)
        #expect(presentation.hostUpdateClaimIsValid)
        #expect(!panel(snapshot).showsTroubleCard)
    }

    @Test
    @MainActor
    func slowUpdateShowsElapsedTimeInAmberWithoutAnActionCard() {
        let snapshot = presentationSnapshot(
            reasons: ["host_update_slow"],
            sessions: [],
            suggestedActionIds: ["inspect_transport"],
            heartbeatTransport: presentationHeartbeatFailure(),
            hostLink: presentationHostLink(
                state: "slow_update",
                claimStartedAt: "1970-01-01T00:05:20Z"
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 500))
        let status = presentation.facts.first { $0.id == "heartbeat" }

        #expect(presentation.headline == "Update is taking longer than usual · 3m")
        #expect(presentation.promotion == .inspect)
        #expect(presentation.promotion.iconSeverity == HarnessSeverity.yellow)
        #expect(status?.value == "Paused · updating")
        #expect(status?.promotion == .inspect)
        #expect(!panel(snapshot).showsTroubleCard)
    }

    @Test
    @MainActor
    func expiredStoredUpdateClaimRestoresHeartbeatFailureAndTrouble() {
        let referenceDate = Date(timeIntervalSince1970: 100)
        let expiredClaims = [
            (deadline: "1970-01-01T00:01:30Z", cutoff: "1970-01-01T00:02:30Z"),
            (deadline: "1970-01-01T00:02:30Z", cutoff: "1970-01-01T00:01:30Z"),
        ]

        for claim in expiredClaims {
            let snapshot = presentationSnapshot(
                reasons: ["heartbeat_post_failed", "host_updating"],
                sessions: [],
                severity: "yellow",
                suggestedActionIds: ["inspect_shipping"],
                heartbeatTransport: presentationHeartbeatFailure(),
                hostLink: presentationHostLink(
                    state: "updating",
                    claimStartedAt: "1970-01-01T00:00:00Z",
                    deadline: claim.deadline,
                    cutoff: claim.cutoff
                )
            )
            let presentation = snapshot.menuBarPresentation(relativeTo: referenceDate)
            let status = presentation.facts.first { $0.id == "heartbeat" }

            #expect(!presentation.hostUpdateClaimIsValid)
            #expect(presentation.headline == "Machine heartbeat failed")
            #expect(status?.value == "POST failed")
            #expect(panel(snapshot, presentationDate: referenceDate).showsTroubleCard)
        }
    }

    @Test
    func expiredUpdateOnlySnapshotNeedsFreshHeartbeatEvidence() {
        let acknowledgedAt = "1970-01-01T00:00:00Z"
        let snapshot = presentationSnapshot(
            reasons: ["host_updating"],
            sessions: [],
            heartbeatTransport: presentationHeartbeatSuccess(lastSuccessAt: acknowledgedAt),
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: acknowledgedAt,
                deadline: "1970-01-01T00:01:30Z",
                cutoff: "1970-01-01T00:02:30Z",
                lastAcknowledgedAt: acknowledgedAt
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 200))
        let status = presentation.facts.first { $0.id == "heartbeat" }

        #expect(!presentation.hostUpdateClaimIsValid)
        #expect(presentation.headline == "Machine heartbeat failed")
        #expect(presentation.promotion == .inspect)
        #expect(status?.value == "POST failed")
    }


    @Test
    @MainActor
    func validUpdateKeepsIndependentStorageTroubleVisible() {
        let snapshot = presentationSnapshot(
            reasons: ["host_updating", "storage_v2_sources_proof_unknown"],
            sessions: [],
            storageBlocked: 1,
            suggestedActionIds: ["inspect_storage_source"],
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: "1970-01-01T00:01:37Z"
            )
        )
        #expect(snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100)).hostUpdateClaimIsValid)
        #expect(panel(snapshot).showsTroubleCard)
    }

    @Test
    func unavailableEngineEvidenceOutranksUpdatingCopy() {
        let snapshot = presentationSnapshot(
            reasons: ["host_updating", "engine_offline"],
            sessions: [],
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: "1970-01-01T00:01:37Z"
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 100))

        #expect(presentation.hostUpdateClaimIsValid)
        #expect(presentation.systemPromotion == .unavailable)
        #expect(presentation.systemHeadline == "Current local status unavailable")
        #expect(presentation.detail == nil)
    }


    @Test
    func unreachableHostLinkKeepsTheExistingPresentation() {
        let referenceDate = Date(timeIntervalSince1970: 100)
        let expected = presentationSnapshot(
            reasons: ["heartbeat_post_failed"],
            sessions: []
        ).menuBarPresentation(relativeTo: referenceDate)
        let snapshot = presentationSnapshot(
            reasons: ["heartbeat_post_failed"],
            sessions: [],
            hostLink: presentationHostLink(state: "unreachable")
        )

        #expect(snapshot.menuBarPresentation(relativeTo: referenceDate) == expected)
    }

    @Test
    func hostLinkPresentationCopyMatchesSchema() throws {
        let updating = presentationSnapshot(
            sessions: [],
            hostLink: presentationHostLink(
                state: "updating",
                claimStartedAt: "1970-01-01T00:00:00Z"
            )
        ).menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 2))
        let slowUpdate = presentationSnapshot(
            sessions: [],
            hostLink: presentationHostLink(
                state: "slow_update",
                claimStartedAt: "1970-01-01T00:00:00Z"
            )
        ).menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 180))

        #expect(updating.headline == (try hostLinkSchemaCopy("updating.headline")))
        #expect(updating.detail == (try hostLinkSchemaCopy("updating.detail")))
        #expect(slowUpdate.headline == "\(try hostLinkSchemaCopy("slow_update.headline")) · 3m")
        #expect(slowUpdate.detail == (try hostLinkSchemaCopy("updating.detail")))
    }

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
        #expect(presentation.headline == "1 session open")
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
    func legacyPhaseCannotClaimExternalBlock() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "blocked on network")])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(snapshot.currentManagedSessions.first?.menuBarAttentionKind == .phaseUnavailable)
        #expect(presentation.headline == "1 session open")
        #expect(!presentation.headline.contains("needs you"))
    }

    @Test
    func unknownPhaseIsVisibleWithoutGlobalFailure() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "future provider phase")])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .normal)
        #expect(snapshot.currentManagedSessions.first?.menuBarAttentionKind == .phaseUnavailable)
        #expect(!presentation.needsStatusItemBadge)
    }

    @Test
    func attachedSessionWithoutObservedUIPresenceDoesNotClaimAnOpenHelmTerminal() {
        let session = ManagedSessionSnapshot(
            sessionId: "managed-antigravity", provider: "antigravity", workspaceLabel: "longhouse",
            timelineTitle: "Managed headless session", branch: "main", state: "attached",
            phase: "idle", lastActivityAt: "1970-01-01T00:00:00Z", bridgeStatus: "ready",
            bridgePid: nil, bridgeHeartbeatAt: "1970-01-01T00:00:00Z", reasonCodes: []
        )
        let snapshot = presentationSnapshot(sessions: [session])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.headline == "1 session open")
        #expect(!presentation.headline.contains("Helm"))
    }

    @Test
    func durableConflictOutranksSessionInput() {
        let snapshot = presentationSnapshot(
            reasons: ["storage_v2_sources_blocked"],
            sessions: [presentationSession(phase: "needs permission")],
            storageBlocked: 1,
            storageBlockKind: "source_epoch_conflict_unresolved"
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
        #expect(presentation.headline == "Durable upload needs inspection for 1 source")
    }

    @Test
    func nativePayloadConflictPromotesRepairWithoutReasonCode() {
        let snapshot = presentationSnapshot(
            sessions: [],
            storageBlocked: 2,
            storageBlockKind: "source_epoch_conflict_unresolved"
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
        #expect(presentation.headline == "Durable upload needs inspection for 1 source")
    }

    @Test
    func reconcilableSourceConflictStaysAmber() {
        let snapshot = presentationSnapshot(
            sessions: [],
            storageBlocked: 2,
            storageBlockKind: "source_epoch_conflict"
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
        let durable = presentation.facts.first(where: { $0.id == "durable-upload" })

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Source upload reconciliation pending for 2 sources")
        #expect(durable?.promotion == .inspect)
    }

    @Test
    func exhaustedManagedRecoveryStaysAmberAndSpecific() {
        let snapshot = presentationSnapshot(
            reasons: ["managed_launch_recovery_exhausted"],
            sessions: []
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Managed session recovery needs attention")
    }

    @Test
    func unresolvedSourceRemainsRedWhenNewerConflictIsReconcilable() {
        let snapshot = presentationSnapshot(
            sessions: [],
            storageBlocked: 2,
            storageBlockKind: "source_epoch_conflict",
            storageUnresolved: 1
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
        #expect(presentation.headline == "Durable upload needs inspection for 1 source")
    }

    @Test
    func unknownSourceConflictRequiresInspection() {
        let snapshot = presentationSnapshot(
            sessions: [],
            storageBlocked: 1,
            storageBlockKind: nil
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Durable upload proof unavailable for 1 source")
    }


    @Test
    func transportReasonsPromoteRetryingTransport() {
        let snapshot = presentationSnapshot(
            reasons: ["connect_errors"],
            sessions: []
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
        let transport = presentation.facts.first(where: { $0.id == "transport" })

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Local upload needs attention")
        #expect(transport?.value == "Retrying")
        #expect(transport?.promotion == .inspect)
    }

    @Test
    func heartbeatEvidenceRefusalAndRecoveryPreserveIndependentTransport() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/native-desktop-health.json")
        var envelope = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any])
        envelope["heartbeat_transport"] = ["state": "healthy", "evidence_state": "rejected"]
        envelope["reasons"] = ["heartbeat_evidence_rejected"]
        let refused = try HealthSnapshotDecoder.decode(data: JSONSerialization.data(withJSONObject: envelope))
            .markingRuntimeHostProjectionUnavailable()
        let refusal = refused.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 1_785_772_800))
        #expect(refusal.promotion == .inspect)
        #expect(refusal.facts.first(where: { $0.id == "heartbeat" })?.promotion == .inspect)
        #expect(refusal.facts.first(where: { $0.id == "transport" })?.promotion == .normal)

        envelope["heartbeat_transport"] = ["state": "healthy", "evidence_state": "applied"]
        envelope["reasons"] = []
        let recovered = try HealthSnapshotDecoder.decode(data: JSONSerialization.data(withJSONObject: envelope))
        let recovery = recovered.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 1_785_772_800))
        #expect(recovery.facts.first(where: { $0.id == "heartbeat" })?.promotion == .normal)
        #expect(recovery.facts.first(where: { $0.id == "transport" })?.promotion == .normal)
    }

    @Test
    func incompleteSessionDiscoveryKeepsIndependentHealthFactsCurrent() {
        let control = SessionControlSnapshot(
            ownership: "machine",
            connection: "connected",
            controlPlane: "live",
            actions: SessionControlActionsSnapshot(
                terminate: SessionActionSnapshot(state: "available", reason: nil),
                reattach: SessionActionSnapshot(state: "available", reason: nil)
            )
        )
        let snapshot = presentationSnapshot(
            reasons: ["engine_reconciling"],
            sessions: [presentationSession(phase: "idle", control: control)],
            localProjection: LocalProjectionStatus(
                version: 7,
                generatedAt: "1970-01-01T00:00:00Z",
                enginePulseAt: "1970-01-01T00:00:00Z",
                lastReconciledAt: "1970-01-01T00:00:00Z",
                reconciliation: ProjectionReconciliationStatus(
                    state: "reconciling",
                    reason: "startup",
                    startedAt: "1970-01-01T00:00:00Z"
                )
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(snapshot.sessionDiscoveryAttention == true)
        #expect(presentation.promotion == .inspect)
        #expect(presentation.facts.first(where: { $0.id == "local-agent" })?.value == "Running")
        #expect(presentation.facts.first(where: { $0.id == "remote-control" })?.value == "Connected")
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Clear")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Connected")
    }

    @Test
    func unresolvedDiscoveryRetryKeepsFailureReasonVisibleWithoutRepairPromotion() {
        let snapshot = presentationSnapshot(
            reasons: ["engine_reconciling"],
            sessions: [],
            localProjection: LocalProjectionStatus(
                version: 8,
                generatedAt: "1970-01-01T00:00:00Z",
                enginePulseAt: "1970-01-01T00:00:01Z",
                lastReconciledAt: "1970-01-01T00:00:00Z",
                reconciliation: ProjectionReconciliationStatus(
                    state: "reconciling",
                    reason: "retry",
                    startedAt: "1970-01-01T00:00:01Z",
                    failureReason: "unmanaged_binding"
                )
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 1))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Clear")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Connected")
    }

    @Test
    func discoveryWarningDoesNotOutrankRealTransportFailure() {
        let snapshot = presentationSnapshot(
            reasons: ["engine_reconciling", "connect_errors"],
            sessions: []
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Retrying")
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Clear")
    }

    @Test
    func staleDiscoveryReceiptStillKeepsCurrentEngineFactsIndependent() {
        let snapshot = presentationSnapshot(
            reasons: ["engine_reconciliation_stale"],
            sessions: [],
            localProjection: LocalProjectionStatus(
                version: 9,
                generatedAt: "1970-01-01T00:00:00Z",
                enginePulseAt: "1970-01-01T00:00:02Z",
                lastReconciledAt: nil,
                reconciliation: ProjectionReconciliationStatus(
                    state: "idle",
                    reason: nil,
                    startedAt: nil
                )
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 2))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Clear")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Connected")
    }

    @Test
    func failedReconciliationStateRemainsAVisibleDiscoveryFailure() {
        let snapshot = presentationSnapshot(
            sessions: [],
            localProjection: LocalProjectionStatus(
                version: 10,
                generatedAt: "1970-01-01T00:00:00Z",
                enginePulseAt: "1970-01-01T00:00:03Z",
                lastReconciledAt: "1970-01-01T00:00:00Z",
                reconciliation: ProjectionReconciliationStatus(
                    state: "failed",
                    reason: "unmanaged_binding",
                    startedAt: nil,
                    failureReason: "unmanaged_binding"
                )
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 3))

        #expect(snapshot.sessionDiscoveryAttentionReason == "engine_reconciliation_failed")
        #expect(presentation.promotion == .inspect)
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Clear")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Connected")
    }


    @Test
    func scheduledDiscoveryRefreshKeepsCompletedEvidenceCurrent() {
        let snapshot = presentationSnapshot(
            sessions: [],
            localProjection: LocalProjectionStatus(
                version: 10,
                generatedAt: "1970-01-01T00:01:01Z",
                enginePulseAt: "1970-01-01T00:01:01Z",
                lastReconciledAt: "1970-01-01T00:00:00Z",
                reconciliation: ProjectionReconciliationStatus(
                    state: "reconciling",
                    reason: "full_reconciliation",
                    startedAt: "1970-01-01T00:01:00Z"
                )
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 61))

        #expect(snapshot.sessionDiscoveryAttention == false)
        #expect(presentation.promotion == .normal)
    }

    @Test
    func freshNativeEngineCountsAsRunningWhenServiceEvidenceIsAbsent() {
        let snapshot = presentationSnapshot(sessions: [], serviceStatus: nil)

        let local = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
            .facts.first(where: { $0.id == "local-agent" })

        #expect(local?.value == "Running")
        #expect(local?.promotion == .normal)
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
        #expect(presentation.headline == "Local status is stale")
        #expect(presentation.facts.first(where: { $0.id == "local-agent" })?.value == "Running")
        #expect(presentation.facts.first(where: { $0.id == "local-agent" })?.promotion == .unavailable)
    }

    @Test
    func staleProducerCannotClaimNoSessionsOrClearDurableUpload() {
        let snapshot = presentationSnapshot(sessions: [])
        let trust = DataTrust.lastKnown(
            LastKnownContext(lastSuccessAt: Date(timeIntervalSince1970: 0), failure: nil)
        )

        let presentation = snapshot.menuBarPresentation(
            relativeTo: Date(timeIntervalSince1970: 60),
            localEvidenceTrust: trust
        )

        #expect(presentation.promotion == .unavailable)
        #expect(presentation.headline == "Current local status unavailable")
        #expect(presentation.facts.first(where: { $0.id == "durable-upload" })?.value == "Unknown")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Unknown")
    }

    @Test
    func expiredProjectionDoesNotImplyLocalAgentIsDown() {
        let snapshot = presentationSnapshot(sessions: [])
        let projectionTrust = DataTrust.lastKnown(
            LastKnownContext(lastSuccessAt: Date(timeIntervalSince1970: 0), failure: nil)
        )

        let facts = snapshot.menuBarPresentation(
            relativeTo: Date(timeIntervalSince1970: 60),
            projectionTrust: projectionTrust
        ).facts

        #expect(facts.first(where: { $0.id == "local-agent" })?.value == "Running")
        #expect(facts.first(where: { $0.id == "transport" })?.value == "Unknown")
        #expect(facts.first(where: { $0.id == "transport" })?.detail?.contains("Longhouse server") == true)
    }
    @Test
    func expiredProjectionUsesInspectHeadlineWithoutLocalRepair() {
        let snapshot = presentationSnapshot(sessions: [])
        let projectionTrust = DataTrust.lastKnown(
            LastKnownContext(lastSuccessAt: Date(timeIntervalSince1970: 0), failure: nil)
        )

        let presentation = snapshot.menuBarPresentation(
            relativeTo: Date(timeIntervalSince1970: 60),
            projectionTrust: projectionTrust
        )

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Remote session view unavailable")
        #expect(presentation.facts.first(where: { $0.id == "local-agent" })?.value == "Running")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.value == "Unknown")
        #expect(presentation.facts.first(where: { $0.id == "transport" })?.detail?.contains("Longhouse server") == true)
    }


    @Test
    func stalledShippingProgressIsNotRenderedAsClear() {
        let snapshot = presentationSnapshot(
            reasons: ["ship_stalled"],
            sessions: [],
            shippingProgress: ShippingProgressSnapshot(
                pendingWork: true,
                stalled: true,
                secondsWithoutProgress: 90,
                observedAt: "1970-01-01T00:00:00Z"
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 90))
        let durable = presentation.facts.first(where: { $0.id == "durable-upload" })

        #expect(durable?.value == "Stalled")
        #expect(durable?.detail?.contains("no progress 1m") == true)
        #expect(durable?.promotion == .inspect)
    }

    @Test
    func missingRuntimeTruthDoesNotClaimRemoteControlReady() {
        let snapshot = presentationSnapshot(sessions: [presentationSession(phase: "idle")])

        let fact = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
            .facts.first(where: { $0.id == "remote-control" })

        #expect(fact?.value == "Unavailable")
        #expect(fact?.promotion == .unavailable)
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
    func mismatchedServicePromotesRepairWhileAgentStillRuns() {
        let snapshot = presentationSnapshot(
            reasons: ["service_artifact_mismatch"], sessions: [],
            serviceStatus: "running"
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .repair)
    }

    @Test
    func archiveDeadLettersAreInspectableNotRepair() {
        let snapshot = presentationSnapshot(reasons: ["archive_dead_lettered"], sessions: [])

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Historical archive needs review")
    }

    @Test
    func spoolDeadLettersPromoteScopedShippingInspection() {
        let snapshot = presentationSnapshot(
            reasons: ["spool_dead_letters"],
            sessions: [],
            suggestedActionIds: ["inspect_shipping"]
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
        let durable = presentation.facts.first(where: { $0.id == "durable-upload" })

        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Durable upload needs inspection for 1 dead letter")
        #expect(durable?.value == "1 dead letter")
        #expect(durable?.promotion == .inspect)
    }

    @Test
    func archivePauseIsInspectableNotNormal() {
        let snapshot = presentationSnapshot(
            reasons: ["archive_repair_paused"], sessions: [],
            archive: ArchiveBacklogStatus(
                state: "complete", mode: "paused", pendingRanges: 0,
                pendingPaths: 0, pendingSessions: 0, pendingBytes: 0,
                deadRanges: 0, deadBytes: 0
            )
        )

        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))

        #expect(presentation.promotion == .inspect)
        #expect(presentation.needsStatusItemBadge)
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
    func localRefreshToleratesDuplicateCachedSessionIds() throws {
        let connection = RealtimeConnectionSnapshot(
            runtimeUrl: "https://runtime.example",
            machineName: "cinder",
            tokenPath: "/tmp/device-token"
        )
        let antigravity = ManagedSessionSnapshot(
            sessionId: "shared-id", provider: "antigravity", workspaceLabel: "zerg",
            timelineTitle: "Antigravity title", branch: "main", state: "attached",
            phase: "Using shell", lastActivityAt: nil, bridgeStatus: "ready",
            bridgePid: 42, bridgeHeartbeatAt: nil, reasonCodes: [],
            authority: "runtime_host"
        )
        let claude = ManagedSessionSnapshot(
            sessionId: "shared-id", provider: "claude", workspaceLabel: "zerg",
            timelineTitle: "Claude title", branch: "main", state: "attached",
            phase: "Waiting", lastActivityAt: nil, bridgeStatus: "ready",
            bridgePid: 43, bridgeHeartbeatAt: nil, reasonCodes: [],
            authority: "runtime_host"
        )
        let localPreview = ManagedSessionSnapshot(
            sessionId: "shared-id", provider: "antigravity", workspaceLabel: "zerg",
            branch: "new-local-branch", state: "attached", phase: nil,
            lastActivityAt: nil, bridgeStatus: "ready", bridgePid: 42,
            bridgeHeartbeatAt: nil, reasonCodes: [], authority: "machine_preview"
        )
        let previous = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-20T23:17:29Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [antigravity, claude],
            realtime: connection, launchReadiness: nil
        )
        let refreshed = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-26T23:04:37Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [localPreview],
            realtime: connection, launchReadiness: nil
        ).preservingRealtimeProjection(from: previous)

        let session = try #require(refreshed.managedSessions?.first)
        #expect(session.timelineTitle == "Antigravity title")
        #expect(session.phase == "Using shell")
        #expect(session.authority == "runtime_host")
    }

    @Test
    func localRefreshPreservesCanonicalPhaseAndControlProjection() throws {
        let connection = RealtimeConnectionSnapshot(
            runtimeUrl: "https://runtime.example",
            machineName: "cinder",
            tokenPath: "/tmp/device-token"
        )
        let canonical = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "zerg",
            timelineTitle: "Fix refresh clobbering", branch: "main",
            state: "attached", phase: "Using shell", rawPhase: "executing",
            phaseProvenance: "runtime_host", phaseObservedAt: "2026-07-22T20:29:17Z",
            lastActivityAt: "2026-07-22T20:30:31Z", bridgeStatus: "ready",
            bridgePid: 42, bridgeHeartbeatAt: "2026-07-22T20:30:32Z",
            uiAttached: true, uiPresence: "foreground_tui", reasonCodes: [],
            authority: "runtime_host", stateContractVersion: 1,
            presentationPolicyVersion: 1, commitSeq: "738014", mode: "helm",
            presentation: SessionPresentationSnapshot(
                primary: SessionPresentationLabelSnapshot(key: "executing", label: "Using shell", tone: "running"),
                signal: SessionSignalSnapshot(state: "working", validUntil: nil),
                access: SessionPresentationLabelSnapshot(key: "live_control", label: "Live control", tone: "live")
            ),
            activity: SessionActivitySnapshot(
                state: "executing", rawKind: "running", tool: "shell",
                observedAt: "2026-07-22T20:29:17Z"
            ),
            control: SessionControlSnapshot(
                ownership: "owned", connection: "connected",
                actions: SessionControlActionsSnapshot(
                    terminate: SessionActionSnapshot(state: "available", reason: nil),
                    reattach: SessionActionSnapshot(state: "unavailable", reason: "already_connected")
                )
            )
        )
        let localPreview = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "zerg",
            timelineTitle: "Fix refresh clobbering", branch: "new-local-branch",
            state: "attached", phase: nil, lastActivityAt: "2026-07-22T20:30:33Z",
            bridgeStatus: "ready", bridgePid: 42,
            bridgeHeartbeatAt: "2026-07-22T20:30:33Z",
            uiAttached: true, uiPresence: "foreground_tui", reasonCodes: [],
            authority: "machine_preview"
        )
        let previous = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-22T20:30:32Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [canonical],
            realtime: connection, launchReadiness: nil
        )
        let refreshed = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-22T20:30:33Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [localPreview],
            realtime: connection, launchReadiness: nil
        ).preservingRealtimeProjection(from: previous)

        let session = try #require(refreshed.managedSessions?.first)
        #expect(session.branch == "new-local-branch")
        #expect(session.authority == "runtime_host")
        #expect(session.phase == "Using shell")
        #expect(session.presentation?.primary?.key == "executing")
        #expect(session.control?.connection == "connected")
        #expect(session.authority == "runtime_host" && session.control?.actions.terminate?.state == "available")
        #expect(session.menuBarAttentionKind == .working)
    }

    @Test
    func runtimeProjectionRefreshPreservesMachineActionIdentifiers() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-07-22T20:30:32Z",
            healthState: "broken",
            severity: "red",
            headline: "Orphaned managed process",
            reasons: ["orphaned_managed_bridge"],
            suggestedActions: ["Stop the exact managed process"],
            suggestedActionIds: ["stop_managed_bridge"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        #expect(snapshot.markingRuntimeHostProjectionUnavailable().suggestedActionIds == ["stop_managed_bridge"])
    }

    @Test
    func localEngineProjectionCannotRewriteSessionState() throws {
        let session = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "zerg",
            branch: "main", state: "attached", phase: "Thinking", lastActivityAt: nil,
            bridgeStatus: "ready", bridgePid: 42, bridgeHeartbeatAt: nil,
            reasonCodes: [], authority: "runtime_host"
        )
        let snapshot = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-14T17:00:00Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [session],
            launchReadiness: nil
        )

        let refreshed = snapshot.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [LocalStatusMonitor.SessionState(sessionId: "session-1")],
                engine: nil
            )
        )

        let current = try #require(refreshed.managedSessions?.first)
        #expect(current.state == "attached")
        #expect(current.phase == "Thinking")
        #expect(current.authority == "runtime_host")
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
                source: "runtime_host",
                authority: "runtime_host",
                stateContractVersion: 1,
                presentationPolicyVersion: 1,
                commitSeq: "42",
                mode: "helm",
                presentation: SessionPresentationSnapshot(
                    primary: SessionPresentationLabelSnapshot(key: "thinking", label: "Thinking", tone: "active"),
                    signal: SessionSignalSnapshot(state: "working", validUntil: nil),
                    access: SessionPresentationLabelSnapshot(key: "live_control", label: "Live control", tone: "positive")
                ),
                activity: SessionActivitySnapshot(
                    state: "thinking", rawKind: "assistant_working", tool: nil,
                    observedAt: "2026-07-14T05:00:00Z"
                ),
                control: SessionControlSnapshot(
                    ownership: "longhouse", connection: "connected",
                    actions: SessionControlActionsSnapshot(
                        terminate: SessionActionSnapshot(state: "available", reason: nil),
                        reattach: SessionActionSnapshot(state: "available", reason: nil)
                    )
                )
            )
        )

        #expect(updated.resolvedTitleText == "Make titles immediate")
        #expect(updated.phase == "Thinking")
        #expect(updated.titleSource == "prompt")
        #expect(updated.titleProvenance == "runtime_host")
        #expect(updated.phaseProvenance == "runtime_host")
        #expect(updated.authority == "runtime_host" && updated.control?.actions.terminate?.state == "available")

        let titleOnlyUpdate = updated.applying(
            SessionProjection(
                sessionId: "session-1",
                timelineTitle: "A newer title",
                summaryTitle: nil,
                firstUserMessage: nil,
                titleState: "ready",
                titleSource: "summary",
                runtimePhase: "idle",
                displayPhase: "Idle",
                lastActivityAt: "2026-07-14T05:01:00Z",
                source: "runtime_host"
            )
        )
        #expect(titleOnlyUpdate.timelineTitle == "A newer title")
        #expect(titleOnlyUpdate.authority == "runtime_host")
        #expect(titleOnlyUpdate.presentation == updated.presentation)
        #expect(titleOnlyUpdate.activity == updated.activity)
        #expect(titleOnlyUpdate.control == updated.control)
        #expect(titleOnlyUpdate.authority == "runtime_host" && titleOnlyUpdate.control?.actions.terminate?.state == "available")
    }

    @Test
    func realtimeShadowProjectionCannotEnterManagedSessionList() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-07-22T20:30:32Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: [],
            launchReadiness: nil
        )
        let shadow = SessionProjection(
            sessionId: "shadow-1", timelineTitle: "Historical shadow session",
            summaryTitle: nil, firstUserMessage: nil, titleState: "ready",
            titleSource: "ai", runtimePhase: nil, displayPhase: "Inactive",
            lastActivityAt: "2026-07-22T20:30:00Z", source: "runtime_host",
            authority: "runtime_host", mode: "shadow"
        )

        #expect(snapshot.applying(shadow).managedSessions?.isEmpty == true)
    }

    @Test
    func realtimeStreamPreservesCRLFEventBoundaries() {
        var decoder = SessionProjectionStream.SSELineDecoder()
        let payload = Data("event: session_delta\r\ndata: {}\r\n\r\n".utf8)
        let lines = payload.compactMap { decoder.append($0) }

        #expect(lines == ["event: session_delta", "data: {}", ""])
    }

    @Test
    func realtimeStreamCoalescesChattyLivenessIntoLeasePulses() {
        let start = Date(timeIntervalSince1970: 1_000)
        var pulse = ProjectionLeasePulse(lastEmittedAt: start)

        let afterOneSecond = pulse.shouldEmit(at: start.addingTimeInterval(1))
        let beforeInterval = pulse.shouldEmit(at: start.addingTimeInterval(9.999))
        let atInterval = pulse.shouldEmit(at: start.addingTimeInterval(10))
        let immediatelyAfter = pulse.shouldEmit(at: start.addingTimeInterval(10.001))
        let nextInterval = pulse.shouldEmit(at: start.addingTimeInterval(20))

        #expect(!afterOneSecond)
        #expect(!beforeInterval)
        #expect(atInterval)
        #expect(!immediatelyAfter)
        #expect(nextInterval)
    }

    @Test
    func realtimeStreamRetriesWhenResponseBodyStalls() async throws {
        let tokenURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-stream-token-\(UUID().uuidString)")
        try Data("test-token".utf8).write(to: tokenURL)
        defer { try? FileManager.default.removeItem(at: tokenURL) }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StalledStreamURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let connection = RealtimeConnectionSnapshot(
            runtimeUrl: "stalled://runtime.example",
            machineName: nil,
            tokenPath: tokenURL.path
        )
        let stream = SessionProjectionStream.projections(
            connection: connection,
            sessionIds: ["session-1"],
            session: session,
            idleTimeout: 0.25
        )

        let sawFailure = await withTaskGroup(of: Bool.self) { group -> Bool in
            group.addTask {
                for await event in stream {
                    if case .failed = event { return true }
                }
                return false
            }
            group.addTask {
                try? await Task.sleep(for: .seconds(3))
                return false
            }
            let result = await group.next() ?? false
            group.cancelAll()
            return result
        }

        #expect(sawFailure)
        #expect(StalledStreamURLProtocol.requestCounter.value >= 2)
    }

    @Test
    func localStatusMonitorWakesForPulseReconciliationAndSessionChanges() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-status-monitor-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let statusURL = directory.appendingPathComponent("engine-status.json")
        func writeStatus(
            pulse: String,
            reconciliation: String = "idle",
            sessionId: String = "session-1",
            pending: Int = 0
        ) throws {
            let payload: [String: Any] = [
                "managed_sessions": [["session_id": sessionId]],
                "machine_evidence": [
                    "schema_version": 1,
                    "observed_at": pulse,
                    "control": [],
                ],
                "spool_pending_count": pending,
                "last_updated": pulse,
                "local_projection": [
                    "version": 1,
                    "generated_at": pulse,
                    "engine_pulse_at": pulse,
                    "reconciliation": ["state": reconciliation],
                ],
            ]
            try JSONSerialization.data(withJSONObject: payload).write(to: statusURL, options: .atomic)
        }

        try writeStatus(pulse: "initial")
        let changed = ChangeCounter()
        let monitor = LocalStatusMonitor(statusPath: statusURL.path) { projection in
            Task {
                await changed.increment(
                    presentationChanged: projection.presentationChanged,
                    requiresSnapshotRefresh: projection.requiresSnapshotRefresh
                )
            }
        }
        monitor.start()
        defer { monitor.stop() }
        #expect(try await changed.waitForChange(after: 0))
        let beforePulse = await changed.value

        // start() arms the directory watcher asynchronously. Observe a pulse
        // rather than assuming registration and delivery fit a fixed sleep.
        var pulse = ""
        var observedPulse = false
        for attempt in 0..<30 {
            pulse = "pulse-\(attempt)"
            try writeStatus(pulse: pulse)
            if try await changed.waitForChange(after: beforePulse, timeout: .milliseconds(100)) {
                observedPulse = true
                break
            }
        }
        try #require(observedPulse)
        #expect(await changed.presentationChanges.last == false)
        #expect(await changed.refreshRequirements.last == false)

        // A changed clock must not mask state or managed-session changes.
        let beforeReconciliation = await changed.value
        try writeStatus(pulse: pulse, reconciliation: "reconciling")
        #expect(try await changed.waitForChange(after: beforeReconciliation))
        #expect(await changed.presentationChanges.last == true)
        #expect(await changed.refreshRequirements.last == false)

        let beforeSessions = await changed.value
        try writeStatus(pulse: pulse, reconciliation: "reconciling", sessionId: "session-2")
        #expect(try await changed.waitForChange(after: beforeSessions))
        #expect(await changed.presentationChanges.last == true)
        #expect(await changed.refreshRequirements.last == true)
    }

    @Test
    @MainActor
    func statusFileDisappearingAfterObservationIsNotPaperedOver() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-status-vanish-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let statusURL = directory.appendingPathComponent("engine-status.json")
        let now = ISO8601DateFormatter().string(from: Date())
        let payload: [String: Any] = [
            "managed_sessions": [],
            "last_updated": now,
            "local_projection": ["version": 1, "generated_at": now, "engine_pulse_at": now],
        ]
        try JSONSerialization.data(withJSONObject: payload).write(to: statusURL, options: .atomic)
        func snapshot(exists: Bool) -> HealthSnapshot {
            HealthSnapshot(
                schemaVersion: 1,
                collectedAt: now,
                healthState: exists ? "healthy" : "degraded",
                severity: exists ? "green" : "yellow",
                headline: exists ? "healthy" : "Engine status file is missing",
                reasons: [],
                suggestedActions: [],
                service: nil,
                engineStatus: EngineStatusSnapshot(
                    path: statusURL.path,
                    exists: exists,
                    fresh: exists,
                    ageSeconds: nil,
                    payload: nil,
                    error: exists ? nil : "No such file"
                ),
                outbox: nil,
                activitySummary: nil,
                launchReadiness: nil
            )
        }
        let source = CountingHealthSnapshotSource(snapshots: [snapshot(exists: true), snapshot(exists: false)])
        let store = SnapshotStore(source: source, cacheURL: nil)
        store.refresh()
        let clock = ContinuousClock()
        var deadline = clock.now.advanced(by: .seconds(3))
        while store.snapshot?.engineStatus?.payload == nil, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(store.snapshot?.engineStatus?.payload != nil, "the file observation was applied")

        try FileManager.default.removeItem(at: statusURL)
        deadline = clock.now.advanced(by: .seconds(3))
        while store.snapshot?.engineStatus?.exists != false, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(store.snapshot?.engineStatus?.exists == false)
        #expect(store.snapshot?.engineStatus?.error == "No such file")
    }

    @Test
    @MainActor
    func localClockUpdatesDoNotPublishButSemanticChangesDo() throws {
        let now = Date()
        let firstPulse = ISO8601DateFormatters.fractionalTimestamp(for: now.addingTimeInterval(-8))
        let secondPulse = ISO8601DateFormatters.fractionalTimestamp(for: now.addingTimeInterval(-4))
        let thirdPulse = ISO8601DateFormatters.fractionalTimestamp(for: now)
        let baseSnapshot = presentationSnapshot(
            sessions: [],
            shippingProgress: ShippingProgressSnapshot(
                pendingWork: false,
                stalled: false,
                secondsWithoutProgress: 0,
                observedAt: firstPulse
            ),
            localProjection: LocalProjectionStatus(
                version: 1,
                generatedAt: firstPulse,
                enginePulseAt: firstPulse,
                lastReconciledAt: nil,
                reconciliation: nil
            )
        )
        let basePayload = try #require(baseSnapshot.engineStatus?.payload)
        let baseStorage = try #require(basePayload.storageV2Outbox)

        func payload(pulse: String, pending: Int) -> EngineStatusPayload {
            EngineStatusPayload(
                version: basePayload.version,
                daemonPid: basePayload.daemonPid,
                lastShipAt: basePayload.lastShipAt,
                spoolPendingCount: basePayload.spoolPendingCount,
                spoolDeadCount: basePayload.spoolDeadCount,
                archiveBacklog: basePayload.archiveBacklog,
                storageV2Outbox: StorageV2OutboxStatus(
                    pendingCount: pending,
                    pendingBytes: baseStorage.pendingBytes,
                    blockedSourceCount: baseStorage.blockedSourceCount,
                    reconcilingBlockedSourceCount: baseStorage.reconcilingBlockedSourceCount,
                    unresolvedBlockedSourceCount: baseStorage.unresolvedBlockedSourceCount,
                    blockedBytes: baseStorage.blockedBytes,
                    latestBlockSourceEpoch: baseStorage.latestBlockSourceEpoch,
                    latestUnresolvedBlockSourceEpoch: baseStorage.latestUnresolvedBlockSourceEpoch,
                    latestBlockKind: baseStorage.latestBlockKind,
                    latestBlockDetail: baseStorage.latestBlockDetail,
                    byteLimit: baseStorage.byteLimit,
                    error: baseStorage.error,
                    malformedCounter: baseStorage.malformedCounter
                ),
                parseErrorCount1H: basePayload.parseErrorCount1H,
                diskFreeBytes: basePayload.diskFreeBytes,
                isOffline: basePayload.isOffline,
                localProjection: LocalProjectionStatus(
                    version: 1,
                    generatedAt: pulse,
                    enginePulseAt: pulse,
                    lastReconciledAt: nil,
                    reconciliation: nil
                ),
                shippingProgress: ShippingProgressSnapshot(
                    pendingWork: pending > 0,
                    stalled: pending > 0,
                    secondsWithoutProgress: pending > 0 ? 90 : 0,
                    observedAt: pulse
                ),
                recentDeadLetters: basePayload.recentDeadLetters,
                lastUpdated: pulse,
                build: basePayload.build
            )
        }

        let store = SnapshotStore(source: StaticHealthSnapshotSource(snapshot: baseSnapshot), cacheURL: nil)
        var snapshotPublications = 0
        let subscription = store.$snapshot.dropFirst().sink { _ in snapshotPublications += 1 }

        store.ingestLocalStatus(
            LocalStatusMonitor.Projection(sessions: [], engine: payload(pulse: firstPulse, pending: 0))
        )
        store.ingestLocalStatus(
            LocalStatusMonitor.Projection(sessions: [], engine: payload(pulse: secondPulse, pending: 0))
        )
        #expect(snapshotPublications == 0)

        store.ingestLocalStatus(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: payload(pulse: thirdPulse, pending: 1),
                presentationChanged: true
            )
        )
        #expect(snapshotPublications == 1)
        #expect(store.snapshot?.engineStatus?.payload?.storageV2Outbox?.pendingCount == 1)
        _ = subscription
    }

    @Test
    @MainActor
    func localPhaseLedgerChurnProducerLoadCount() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-status-churn-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let statusURL = directory.appendingPathComponent("engine-status.json")
        let cacheDirectory = directory.appendingPathComponent("cache", isDirectory: true)
        try FileManager.default.createDirectory(at: cacheDirectory, withIntermediateDirectories: true)
        let cacheURL = cacheDirectory.appendingPathComponent("last-good-snapshot.json")
        let cacheWrites = try DirectoryWriteCounter(directoryURL: cacheDirectory)
        defer { cacheWrites.stop() }
        let baselineCacheDirectory = directory.appendingPathComponent("baseline-cache", isDirectory: true)
        try FileManager.default.createDirectory(
            at: baselineCacheDirectory,
            withIntermediateDirectories: true
        )
        let baselineCacheURL = baselineCacheDirectory.appendingPathComponent("last-good-snapshot.json")
        let baselineCacheWrites = try DirectoryWriteCounter(directoryURL: baselineCacheDirectory)
        defer { baselineCacheWrites.stop() }

        func waitForCacheWrites(_ counter: DirectoryWriteCounter, _ count: Int) async throws {
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: .seconds(2))
            while counter.value < count, clock.now < deadline {
                try await Task.sleep(for: .milliseconds(10))
            }
            #expect(counter.value >= count)
        }

        func waitForLoads(
            _ source: CountingHealthSnapshotSource,
            _ count: Int,
            timeout: Duration = .seconds(2)
        ) async throws {
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: timeout)
            while source.loadCount < count, clock.now < deadline {
                try await Task.sleep(for: .milliseconds(10))
            }
            #expect(source.loadCount >= count)
        }

        func writeStatus(phase: String) throws {
            let timestamp = ISO8601DateFormatters.fractionalTimestamp(for: Date())
            let payload: [String: Any] = [
                "managed_sessions": [["session_id": "session-1"]],
                "sessions": [["session_id": "session-1", "phase": phase, "phase_observed_at": timestamp]],
                "phase_ledger": [["session_id": "session-1", "phase": phase, "observed_at": timestamp]],
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false,
                "last_updated": timestamp,
            ]
            try JSONSerialization.data(withJSONObject: payload).write(to: statusURL, options: .atomic)
        }

        try writeStatus(phase: "phase-initial")
        let session = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "test",
            branch: nil, state: "attached", phase: nil, lastActivityAt: nil,
            bridgeStatus: nil, bridgePid: nil, bridgeHeartbeatAt: nil, reasonCodes: []
        )
        let snapshot = presentationSnapshot(sessions: [session])
        let source = CountingHealthSnapshotSource(
            snapshots: [snapshot],
            supportsLiveMonitoring: false
        )
        let store = SnapshotStore(source: source, cacheURL: cacheURL)
        let baselineSource = CountingHealthSnapshotSource(
            snapshots: [snapshot],
            supportsLiveMonitoring: false
        )
        let baselineStore = SnapshotStore(source: baselineSource, cacheURL: baselineCacheURL)
        try await waitForCacheWrites(cacheWrites, 1)
        try await waitForCacheWrites(baselineCacheWrites, 1)
        let changes = ChangeCounter()
        let monitor = LocalStatusMonitor(statusPath: statusURL.path) { projection in
            Task { @MainActor in
                store.ingestLocalStatus(projection)
                baselineStore.ingestLocalStatus(
                    LocalStatusMonitor.Projection(
                        sessions: projection.sessions,
                        engine: projection.engine,
                        evidenceObservedAt: projection.evidenceObservedAt,
                        presentationChanged: projection.presentationChanged
                    )
                )
                if projection.requiresSnapshotRefresh {
                    baselineStore.refresh(reason: .background)
                }
                await changes.increment(requiresSnapshotRefresh: projection.requiresSnapshotRefresh)
            }
        }
        monitor.start()
        defer { monitor.stop() }
        #expect(try await changes.waitForChange(after: 0))
        try await Task.sleep(for: .milliseconds(100))
        let beforeChurnLoads = source.loadCount
        let beforeChurnWrites = cacheWrites.value
        let beforeBaselineLoads = baselineSource.loadCount
        let beforeBaselineWrites = baselineCacheWrites.value
        for index in 0..<12 {
            let previous = await changes.value
            try writeStatus(phase: "phase-\(index)")
            #expect(try await changes.waitForChange(after: previous))
            if index < 11 {
                try await Task.sleep(for: .milliseconds(500))
            }
        }
        try await waitForLoads(baselineSource, beforeBaselineLoads + 12)
        let baselineLoadsDuringChurn = baselineSource.loadCount - beforeBaselineLoads
        let baselineWritesDuringChurn = baselineCacheWrites.value - beforeBaselineWrites
        let loadsDuringChurn = source.loadCount - beforeChurnLoads
        let writesDuringChurn = cacheWrites.value - beforeChurnWrites
        print("LOCAL_STATUS_CHURN_BASELINE_PRODUCER_LOADS=\(baselineLoadsDuringChurn)")
        print("LOCAL_STATUS_CHURN_BASELINE_CACHE_WRITES=\(baselineWritesDuringChurn)")
        print("LOCAL_STATUS_CHURN_PRODUCER_LOADS=\(loadsDuringChurn)")
        print("LOCAL_STATUS_CHURN_CACHE_WRITES=\(writesDuringChurn)")
        #expect(baselineLoadsDuringChurn == 12)
        #expect(loadsDuringChurn <= 3)
        // Same headline inside the rewrite window: the cold-launch cache is
        // not replaced, however many refreshes the churn caused.
        #expect(baselineWritesDuringChurn == 0)
        #expect(writesDuringChurn == 0)

        // The last churn inside a coalescing window is caught up at the
        // window's end, not left to the 60 s trust backstop.
        let afterChurnLoads = source.loadCount
        try await waitForLoads(source, afterChurnLoads + 1, timeout: .seconds(7))
    }





    @Test
    @MainActor
    func unchangedCachedSnapshotDoesNotWriteAgain() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-cache-write-\(UUID().uuidString)")
        let cacheDirectory = directory.appendingPathComponent("cache", isDirectory: true)
        try FileManager.default.createDirectory(at: cacheDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cacheWrites = try DirectoryWriteCounter(directoryURL: cacheDirectory)
        defer { cacheWrites.stop() }
        let cacheURL = cacheDirectory.appendingPathComponent("last-good-snapshot.json")
        let source = CountingHealthSnapshotSource(
            snapshots: [presentationSnapshot(sessions: [])],
            supportsLiveMonitoring: false
        )
        let store = SnapshotStore(source: source, cacheURL: cacheURL)

        let clock = ContinuousClock()
        let cacheDeadline = clock.now.advanced(by: .seconds(2))
        while cacheWrites.value < 1, clock.now < cacheDeadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(cacheWrites.value == 1)

        let previousState = store.refreshState
        store.refresh()
        let refreshDeadline = clock.now.advanced(by: .seconds(2))
        while store.refreshState == previousState, clock.now < refreshDeadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(store.refreshState != previousState)
        try await Task.sleep(for: .milliseconds(100))
        #expect(cacheWrites.value == 1)
    }

    @Test
    @MainActor
    func cachedSnapshotIsRewrittenAtOnceWhenHealthChanges() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-cache-health-\(UUID().uuidString)")
        let cacheDirectory = directory.appendingPathComponent("cache", isDirectory: true)
        try FileManager.default.createDirectory(at: cacheDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cacheURL = cacheDirectory.appendingPathComponent("last-good-snapshot.json")
        let source = CountingHealthSnapshotSource(
            snapshots: [
                presentationSnapshot(sessions: []),
                presentationSnapshot(sessions: [], severity: "red"),
            ],
            supportsLiveMonitoring: false
        )
        let store = SnapshotStore(source: source, cacheURL: cacheURL)
        let clock = ContinuousClock()

        func cachedSeverity() -> String? {
            guard let data = try? Data(contentsOf: cacheURL),
                  let snapshot = try? HealthSnapshotDecoder.decode(data: data) else { return nil }
            return snapshot.severity
        }
        func waitForSeverity(_ severity: String) async throws {
            let deadline = clock.now.advanced(by: .seconds(2))
            while cachedSeverity() != severity, clock.now < deadline {
                try await Task.sleep(for: .milliseconds(10))
            }
        }

        try await waitForSeverity("green")
        #expect(cachedSeverity() == "green")
        // Inside the minute-long rewrite window, but the health changed: a cold
        // launch must not come up showing the old state.
        store.refresh()
        try await waitForSeverity("red")
        #expect(cachedSeverity() == "red")
    }

    @Test
    @MainActor
    func localStatusCriticalTransitionsAndSessionChangesBypassChurnCoalescing() async throws {
        let session = ManagedSessionSnapshot(
            sessionId: "session-1", provider: "codex", workspaceLabel: "test",
            branch: nil, state: "attached", phase: nil, lastActivityAt: nil,
            bridgeStatus: nil, bridgePid: nil, bridgeHeartbeatAt: nil, reasonCodes: []
        )
        let snapshot = presentationSnapshot(sessions: [session])
        let base = try #require(snapshot.engineStatus?.payload)
        let source = CountingHealthSnapshotSource(snapshots: [snapshot], supportsLiveMonitoring: false)
        let store = SnapshotStore(source: source, cacheURL: nil)

        func payload(
            isOffline: Bool?,
            spoolDeadCount: Int?,
            localProjection: LocalProjectionStatus?
        ) -> EngineStatusPayload {
            EngineStatusPayload(
                version: base.version,
                daemonPid: base.daemonPid,
                lastShipAt: base.lastShipAt,
                spoolPendingCount: base.spoolPendingCount,
                spoolDeadCount: spoolDeadCount,
                archiveBacklog: base.archiveBacklog,
                storageV2Outbox: base.storageV2Outbox,
                parseErrorCount1H: base.parseErrorCount1H,
                diskFreeBytes: base.diskFreeBytes,
                isOffline: isOffline,
                localProjection: localProjection,
                shippingProgress: base.shippingProgress,
                recentDeadLetters: base.recentDeadLetters,
                lastUpdated: base.lastUpdated,
                build: base.build
            )
        }

        func requirePromptRefresh(_ projection: LocalStatusMonitor.Projection) async throws {
            let previousCount = source.loadCount
            let previousState = store.refreshState
            store.ingestLocalStatus(projection)
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: .seconds(1))
            while (source.loadCount == previousCount || store.refreshState == previousState),
                  clock.now < deadline {
                try await Task.sleep(for: .milliseconds(10))
            }
            #expect(source.loadCount == previousCount + 1)
            #expect(store.refreshState != previousState)
        }

        try await requirePromptRefresh(
            LocalStatusMonitor.Projection(
                sessions: [LocalStatusMonitor.SessionState(sessionId: "session-2")],
                engine: base
            )
        )
        try await requirePromptRefresh(
            LocalStatusMonitor.Projection(
                sessions: [LocalStatusMonitor.SessionState(sessionId: "session-1")],
                engine: payload(
                    isOffline: true,
                    spoolDeadCount: base.spoolDeadCount,
                    localProjection: base.localProjection
                )
            )
        )
        try await requirePromptRefresh(
            LocalStatusMonitor.Projection(
                sessions: [LocalStatusMonitor.SessionState(sessionId: "session-1")],
                engine: payload(
                    isOffline: base.isOffline,
                    spoolDeadCount: 1,
                    localProjection: base.localProjection
                )
            )
        )
        let failedReconciliation = LocalProjectionStatus(
            version: 1,
            generatedAt: nil,
            enginePulseAt: nil,
            lastReconciledAt: nil,
            reconciliation: ProjectionReconciliationStatus(
                state: "failed",
                reason: nil,
                startedAt: nil,
                failureReason: "scan failed"
            )
        )
        try await requirePromptRefresh(
            LocalStatusMonitor.Projection(
                sessions: [LocalStatusMonitor.SessionState(sessionId: "session-1")],
                engine: payload(
                    isOffline: base.isOffline,
                    spoolDeadCount: base.spoolDeadCount,
                    localProjection: failedReconciliation
                )
            )
        )
    }

    @Test
    @MainActor
    func ageLabelsAdvanceUnderSustainedLocalUpdates() async throws {
        let store = SnapshotStore(
            source: StaticHealthSnapshotSource(snapshot: presentationSnapshot(sessions: [])),
            cacheURL: nil
        )
        store.beginPresentationUpdates()
        defer { store.endPresentationUpdates() }
        let first = store.presentationDate
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(SnapshotStore.presentationUpdateIntervalSeconds + 2))
        // A local update every 200 ms re-arms the deadline each time; the age
        // tick must still land.
        while store.presentationDate == first, clock.now < deadline {
            store.ingestLocalStatus(LocalStatusMonitor.Projection(sessions: [], engine: nil))
            try await Task.sleep(for: .milliseconds(200))
        }
        #expect(store.presentationDate > first)
    }

    @Test
    func heartbeatSendTelemetryAloneNeverRequiresARefresh() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-status-telemetry-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let statusURL = directory.appendingPathComponent("engine-status.json")
        func write(sends: Int) throws {
            let payload: [String: Any] = [
                "managed_sessions": [],
                "heartbeat_transport": [
                    "state": "connected",
                    "send_metrics": ["last_send_wire_bytes": 57_000 + sends, "post_latency_sample_count": sends],
                    "resource_usage": ["sample_count": sends],
                ],
            ]
            try JSONSerialization.data(withJSONObject: payload).write(to: statusURL, options: .atomic)
        }
        try write(sends: 0)
        let changed = ChangeCounter()
        let monitor = LocalStatusMonitor(statusPath: statusURL.path) { projection in
            Task {
                await changed.increment(
                    presentationChanged: projection.presentationChanged,
                    requiresSnapshotRefresh: projection.requiresSnapshotRefresh
                )
            }
        }
        monitor.start()
        defer { monitor.stop() }
        #expect(try await changed.waitForChange(after: 0))
        for sends in 1...5 {
            try write(sends: sends)
            try await Task.sleep(for: .milliseconds(100))
        }
        #expect(await changed.refreshRequirements.allSatisfy { $0 == false })
        #expect(await changed.presentationChanges.dropFirst().allSatisfy { $0 == false })

        // A failure counter is health, not churn: crossing into errors asks
        // for a producer refresh.
        var refreshRequested = false
        for attempt in 0..<20 where !refreshRequested {
            let payload: [String: Any] = [
                "managed_sessions": [],
                "parse_error_count_1h": attempt + 1,
                "heartbeat_transport": [
                    "state": "connected",
                    "send_metrics": ["last_send_wire_bytes": 57_005, "post_latency_sample_count": 5],
                ],
            ]
            try JSONSerialization.data(withJSONObject: payload).write(to: statusURL, options: .atomic)
            try await Task.sleep(for: .milliseconds(100))
            refreshRequested = await changed.refreshRequirements.contains(true)
        }
        #expect(refreshRequested)
    }

    @Test
    @MainActor
    func sessionLeaseExpiryInvalidatesPresentationWithoutFileEvent() async throws {
        let expiry = Date().addingTimeInterval(0.4)
        let validUntil = ISO8601DateFormatters.fractionalTimestamp(for: expiry)
        let session = ManagedSessionSnapshot(
            sessionId: "lease-session",
            provider: "codex",
            workspaceLabel: "longhouse",
            branch: "main",
            state: "attached",
            phase: "Working",
            lastActivityAt: nil,
            bridgeStatus: "ready",
            bridgePid: 42,
            bridgeHeartbeatAt: nil,
            reasonCodes: [],
            presentation: SessionPresentationSnapshot(
                primary: SessionPresentationLabelSnapshot(key: "needs_answer", label: "Needs answer", tone: "attention"),
                signal: SessionSignalSnapshot(state: "attention", validUntil: validUntil),
                access: nil
            )
        )
        let store = SnapshotStore(
            source: StaticHealthSnapshotSource(snapshot: presentationSnapshot(sessions: [session])),
            cacheURL: nil
        )
        store.beginPresentationUpdates()
        defer { store.endPresentationUpdates() }
        let initialPresentationDate = store.presentationDate
        #expect(session.menuBarAttentionKind(asOf: initialPresentationDate) == .needsYou)

        var presentationUpdates = 0
        let subscription = store.$presentationDate.dropFirst().sink { _ in presentationUpdates += 1 }
        try await Task.sleep(for: .milliseconds(700))

        #expect(presentationUpdates >= 1)
        #expect(store.presentationDate >= expiry)
        #expect(
            store.snapshot?.managedSessions?.first?.menuBarAttentionKind(asOf: store.presentationDate)
                == .unknown("activity unknown")
        )
        _ = subscription
    }

    @Test
    @MainActor
    func quietMachineRefreshesProducerBeforeTrustExpires() async throws {
        let source = CountingHealthSnapshotSource(snapshots: [presentationSnapshot(sessions: [])])
        let store = SnapshotStore(source: source, cacheURL: nil, refreshDeadline: 0.4)
        store.refresh()
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(3))
        while source.loadCount < 3, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        // No engine event and no --refresh-seconds: the backstop alone keeps
        // the producer inside its trust deadline.
        #expect(source.loadCount >= 3)
        #expect(store.dataTrust() == .current)
    }

    @Test
    @MainActor
    func presentationUpdatesNeverInventEngineFileEvidence() async throws {
        let missing = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-10-11T00:00:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Engine status file is missing",
            reasons: ["engine_status_missing"],
            suggestedActions: [],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: "/nonexistent/engine-status.json",
                exists: false,
                fresh: false,
                ageSeconds: nil,
                payload: nil,
                error: "No such file"
            ),
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let store = SnapshotStore(source: StaticHealthSnapshotSource(snapshot: missing), cacheURL: nil)
        store.refresh()
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(3))
        while store.snapshot == nil, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        store.beginPresentationUpdates()
        defer { store.endPresentationUpdates() }
        #expect(store.snapshot?.engineStatus?.exists == false)
        #expect(store.snapshot?.engineStatus?.error == "No such file")
    }

    @Test
    @MainActor
    func visiblePanelAgeLabelsAdvanceWithoutStateChanges() async throws {
        let store = SnapshotStore(
            source: StaticHealthSnapshotSource(snapshot: presentationSnapshot(sessions: [])),
            cacheURL: nil
        )
        store.beginPresentationUpdates()
        defer { store.endPresentationUpdates() }
        let first = store.presentationDate
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(SnapshotStore.presentationUpdateIntervalSeconds + 2))
        while store.presentationDate == first, clock.now < deadline {
            try await Task.sleep(for: .milliseconds(50))
        }
        #expect(store.presentationDate > first)
    }

    @Test
    @MainActor
    func snapshotStoreRefreshesWhenReconciliationClassificationChanges() {
        func payload(
            state: String,
            failureReason: String? = nil,
            lastReconciledAt: String? = "2026-09-15T18:00:00Z"
        ) -> EngineStatusPayload {
            EngineStatusPayload(
                version: "test",
                daemonPid: 42,
                lastShipAt: nil,
                spoolPendingCount: 0,
                spoolDeadCount: 0,
                parseErrorCount1H: 0,
                diskFreeBytes: nil,
                isOffline: false,
                localProjection: LocalProjectionStatus(
                    version: 1,
                    generatedAt: "2026-09-15T18:00:01Z",
                    enginePulseAt: "2026-09-15T18:00:01Z",
                    lastReconciledAt: lastReconciledAt,
                    reconciliation: ProjectionReconciliationStatus(
                        state: state,
                        reason: state == "reconciling" ? "retry" : nil,
                        startedAt: state == "reconciling" ? "2026-09-15T18:00:01Z" : nil,
                        failureReason: failureReason
                    )
                ),
                recentDeadLetters: nil,
                lastUpdated: "2026-09-15T18:00:01Z"
            )
        }

        let idle = payload(state: "idle")
        let pulseOnly = payload(state: "idle")
        let failedRetry = payload(state: "reconciling", failureReason: "unmanaged_binding")
        let completed = payload(state: "idle", lastReconciledAt: "2026-09-15T18:01:00Z")

        #expect(!SnapshotStore.classificationChanged(from: idle, to: pulseOnly))
        #expect(SnapshotStore.classificationChanged(from: idle, to: failedRetry))
        #expect(SnapshotStore.classificationChanged(from: failedRetry, to: completed))
        // Another clean pass only moves the clock; the file shows it locally.
        #expect(!SnapshotStore.classificationChanged(from: idle, to: completed))
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
        #expect(snapshot.hasResolvedPythonPackageVersion == true)
        #expect(snapshot.pythonPackageVersionLabel == "0.2.0-dev+cccccccc.dirty")
        #expect(snapshot.nativePairVersionLabel == "0.1.64-dev+bbbbbbbb.dirty / 0.1.64-dev+bbbbbbbb.dirty")
        #expect(snapshot.runningEngineVersionLabel == "0.1.64-dev+aaaaaaaa.dirty")
        #expect(snapshot.engineRestartPending == true)
        #expect(snapshot.restartPendingChipLabel == "RESTART PENDING")
        #expect(snapshot.needsMenuBarAttention == false)
    }


    @Test
    @MainActor
    func fixtureStoreDoesNotStartLiveMonitoring() async throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let recordedURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/native-desktop-health.json")
        let statusURL = tempDir.appendingPathComponent("engine-status.json")
        try Data(#"{"managed_sessions":[]}"#.utf8).write(to: statusURL)

        var fixture = try #require(
            JSONSerialization.jsonObject(with: Data(contentsOf: recordedURL)) as? [String: Any]
        )
        var engineStatus = try #require(fixture["engine_status"] as? [String: Any])
        engineStatus["path"] = statusURL.path
        fixture["engine_status"] = engineStatus
        let fixtureURL = tempDir.appendingPathComponent("fixture.json")
        try JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys]).write(to: fixtureURL)

        let source = FixtureHealthSnapshotSource(fileURL: fixtureURL)
        #expect(source.supportsLiveMonitoring == false)
        let store = SnapshotStore(source: source)
        store.refresh(reason: .manual)
        for _ in 0..<100 where store.isManualRefreshActive {
            try? await Task.sleep(for: .milliseconds(10))
        }
        let settledSuccessAt = try #require(store.refreshState.lastSuccessAt)

        try Data(#"{"managed_sessions":[{"session_id":"unexpected-live-update"}]}"#.utf8)
            .write(to: statusURL)
        try? await Task.sleep(for: .milliseconds(150))

        #expect(store.refreshState.lastSuccessAt == settledSuccessAt)
        #expect(store.projectionState == .neverAttempted)
        #expect(store.snapshot?.managedSessions?.first?.sessionId == "00000000-0000-4000-8000-000000000001")
    }

    @Test
    func parsesRuntimeArguments() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: [
            "--input", "/tmp/example.json",
            "--output", "/tmp/example.png",
            "--action-log", "/tmp/actions.jsonl",
            "--ui-url", "https://longhouse.ai",
            "--appearance", "light",
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
        #expect(config.renderAppearance == .light)
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
    func sourceInspectionDryRunUsesScopedShippingCommand() throws {
        let sourceEpoch = "01234567-89ab-cdef-0123-456789abcdef"
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Durable upload needs inspection",
            reasons: ["storage_v2_sources_unresolved"],
            suggestedActions: ["Inspect retained source evidence"],
            service: nil,
            engineStatus: EngineStatusSnapshot(
                path: nil,
                exists: true,
                fresh: true,
                ageSeconds: 1,
                payload: EngineStatusPayload(
                    version: "test",
                    daemonPid: 1,
                    lastShipAt: nil,
                    spoolPendingCount: 0,
                    spoolDeadCount: 0,
                    storageV2Outbox: StorageV2OutboxStatus(
                        pendingCount: 0,
                        pendingBytes: 0,
                        blockedSourceCount: 1,
                        blockedBytes: 0,
                        latestBlockSourceEpoch: sourceEpoch,
                        latestUnresolvedBlockSourceEpoch: sourceEpoch,
                        latestBlockKind: "source_epoch_conflict_unresolved",
                        latestBlockDetail: nil,
                        byteLimit: 1_073_741_824,
                        error: nil
                    ),
                    parseErrorCount1H: 0,
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: [],
                    lastUpdated: "2026-04-08T01:52:00Z"
                ),
                error: nil
            ),
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.inspectStorageSource, snapshot: snapshot)

        #expect(feedback?.style == .info)
        #expect(feedback?.title == "Source inspection dry run recorded")
        #expect(feedback?.detail.contains("longhouse shipping inspect --source-epoch \(sourceEpoch) --json") == true)
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
    func freeDiskSpaceDryRunReturnsVisibleFeedback() throws {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-08T01:52:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Disk space is critically low",
            reasons: ["disk_critically_low"],
            suggestedActions: ["free_disk_space"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.freeDiskSpace, snapshot: snapshot)

        #expect(feedback?.style == .info)
        #expect(feedback?.title == "Free disk space dry run recorded")
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
        #expect(
            sink.stopTransportForTesting(provider: "codex", controlPlane: "codex_app_server") == "codex"
        )
        #expect(
            sink.stopTransportForTesting(provider: "cursor", controlPlane: "cursor_acp") == "cursor"
        )
        #expect(
            sink.stopTransportForTesting(provider: "codex", controlPlane: "claude_channel_bridge") == "unsupported"
        )
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

    /// DMG-only Mac: the app starts before any `longhouse` exists, then its own
    /// first-run setup script installs one. The app used to keep asking a login
    /// shell for it (which never reads the ~/.zshrc the installer edits), so it
    /// reported "Finish setup" after a successful sign-in until it was relaunched.
    @Test
    func appHealthSourceFindsTheCLIInstalledAfterTheAppStarted() throws {
        let homeDirectory = try makeFakeHomeDirectory()
        let source = CLIHealthSnapshotSource(
            followingInstalledCLI: {
                LonghouseCLI.defaultHealthInvocation(homeDirectory: homeDirectory, pathEnvironment: "/usr/bin:/bin")
            },
            currentBundlePath: "/Applications/Longhouse.app"
        )
        let expectedPath = homeDirectory.appendingPathComponent(".local/bin/longhouse").path

        #expect(source.currentInvocation.launchPath != expectedPath)

        _ = try installFakeLonghouseBinary(
            homeDirectory: homeDirectory,
            script: "#!/bin/sh\ncat '\(harnessFixtureURL("healthy").path)'\n"
        )

        #expect(source.currentInvocation.launchPath == expectedPath)
        #expect(source.describedCommand == "\(expectedPath) local-health --json")
        let snapshot = try source.load()
        #expect(snapshot.isSetupRequired == false)
    }

    @Test
    func signInTerminalScriptBringsTerminalForwardAndEscapesTheCommand() {
        let source = SpyHealthActionSink.terminalScriptSource(command: "echo \"hi\" \\ there")

        #expect(source.contains("activate"))
        #expect(source.contains("do script \"echo \\\"hi\\\" \\\\ there\""))
    }

    @Test
    func hostLabelKeepsAnAddressWholeAndShortensANameToItsFirstLabel() {
        func label(_ url: String) -> String {
            HealthSnapshot(
                schemaVersion: 1, collectedAt: "2026-09-30T20:00:00Z",
                healthState: "healthy", severity: "green", headline: "Healthy",
                reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
                outbox: nil, activitySummary: nil, managedSessions: nil,
                realtime: RealtimeConnectionSnapshot(runtimeUrl: url, machineName: "mac", tokenPath: nil),
                launchReadiness: nil
            ).hostValueLabel
        }

        #expect(label("http://127.0.0.1:18081") == "127.0.0.1")
        #expect(label("http://192.168.1.20:8080") == "192.168.1.20")
        #expect(label("http://[::1]:8080") == "::1")
        #expect(label("http://localhost:8080") == "localhost")
        #expect(label("https://david010.longhouse.ai") == "david010")
    }

    private func harnessFixtureURL(_ name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(name).json")
    }

    @Test
    func nativeSetupRequiredPresentsSignInInsteadOfStaleWall() throws {
        // Captured from `longhouse local-health --json` in a fresh HOME that
        // has the native pair installed but was never authorized.
        let snapshot = try FixtureHealthSnapshotSource(fileURL: harnessFixtureURL("setup-required")).load()
        let presentation = snapshot.menuBarPresentation(relativeTo: Date())

        #expect(snapshot.isSetupRequired == true)
        #expect(snapshot.sessionDiscoveryAttention == false)
        #expect(presentation.promotion == .repair)
        #expect(presentation.headline == "Finish setup on this Mac")
        #expect(snapshot.ambientStatusLabel == "Setup required")
        #expect(snapshot.attentionSummaryLabel.contains("Sign in"))

        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly)
        let feedback = sink.handle(.repairInstall, snapshot: snapshot)
        #expect(feedback?.title == "Setup dry run recorded")
    }

    @Test
    func brokenRetainedDataOnUnconfiguredMacIsNotHiddenBehindSignIn() throws {
        let data = try Data(contentsOf: harnessFixtureURL("setup-required"))
        var envelope = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        envelope["health_state"] = "broken"
        envelope["severity"] = "red"
        envelope["reasons"] = ["machine_setup_required", "storage_v2_outbox_unreadable"]
        let snapshot = try HealthSnapshotDecoder.decode(data: JSONSerialization.data(withJSONObject: envelope))

        #expect(snapshot.isSetupRequired == false)
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).headline != "Finish setup on this Mac")
    }

    @Test
    func configuredMachineFixturesAreNotSetupRequired() throws {
        let fixtures = try FileManager.default.contentsOfDirectory(
            at: harnessFixtureURL("healthy").deletingLastPathComponent(),
            includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "json" && $0.lastPathComponent != "setup-required.json" }
        #expect(!fixtures.isEmpty)
        for fixture in fixtures {
            let snapshot = try FixtureHealthSnapshotSource(fileURL: fixture).load()
            #expect(snapshot.isSetupRequired == false, "\(fixture.lastPathComponent)")
        }
    }

    @Test
    func setupTerminalCommandQuotesEveryArgument() {
        let command = LonghouseCLI.terminalCommand(
            for: ("/bin/zsh", ["/Applications/Long house.app/it's/desktop-app-setup.sh"])
        )
        #expect(command == "'/bin/zsh' '/Applications/Long house.app/it'\\''s/desktop-app-setup.sh'")
    }

    @Test
    func cliSourceDrainsLargeStdoutAndStderrConcurrently() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }
        let payloadURL = tempDir.appendingPathComponent("health.json")
        let payload: [String: Any] = [
            "schema_version": 1,
            "collected_at": "2026-05-05T12:00:00Z",
            "health_state": "healthy",
            "severity": "green",
            "headline": "Longhouse shipping healthy",
            "reasons": [String(repeating: "x", count: 200000)],
            "suggested_actions": [String](),
        ]
        try JSONSerialization.data(withJSONObject: payload).write(to: payloadURL)
        let outputCommand = LonghouseCLI.terminalCommand(for: ("/bin/cat", [payloadURL.path]))
        let source = CLIHealthSnapshotSource(
            launchPath: "/bin/sh",
            arguments: ["-c", "\(outputCommand) && /usr/bin/head -c 200000 /dev/zero >&2"]
        )

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
        #expect(invocation.arguments == ["local-health", "--json"])
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
    func nativeBootstrapDecodesEnginePulseWithoutLaunchingCLI() throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let statusURL = tempDir.appendingPathComponent("engine-status.json")
        try Data(
            #"{"daemon_pid":42,"local_projection":{"engine_pulse_at":"2026-07-16T16:00:00Z","reconciliation":{"state":"reconciling","reason":"startup"}},"last_updated":"2026-07-16T16:00:00Z"}"#.utf8
        ).write(to: statusURL)

        let snapshot = SnapshotStore.loadNativeBootstrapSnapshot(
            from: statusURL,
            referenceDate: ISO8601DateFormatter().date(from: "2026-07-16T16:00:01Z")!
        )

        #expect(snapshot?.headline == "Refreshing local status")
        #expect(snapshot?.engineStatus?.fresh == true)
        #expect(snapshot?.engineStatus?.payload?.daemonPid == 42)
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
    @MainActor
    func signInPollsUntilTheEngineStatusWatchIsArmedThenStops() async throws {
        // Sign-in completes in Terminal and the browser, and the new engine
        // creates its status directory a moment later. The window kept "Finish
        // setup", then "Local status is stale" (stranger runs 100402498d26,
        // 10041609a760), because nothing re-read local health once the watch
        // had failed to open a directory that did not exist yet.
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-signin-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: tempDir) }
        let statusPath = tempDir.appendingPathComponent("agent/engine-status.json").path
        func snapshot(_ state: String, _ headline: String, statusPath: String?) -> HealthSnapshot {
            HealthSnapshot(
                schemaVersion: 1,
                collectedAt: "2026-10-04T02:53:00Z",
                healthState: state,
                severity: state == "healthy" ? "green" : "yellow",
                headline: headline,
                reasons: state == "healthy" ? [] : ["machine_setup_required"],
                suggestedActions: [],
                service: nil,
                engineStatus: statusPath.map {
                    EngineStatusSnapshot(path: $0, exists: false, fresh: false, ageSeconds: nil, payload: nil, error: nil)
                },
                outbox: nil,
                activitySummary: nil,
                launchReadiness: nil
            )
        }
        let setup = snapshot("setup_required", "Sign in to connect this machine to Longhouse", statusPath: nil)
        let connected = snapshot("healthy", "Longhouse native health is healthy", statusPath: statusPath)
        let source = CountingHealthSnapshotSource(snapshots: [setup, setup, connected])
        let store = SnapshotStore(source: source, cacheURL: nil, setupRefreshInterval: 0.02)
        store.setFeedback(HealthActionFeedback(
            action: .repairInstall,
            style: .info,
            title: "Sign-in opened in Terminal",
            detail: "Enter your Longhouse address there and approve this Mac in the browser, then click Refresh."
        ))

        store.refresh(reason: .manual)
        for _ in 0..<100 {
            if store.snapshot?.headline == connected.headline { break }
            try? await Task.sleep(for: .milliseconds(10))
        }
        #expect(store.snapshot?.headline == connected.headline)
        #expect(store.feedback == nil, "the sign-in instruction goes away once the Mac is connected")
        #expect(!store.isWatchingLocalStatus, "the engine has not created its status directory yet")
        let loadsBeforeDirectory = source.loadCount
        try? await Task.sleep(for: .milliseconds(100))
        #expect(source.loadCount > loadsBeforeDirectory, "with nothing to watch, local health is re-read")

        try FileManager.default.createDirectory(
            at: URL(fileURLWithPath: statusPath).deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        for _ in 0..<100 {
            if store.isWatchingLocalStatus { break }
            try? await Task.sleep(for: .milliseconds(10))
        }
        #expect(store.isWatchingLocalStatus)
        try? await Task.sleep(for: .milliseconds(60))
        let loadsWhenWatching = source.loadCount
        try? await Task.sleep(for: .milliseconds(150))
        #expect(source.loadCount == loadsWhenWatching, "an armed watch takes over from polling")
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
        #expect(snapshot.attentionSummaryLabel.contains("reconcile the local runtime"))
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
            suggestedActions: ["Inspect archive backlog: longhouse local-health --json"],
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
                    diskFreeBytes: nil,
                    isOffline: false,
                    recentDeadLetters: nil,
                    lastUpdated: "2026-04-08T01:52:01Z"
                )
            )
        )
        #expect(!drained.attentionSummaryLabel.contains("transcript range"))
        // A local engine pulse updates liveness, not the freshness of the
        // producer snapshot shown above the panel.
        #expect(drained.collectedAt == "2026-04-08T01:52:00Z")
    }

    @Test
    func localShippingPulseProjectsStallReasonTransportAndActionTogether() throws {
        let snapshot = presentationSnapshot(sessions: [])
        let stalled = snapshot.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: EngineStatusPayload(
                    version: "test", daemonPid: 1, lastShipAt: nil,
                    spoolPendingCount: 1, spoolDeadCount: 0,
                    parseErrorCount1H: 0, diskFreeBytes: nil, isOffline: false,
                    shippingProgress: ShippingProgressSnapshot(
                        pendingWork: true, stalled: true,
                        secondsWithoutProgress: 90, observedAt: "2026-08-03T16:00:00Z"
                    ),
                    recentDeadLetters: [], lastUpdated: nil
                )
            )
        )

        #expect(stalled.reasons.contains("ship_stalled"))
        #expect(stalled.suggestedActionIds == ["inspect_transport"])
        #expect(stalled.transport?.status == "degraded")
        #expect(stalled.healthState == "degraded")
        #expect(stalled.severity == "yellow")
        #expect(stalled.attention?.state == "needs_attention")
        #expect(stalled.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).headline == "Local upload needs attention")
        #expect(stalled.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).facts.first(where: { $0.id == "durable-upload" })?.value == "Stalled")

        let recovered = stalled.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: EngineStatusPayload(
                    version: "test", daemonPid: 1, lastShipAt: nil,
                    spoolPendingCount: 0, spoolDeadCount: 0,
                    parseErrorCount1H: 0, diskFreeBytes: nil, isOffline: false,
                    shippingProgress: ShippingProgressSnapshot(
                        pendingWork: false, stalled: false,
                        secondsWithoutProgress: 0, observedAt: "2026-08-03T16:01:00Z"
                    ),
                    recentDeadLetters: [], lastUpdated: nil
                )
            )
        )

        #expect(recovered.reasons.contains("ship_stalled") == false)
        #expect(recovered.suggestedActionIds == [])
        #expect(recovered.transport?.status == "healthy")
        #expect(recovered.healthState == "healthy")
        #expect(recovered.severity == "green")
        #expect(recovered.headline == "Longhouse native health is healthy")
        #expect(recovered.attention == nil)
        #expect(recovered.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).promotion == .normal)
    }

    @Test
    func localProjectionDoesNotInferStallFromOfflineOrStaleFacts() {
        let offline = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-08-03T16:00:00Z",
            healthState: "degraded",
            severity: "yellow",
            headline: "Longhouse is retrying while offline",
            reasons: ["reported_offline"],
            suggestedActions: ["Verify network reachability"],
            suggestedActionIds: ["inspect_transport"],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: nil
        )
        let offlineProjection = offline.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: EngineStatusPayload(
                    version: "test", daemonPid: 1, lastShipAt: nil,
                    spoolPendingCount: 1, spoolDeadCount: 0,
                    parseErrorCount1H: 0, diskFreeBytes: nil, isOffline: true,
                    shippingProgress: ShippingProgressSnapshot(
                        pendingWork: true, stalled: true,
                        secondsWithoutProgress: 90, observedAt: "2026-08-03T16:00:00Z"
                    ),
                    recentDeadLetters: [], lastUpdated: nil
                )
            )
        )
        #expect(offlineProjection.reasons.contains("ship_stalled") == false)
        #expect(offlineProjection.suggestedActionIds == ["inspect_transport"])
        #expect(offlineProjection.healthState == "degraded")

        let healthy = presentationSnapshot(sessions: [])
        let stale = healthy.applyingLocalProjection(
            LocalStatusMonitor.Projection(
                sessions: [],
                engine: EngineStatusPayload(
                    version: "test", daemonPid: 1, lastShipAt: nil,
                    spoolPendingCount: 1, spoolDeadCount: 0,
                    parseErrorCount1H: 0, diskFreeBytes: nil, isOffline: false,
                    localProjection: LocalProjectionStatus(
                        version: 1,
                        generatedAt: "2026-01-01T00:00:00Z",
                        enginePulseAt: "2026-01-01T00:00:00Z",
                        lastReconciledAt: nil,
                        reconciliation: nil
                    ),
                    shippingProgress: ShippingProgressSnapshot(
                        pendingWork: true, stalled: true,
                        secondsWithoutProgress: 90, observedAt: "2026-01-01T00:00:00Z"
                    ),
                    recentDeadLetters: [], lastUpdated: nil
                )
            )
        )
        #expect(stale.reasons.contains("ship_stalled") == false)
        #expect(stale.healthState == "healthy")
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
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .normal)
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
    func bundledSetupScriptInstallsNativePair() throws {
        let scriptURL = try #require(Bundle.module.url(forResource: "desktop-app-setup", withExtension: "sh"))
        let script = try String(contentsOf: scriptURL, encoding: .utf8)

        #expect(script.contains("https://get.longhouse.ai/install.sh"))
        #expect(script.contains("longhouse verify-pair"))
        #expect(script.contains("LONGHOUSE_DEVICE_TOKEN"))
        #expect(script.contains("LONGHOUSE_RUNTIME_URL"))
        #expect(script.contains("longhouse auth --url"))
        #expect(script.contains("longhouse machine repair --repair-service"))
        // What history may leave the Mac is chosen before the agent can start.
        #expect(script.contains("longhouse machine scope --prompt"))
        // A first-run Mac has no token in its environment: the script must ask
        // for the Runtime Host and run the browser handshake itself.
        #expect(script.contains("read -r \"url?Longhouse URL: \""))
        #expect(!script.contains("Sign in to a Runtime Host in Longhouse.app"))
        #expect(!script.contains("uv tool"))
        #expect(!script.contains("uv python"))
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
    func projectionFreshnessUsesGeneratedAtInsteadOfEnginePulse() throws {
        let data = Data("""
        {
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Local projection is stale",
          "reasons": ["engine_projection_stale"],
          "suggested_actions": [],
          "engine_status": {
            "fresh": true,
            "age_seconds": 1,
            "payload": {
              "daemon_pid": 42,
              "local_projection": {
                "generated_at": "2026-04-08T01:49:59Z",
                "engine_pulse_at": "2026-04-08T01:52:00Z"
              }
            }
          }
        }
        """.utf8)
        let snapshot = try HealthSnapshotDecoder.decode(data: data)
        let referenceDate = try #require(HealthSnapshot.parseISO8601("2026-04-08T01:52:00Z"))

        #expect(snapshot.engineAgeLabel(relativeTo: referenceDate) == "1s")
        #expect(snapshot.engineFreshnessLabel(relativeTo: referenceDate) == "Stale")
        #expect(snapshot.engineFreshnessValueLabel(relativeTo: referenceDate) == "Stale · 2m")
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
    func decodesAttentionFromCanonicalTransportReason() throws {
        let watchingData = Data("""
        {
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Local upload needs attention",
          "reasons": ["connect_errors"],
          "suggested_actions": [],
          "attention": {
            "state": "watching",
            "headline": "Longhouse is retrying quietly",
            "summary": "Recent local shipping retries need inspection.",
            "reasons": ["connect_errors"],
            "suggested_actions": []
          }
        }
        """.utf8)
        let noAttentionData = Data("""
        {
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Local upload needs attention",
          "reasons": ["connect_errors"],
          "suggested_actions": []
        }
        """.utf8)

        let watching = try HealthSnapshotDecoder.decode(data: watchingData)
        let noAttention = try HealthSnapshotDecoder.decode(data: noAttentionData)

        #expect(watching.attention?.normalizedState == "watching")
        #expect(watching.needsMenuBarAttention == false)
        #expect(watching.effectiveHeadline == "Longhouse is retrying quietly")
        #expect(noAttention.attention == nil)
        #expect(noAttention.needsMenuBarAttention == true)
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
        #expect(snapshot.managedSummaryLabel == "2 sessions · 1 detached")
        #expect(snapshot.currentManagedSessions.count == 2)
    }

    @Test
    func unresolvedSourceFixtureCarriesScopedActionIdentifier() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/immutable-source-unresolved.json")

        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()

        #expect(snapshot.severity == "red")
        #expect(snapshot.suggestedActionIds == ["inspect_storage_source"])
        #expect(snapshot.storageBlockRequiresRepair)
    }

    /// A session that lost its control path must never fold into "quiet", and
    /// only sessions that ask for the user may take the focus card or burn low
    /// amber; working sessions burn, everything else is unlit.
    @Test
    func hearthGroupsSessionsByWhatTheyAskOfTheUser() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/hearth-busy.json")
        let sessions = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load().currentManagedSessions

        #expect(sessions.map(\.hearthKind) == [
            .needsYou, .working, .working, .working, .lostControl, .quiet, .quiet,
        ])
        #expect(sessions.filter { $0.hearthKind.asksForUser }.map(\.sessionId) == ["sess-cad"])
        let executing = sessions[1].hearthHeat, waiting = sessions[0].hearthHeat
        #expect(executing > sessions[2].hearthHeat && sessions[2].hearthHeat > waiting)
        // Lit above 0.12; lost control and quiet are cold ash.
        #expect(waiting > 0.12)
        #expect(sessions[4].hearthHeat <= 0.12 && sessions[5].hearthHeat <= 0.12)
    }

    @Test
    @MainActor
    func hearthDelegatedWorkShowsBackgroundSummaryLeadProgressAndSubagentLinks() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/hearth-delegated-work.json")
        let snapshot = try FixtureHealthSnapshotSource(fileURL: fixtureURL).load()
        let collectedAt = try #require(HealthSnapshot.parseISO8601("2026-04-21T18:35:00Z"))
        let view = panel(snapshot, presentationDate: collectedAt)
        let entries = view.hearthEntries
        let delegated = try #require(entries.first { $0.id == "sess-ux-progress" })

        #expect(delegated.kind == .working)
        // Stale quiescent tool ("Edit") must never override the Background primary label.
        #expect(delegated.subtitle == "longhouse · Background · 2 agents · 1 command")
        #expect(delegated.detail == "Rebasing worktree and rerunning make validate · Bash · +2")
        #expect(delegated.detailSuffix == "Bash · +2")
        #expect(delegated.helpText?.contains("[subagent] Rebasing worktree and rerunning make validate (Bash)") == true)
        #expect(delegated.helpText?.contains("[subagent] Monitoring terminal-truth.yml CI run (Thinking)") == true)
        #expect(delegated.subagentLinks.map(\.title) == [
            "Rebasing worktree and rerunning make validate",
            "Monitoring terminal-truth.yml CI run",
        ])

        let expiredAt = try #require(HealthSnapshot.parseISO8601("2026-04-21T19:06:00Z"))
        let expiredEntry = panel(snapshot, presentationDate: expiredAt)
            .hearthEntries
            .first { $0.id == "sess-ux-progress" }
        #expect(expiredEntry?.detail == nil)
        #expect(expiredEntry?.helpText == nil)
        #expect(expiredEntry?.subagentLinks.isEmpty == true)
    }

    @Test
    func sessionProjectionPreservesAndClearsDelegationAcrossRefreshAndExpiry() throws {
        let localRow = ManagedSessionSnapshot(
            sessionId: "sess-1",
            provider: "claude",
            workspaceLabel: "longhouse",
            branch: "main",
            state: "attached",
            phase: "idle",
            lastActivityAt: "2026-04-21T18:34:00Z",
            bridgeStatus: "running",
            bridgePid: 42,
            bridgeHeartbeatAt: "2026-04-21T18:34:59Z",
            reasonCodes: []
        )
        let delegation = SessionDelegationSnapshot(
            state: "pending",
            count: 5,
            kinds: ["subagent": 2, "shell": 3],
            observedAt: "2026-04-21T18:34:55Z",
            validUntil: "2026-04-21T19:05:00Z",
            items: [
                SessionDelegationTaskSnapshot(
                    id: "a-1",
                    kind: "subagent",
                    status: "running",
                    description: "Run test suite",
                    sessionId: "child-1",
                    nativeProgress: SessionDelegationProgressSnapshot(
                        observedAt: "2026-04-21T18:34:54Z",
                        status: "executing",
                        currentTool: "Bash"
                    )
                ),
            ]
        )
        let projected = localRow.applying(
            SessionProjection(
                sessionId: "sess-1",
                timelineTitle: "Parent session",
                summaryTitle: nil,
                firstUserMessage: nil,
                titleState: "ready",
                titleSource: "user",
                runtimePhase: "working",
                displayPhase: "Background · 2 agents · 3 commands",
                lastActivityAt: "2026-04-21T18:34:55Z",
                source: "runtime_host",
                authority: "runtime_host",
                mode: "helm",
                presentation: SessionPresentationSnapshot(
                    primary: SessionPresentationLabelSnapshot(
                        key: "delegated_work",
                        label: "Background · 2 agents · 3 commands",
                        tone: "neutral"
                    ),
                    signal: SessionSignalSnapshot(state: "working", validUntil: "2026-04-21T19:05:00Z"),
                    access: nil
                ),
                activity: SessionActivitySnapshot(
                    state: "quiescent",
                    rawKind: "idle",
                    tool: "Edit",
                    observedAt: "2026-04-21T18:30:00Z"
                ),
                delegation: delegation
            )
        )

        let now = try #require(HealthSnapshot.parseISO8601("2026-04-21T18:35:00Z"))
        // Even when session_delta caps items at 1..4 while count is 5, "+4" reflects total count.
        #expect(projected.delegatedWorkDetail(asOf: now) == "Run test suite · Bash · +4")

        let preserved = localRow.preservingRuntimeHostProjection(from: projected)
        #expect(preserved.delegation == delegation)

        let finished = preserved.applying(
            SessionProjection(
                sessionId: "sess-1",
                timelineTitle: "Parent session",
                summaryTitle: nil,
                firstUserMessage: nil,
                titleState: "ready",
                titleSource: "user",
                runtimePhase: "idle",
                displayPhase: "Idle",
                lastActivityAt: "2026-04-21T18:36:00Z",
                source: "runtime_host",
                authority: "runtime_host",
                mode: "helm",
                presentation: SessionPresentationSnapshot(
                    primary: SessionPresentationLabelSnapshot(key: "idle", label: "Idle", tone: "neutral"),
                    signal: SessionSignalSnapshot(state: "quiet", validUntil: nil),
                    access: nil
                ),
                delegation: SessionDelegationSnapshot(
                    state: "none",
                    count: 0,
                    kinds: [:],
                    observedAt: "2026-04-21T18:36:00Z",
                    validUntil: "2026-04-21T19:06:00Z",
                    items: []
                )
            )
        )
        #expect(finished.activeDelegationTasks(asOf: now).isEmpty)
        #expect(projected.clearingRuntimeHostProjection().delegation == nil)
    }

    @Test
    @MainActor
    func hearthAnimationRespectsDesktopFrameBudget() {
        #expect(HearthClock.frameInterval >= 0.1)
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
        #expect(snapshot.managedSummaryLabel == "2 sessions")
        let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 1_715_648_400))
        #expect(presentation.headline == "2 sessions open")
        #expect(terminal.launchMode == "tui")
        #expect(terminal.uiAttached == true)
        #expect(terminal.normalizedUIPresence == "foreground_tui")
        #expect(terminal.isConsoleManagedSession == false)
        #expect(terminal.needsManagedSessionAttention == false)
        #expect(terminal.isBackgroundManagedSession == false)
        #expect(!(terminal.authority == "runtime_host" && terminal.control?.actions.terminate?.state == "available"))
        #expect(background.launchMode == "detached_ui")
        #expect(background.uiAttached == false)
        #expect(background.normalizedUIPresence == "background")
        #expect(background.isConsoleManagedSession == true)
        #expect(background.needsManagedSessionAttention == false)
        #expect(background.isBackgroundManagedSession == true)
        #expect(!(background.authority == "runtime_host" && background.control?.actions.terminate?.state == "available"))
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
        #expect(HealthSnapshot.providerDisplayName("opencode") == "OpenCode")
        #expect(HealthSnapshot.providerDisplayName("gemini") == "Antigravity")
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
        #expect(session.menuBarAttentionKind == .phaseUnavailable)
        #expect(session.rawPhase == "future_magic")
    }

    @Test
    func aLastObservedSessionReadsUnknownWithoutPromotingTheMenuBar() throws {
        let json = """
        {
          "health_state": "healthy",
          "severity": "green",
          "headline": "Longhouse shipping healthy",
          "reasons": [],
          "suggested_actions": [],
          "managed_sessions": [
            {
              "session_id": "sess-quiet",
              "provider": "cursor",
              "state": "attached",
              "presentation": {
                "primary": {
                  "key": "no_recent_activity",
                  "label": "Last observed idle",
                  "tone": "quiet"
                },
                "signal": {"state": "unknown", "valid_until": null}
              }
            }
          ]
        }
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let snapshot = try decoder.decode(HealthSnapshot.self, from: Data(json.utf8))
        let session = try #require(snapshot.currentManagedSessions.first)
        #expect(session.menuBarAttentionKind == .unknown("no_recent_activity"))
        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .normal)
    }

    @Test
    func theMenuBarDrawsTheServedSignalOnItsOwnClock() throws {
        func session(_ key: String, _ state: String, validUntil: String? = nil) -> ManagedSessionSnapshot {
            ManagedSessionSnapshot(
                sessionId: key, provider: "claude", workspaceLabel: "zerg",
                branch: "main", state: "attached", phase: nil,
                lastActivityAt: nil, bridgeStatus: nil, bridgePid: nil,
                bridgeHeartbeatAt: nil, reasonCodes: [], authority: "runtime_host",
                presentation: SessionPresentationSnapshot(
                    primary: SessionPresentationLabelSnapshot(key: key, label: key, tone: "blocked"),
                    signal: SessionSignalSnapshot(state: state, validUntil: validUntil),
                    access: nil
                )
            )
        }
        let now = try #require(HealthSnapshot.parseISO8601("2026-10-07T12:00:00Z"))
        #expect(session("needs_answer", "attention").menuBarAttentionKind(asOf: now) == .needsYou)
        // A failed launch or a provider sign-in is owed by the user, not unknown.
        #expect(session("launch_failed", "attention").menuBarAttentionKind(asOf: now) == .blocked)
        #expect(session("provider_auth_required", "attention").menuBarAttentionKind(asOf: now) == .blocked)
        #expect(session("delegated_work", "working").menuBarAttentionKind(asOf: now) == .working)
        #expect(
            session("executing", "working", validUntil: "2026-10-07T12:01:00Z").menuBarAttentionKind(asOf: now) == .working
        )
        #expect(
            session("executing", "working", validUntil: "2026-10-07T11:59:00Z").menuBarAttentionKind(asOf: now)
                == .unknown("activity unknown")
        )
        // A host or cache without the served signal is missing evidence:
        // unknown, never idle, whatever the working headline says.
        let unsignalled = ManagedSessionSnapshot(
            sessionId: "executing", provider: "claude", workspaceLabel: "zerg",
            branch: "main", state: "attached", phase: nil,
            lastActivityAt: nil, bridgeStatus: nil, bridgePid: nil,
            bridgeHeartbeatAt: nil, reasonCodes: [], authority: "runtime_host",
            presentation: SessionPresentationSnapshot(
                primary: SessionPresentationLabelSnapshot(key: "executing", label: "Using Bash", tone: "active"),
                signal: nil,
                access: nil
            )
        )
        #expect(unsignalled.menuBarAttentionKind(asOf: now) == .unknown("executing"))
        #expect(session("idle", "quiet").menuBarAttentionKind(asOf: now) == .idle)
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
    func attachedManagedSessionWithoutPhaseStaysUnknown() {
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

        #expect(session.menuBarAttentionKind == .phaseUnavailable)
        #expect(session.normalizedUIPresence == nil)
        #expect(session.isConsoleManagedSession == false)
        #expect(session.needsManagedSessionAttention == false)
        #expect(!(session.authority == "runtime_host" && session.control?.actions.terminate?.state == "available"))
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
        #expect(session.menuBarAttentionKind == .phaseUnavailable)
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
        #expect(session.menuBarAttentionKind == .phaseUnavailable)
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
        #expect(session.menuBarAttentionKind == .phaseUnavailable)
        #expect(!(session.authority == "runtime_host" && session.control?.actions.terminate?.state == "available"))
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

        #expect(session.menuBarAttentionKind == .phaseUnavailable)
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
        #expect(session.menuBarAttentionKind == .phaseUnavailable)
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
    func nativeRedWithoutRowLevelExceptionPromotesRepair() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1,
            collectedAt: "2026-04-22T03:00:00Z",
            healthState: "broken",
            severity: "red",
            headline: "Longhouse reported a new machine failure",
            reasons: [],
            suggestedActions: [],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            managedSummary: nil,
            managedSessions: [],
            orphanBridges: [],
            launchReadiness: nil
        )

        #expect(snapshot.menuBarPresentation(relativeTo: Date()).promotion == .repair)
    }

    private func makeFakeHomeDirectory() throws -> URL {
        let tempDirectory = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
        return tempDirectory
    }

    private func installFakeLonghouseBinary(
        homeDirectory: URL,
        script: String = "#!/bin/sh\nexit 0\n"
    ) throws -> URL {
        let binDirectory = homeDirectory.appendingPathComponent(".local/bin", isDirectory: true)
        try FileManager.default.createDirectory(at: binDirectory, withIntermediateDirectories: true)
        let executableURL = binDirectory.appendingPathComponent("longhouse", isDirectory: false)
        let contents = Data(script.utf8)
        FileManager.default.createFile(atPath: executableURL.path, contents: contents)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: executableURL.path
        )
        return executableURL
    }

    // MARK: - Producer freshness
    //
    // Regression coverage for the 2026-07-23 incident: the configured health
    // exec was deleted, every refresh failed for eleven days, and the panel kept
    // rendering its last-good cache as if it were current.

    @Test
    @MainActor
    func cachedSnapshotWithFailingProducerIsNeverPresentedAsCurrent() async throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let cacheURL = tempDir.appendingPathComponent("last-good.json")
        try writeCachedSnapshot(headline: "Cached Longhouse status", to: cacheURL)

        // Exactly the incident shape: --health-exec points at a path that does
        // not exist, so Process.run() throws before the command ever starts.
        let store = SnapshotStore(
            source: CLIHealthSnapshotSource(
                launchPath: tempDir.appendingPathComponent("longhouse-local-health").path,
                arguments: ["--json"]
            ),
            cacheURL: cacheURL,
            transientRetryDelay: 0.01
        )

        for _ in 0..<200 where store.loadError == nil {
            try? await Task.sleep(for: .milliseconds(10))
        }

        // The cache still renders — it is useful evidence.
        #expect(store.snapshot?.headline == "Cached Longhouse status")
        // But it must never be mistaken for current data.
        #expect(store.dataTrust().isCurrent == false)
        guard case let .neverLoaded(failure) = store.dataTrust() else {
            Issue.record("expected .neverLoaded, got \(store.dataTrust())")
            return
        }
        // The user has to be able to see which command is broken.
        #expect(failure?.command?.contains("longhouse-local-health") == true)
    }

    /// Cached payloads cannot establish freshness after producer failure.
    @Test
    @MainActor
    func freshnessFactStopsClaimingRecencyWhenTrustIsNotCurrent() {
        func facts(trust: DataTrust) -> [MenuBarSystemFact] {
            MenuBarPanelView(
                snapshot: makeHealthySnapshot(),
                history: [],
                presentationDate: Date(timeIntervalSince1970: 0),
                feedback: nil,
                setFeedback: { _ in },
                actionSink: SpyHealthActionSink(logURL: nil, uiURL: nil),
                isManualRefreshing: false,
                dataTrust: trust,
                refresh: {}
            ).displayedFacts
        }


        for trust in [
            DataTrust.neverLoaded(failure: nil),
            DataTrust.lastKnown(LastKnownContext(lastSuccessAt: Date(timeIntervalSince1970: 0), failure: nil)),
        ] {
            guard let freshness = facts(trust: trust).first(where: { $0.id == "freshness" }) else {
                Issue.record("freshness fact missing for \(trust)")
                continue
            }
            #expect(freshness.promotion == .unavailable)
        }

    }

    @MainActor
    private func panel(
        _ snapshot: HealthSnapshot,
        presentationDate: Date = Date(timeIntervalSince1970: 0)
    ) -> MenuBarPanelView {
        MenuBarPanelView(
            snapshot: snapshot,
            history: [],
            presentationDate: presentationDate,
            feedback: nil,
            setFeedback: { _ in },
            actionSink: SpyHealthActionSink(logURL: nil, uiURL: nil),
            isManualRefreshing: false,
            refresh: {}
        )
    }

    /// Session attention outranks system facts in the reducer, so the health
    /// line must read the facts itself: a session waiting on the user cannot
    /// turn an offline transport into "Connected".
    @Test
    @MainActor
    func healthLineReportsOfflineTransportWhileASessionNeedsTheUser() throws {
        let snapshot = presentationSnapshot(
            reasons: ["reported_offline"],
            sessions: [presentationSession(phase: "needs permission")],
            isOffline: true
        )
        #expect(snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).promotion == .needsUser)

        let view = panel(snapshot)
        #expect(view.displayedFacts.first { $0.id == "transport" }?.value == "Offline")
        let summary = view.healthSummary
        #expect(summary.promotion != nil)
        #expect(!summary.text.hasPrefix("Connected"))
    }

    /// A system warning with no matching fact still owns the health line,
    /// including while a session waiting on the user owns the header.
    @Test
    @MainActor
    func healthLineCarriesWarningsThatHaveNoFact() {
        for sessions in [[], [presentationSession(phase: "needs permission")]] {
            let snapshot = presentationSnapshot(reasons: ["managed_launch_recovery_exhausted"], sessions: sessions)
            let presentation = snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0))
            #expect(presentation.promotion == (sessions.isEmpty ? .inspect : .needsUser))
            #expect(presentation.systemPromotion == .inspect)

            let summary = panel(snapshot).healthSummary
            #expect(summary.promotion == .inspect)
            #expect(summary.text == "Managed session recovery needs attention")
        }
    }

    /// Unknown storage proof is only an inspect promotion; its scoped action
    /// lives on the trouble card, so the card has to show.
    @Test
    @MainActor
    func storageProofUnknownKeepsItsInspectAction() {
        let snapshot = presentationSnapshot(
            reasons: ["storage_v2_sources_proof_unknown"],
            sessions: [],
            suggestedActionIds: ["inspect_storage_source"]
        )
        #expect(snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).promotion == .inspect)
        #expect(panel(snapshot).showsTroubleCard)
    }

    @Test
    @MainActor
    func producerSuccessThenFailureDegradesToLastKnown() async throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)

        // Succeeds on the first load, fails on every load after it.
        let source = FlakyHealthSnapshotSource(snapshot: makeHealthySnapshot())
        let store = SnapshotStore(
            source: source,
            cacheURL: tempDir.appendingPathComponent("last-good.json"),
            transientRetryDelay: 0.01
        )
        #expect(store.dataTrust().isCurrent)

        source.startFailing()
        store.refresh(reason: .manual)
        for _ in 0..<200 where store.loadError == nil {
            try? await Task.sleep(for: .milliseconds(10))
        }

        guard case let .lastKnown(context) = store.dataTrust() else {
            Issue.record("expected .lastKnown, got \(store.dataTrust())")
            return
        }
        #expect(context.lastSuccessAt != nil)
        #expect(context.failure?.command == "flaky-test-source")
        // The snapshot from the successful load is still on screen — the point
        // is that it is now labelled last-known rather than silently current.
        #expect(store.snapshot?.headline == "Longhouse shipping healthy")
    }

    @Test
    func producerTrustIgnoresPayloadClocksAndDecaysOnDeadline() {
        let lastSuccess = Date(timeIntervalSince1970: 1_000_000)
        let state = ProducerRefreshState(lastSuccessAt: lastSuccess, latestFailure: nil)

        // Within deadline and no reported failure: current.
        #expect(state.trust(relativeTo: lastSuccess.addingTimeInterval(30), deadline: 120) == .current)

        // A producer that simply goes quiet still decays. This is the backstop for
        // a wedged refresh that never reports a failure at all.
        let decayed = state.trust(relativeTo: lastSuccess.addingTimeInterval(300), deadline: 120)
        #expect(decayed.isCurrent == false)
    }

    @Test
    @MainActor
    func repeatedTransientTimeoutsStopCountingAsRecovering() async throws {
        let tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)

        // A hung producer reports .timedOut every attempt, and .timedOut is
        // transient. Gating the banner on isRecovering alone would keep a
        // permanently broken producer looking normal forever.
        let store = SnapshotStore(
            source: CLIHealthSnapshotSource(
                launchPath: "/bin/zsh",
                arguments: ["-lc", "sleep 5"],
                commandTimeoutSeconds: 0.05
            ),
            cacheURL: tempDir.appendingPathComponent("last-good.json"),
            transientRetryDelay: 0.01
        )

        for _ in 0..<300 where store.refreshState.consecutiveFailures < 2 {
            try? await Task.sleep(for: .milliseconds(10))
        }

        #expect(store.refreshState.consecutiveFailures >= 2)
        #expect(store.isRecovering)
        // Still retrying, but no longer allowed to hide behind it.
        #expect(store.isBrieflyRecovering == false)
        #expect(store.dataTrust().isCurrent == false)
    }

    @Test
    func transientGraceCoversOnlyTheFirstFailure() {
        let failure = ProducerRefreshFailure(message: "timeout", command: nil, observedAt: Date())
        var state = ProducerRefreshState.neverAttempted.recordingSuccess(at: Date())
        #expect(state.isWithinTransientGrace)

        state = state.recordingFailure(failure)
        #expect(state.consecutiveFailures == 1)
        #expect(state.isWithinTransientGrace)

        state = state.recordingFailure(failure)
        #expect(state.consecutiveFailures == 2)
        #expect(state.isWithinTransientGrace == false)

        // A success clears the streak.
        state = state.recordingSuccess(at: Date())
        #expect(state.consecutiveFailures == 0)
        #expect(state.isWithinTransientGrace)
    }

    @Test
    func absentEvidenceRendersUnknownRatherThanHealthy() {
        // No engine payload at all: the producer could not read upload or
        // transport evidence. Rendering "Clear" and "Connected" from that would
        // assert a healthy state nobody observed.
        let blind = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-03T16:00:00Z",
            healthState: "healthy", severity: "green", headline: "Longhouse",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, launchReadiness: nil
        )
        let facts = blind.menuBarPresentation(relativeTo: Date()).facts
        let value = { (id: String) in facts.first(where: { $0.id == id })?.value }

        #expect(value("durable-upload") == "Unknown")
        #expect(value("transport") == "Unknown")
        #expect(facts.first(where: { $0.id == "transport" })?.promotion == .unavailable)
    }

    @Test
    func unreadableStorageOutboxDoesNotRenderDurableFactAsClear() {
        let snapshot = presentationSnapshot(
            reasons: ["storage_v2_outbox_unreadable"],
            sessions: []
        )
        let facts = snapshot.menuBarPresentation(relativeTo: Date()).facts
        let durable = facts.first(where: { $0.id == "durable-upload" })

        #expect(durable?.value == "Unknown")
        #expect(durable?.promotion == .repair)
    }

    @Test
    func absentSessionEvidenceIsNotAnObservedAbsence() {
        let blind = makeHealthySnapshot(sessions: nil)
        let observed = makeHealthySnapshot(sessions: [])

        // nil means "could not read"; [] means "read, and there were none".
        #expect(blind.managedSessions == nil)
        #expect(observed.managedSessions?.isEmpty == true)
    }

    @Test
    func singleSessionProjectionFailureClearsOnlyThatSession() {
        let runtimeHost = { (id: String) in
            ManagedSessionSnapshot(
                sessionId: id, provider: "codex", workspaceLabel: "longhouse",
                branch: "main", state: "attached", phase: "thinking",
                lastActivityAt: nil, bridgeStatus: nil, bridgePid: nil,
                bridgeHeartbeatAt: nil, reasonCodes: [], authority: "runtime_host",
                presentation: SessionPresentationSnapshot(
                    primary: SessionPresentationLabelSnapshot(
                        key: "thinking", label: "Thinking", tone: "active"
                    ),
                    signal: SessionSignalSnapshot(state: "working", validUntil: nil),
                    access: nil
                )
            )
        }
        let snapshot = makeHealthySnapshot(sessions: [runtimeHost("a"), runtimeHost("b")])

        let cleared = snapshot.clearingRuntimeHostProjection(for: "a")
        let sessions = cleared.managedSessions ?? []

        #expect(sessions.first(where: { $0.sessionId == "a" })?.presentation == nil)
        // The healthy session keeps its Runtime Host authority.
        #expect(sessions.first(where: { $0.sessionId == "b" })?.presentation != nil)
    }

    @Test
    func missingOrphanEvidenceIsNotZero() {
        // The native producer does not scan for orphaned bridges. Collapsing
        // that silence to zero would claim "no cleanup needed" without looking,
        // and silently drop the cleanup section.
        let blind = makeHealthySnapshot(sessions: [])
        #expect(blind.observedOrphanBridgeCount == nil)
        #expect(blind.orphanBridgeEvidenceMissing)

        let observed = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-03T16:00:00Z",
            healthState: "healthy", severity: "green", headline: "Longhouse",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil,
            managedSummary: ManagedSummarySnapshot(
                attachedCount: 1, detachedCount: 0, degradedCount: 0,
                orphanBridgeCount: 0, latestActivityAt: nil
            ),
            launchReadiness: nil
        )
        // A producer that looked and found none is a real zero.
        #expect(observed.observedOrphanBridgeCount == 0)
        #expect(observed.orphanBridgeEvidenceMissing == false)
    }

    @Test
    func flappingStreamCannotSuppressFailureIndefinitely() {
        // The bug this pins: replenishing the grace inside the same drop that
        // consumes it. A stream that emits one line and closes, over and over,
        // would forgive itself forever and never report.
        var policy = ProjectionRetryPolicy()

        let first = policy.recordDrop(carriedTraffic: true, duration: 2)
        #expect(first.outcome == .retryQuietly)
        #expect(first.resetBackoff)

        let second = policy.recordDrop(carriedTraffic: true, duration: 2)
        #expect(second.outcome == .reportFailure)

        let third = policy.recordDrop(carriedTraffic: true, duration: 2)
        #expect(third.outcome == .reportFailure)
    }

    @Test
    func healthyLongLivedStreamClosesQuietlyEveryTime() {
        var policy = ProjectionRetryPolicy()
        let long = ProjectionRetryPolicy.healthySessionSeconds + 1

        // Servers rotate long-lived SSE connections. Each ordinary close must
        // stay silent, not accumulate toward a reported failure.
        for _ in 0..<5 {
            let decision = policy.recordDrop(carriedTraffic: true, duration: long)
            #expect(decision.outcome == .retryQuietly)
            #expect(decision.resetBackoff)
        }
        #expect(policy.consecutiveFailures == 0)
    }

    @Test
    func streamThatNeverCarriedTrafficReportsImmediately() {
        var policy = ProjectionRetryPolicy()

        // An immediate 200-then-EOF, or a connection that never opened, gets no
        // grace at all.
        #expect(policy.recordDrop(carriedTraffic: false, duration: 0).outcome == .reportFailure)
        #expect(policy.recordDrop(carriedTraffic: false, duration: 0).outcome == .reportFailure)
    }

    @Test
    func healthySessionClearsAnEarlierFlapStreak() {
        var policy = ProjectionRetryPolicy()
        _ = policy.recordDrop(carriedTraffic: true, duration: 2)
        _ = policy.recordDrop(carriedTraffic: true, duration: 2)
        #expect(policy.consecutiveFailures == 2)

        // Recovering into a genuinely healthy session resets the streak, so a
        // later isolated blip is forgiven again.
        let recovered = policy.recordDrop(
            carriedTraffic: true,
            duration: ProjectionRetryPolicy.healthySessionSeconds + 1
        )
        #expect(recovered.outcome == .retryQuietly)
        #expect(policy.consecutiveFailures == 0)
        #expect(policy.recordDrop(carriedTraffic: true, duration: 2).outcome == .retryQuietly)
    }

    @Test
    func neverAttemptedProducerIsNeverCurrent() {
        let state = ProducerRefreshState.neverAttempted
        #expect(state.trust(relativeTo: Date(), deadline: 120).isCurrent == false)
    }

    @Test
    func expiredProjectionClearsRuntimeHostFieldsButKeepsLocalRows() {
        let session = ManagedSessionSnapshot(
            sessionId: "s1", provider: "codex", workspaceLabel: "longhouse",
            branch: "main", state: "attached", phase: "thinking",
            lastActivityAt: "2026-08-03T12:00:00Z", bridgeStatus: "ready",
            bridgePid: 42, bridgeHeartbeatAt: "2026-08-03T12:00:00Z",
            reasonCodes: [], authority: "runtime_host",
            presentation: SessionPresentationSnapshot(
                primary: SessionPresentationLabelSnapshot(
                    key: "thinking", label: "Thinking", tone: "active"
                ),
                signal: SessionSignalSnapshot(state: "working", validUntil: nil),
                access: nil
            ),
            activity: SessionActivitySnapshot(
                state: "active", rawKind: nil, tool: nil, observedAt: nil
            ),
            control: SessionControlSnapshot(
                ownership: "runtime_host", connection: "connected",
                actions: SessionControlActionsSnapshot(terminate: nil, reattach: nil)
            )
        )
        let snapshot = makeHealthySnapshot(sessions: [session])

        let cleared = snapshot.markingRuntimeHostProjectionUnavailable()
        let clearedSession = try? #require(cleared.managedSessions?.first)

        // Runtime Host authority is gone...
        #expect(clearedSession?.presentation == nil)
        #expect(clearedSession?.activity == nil)
        #expect(clearedSession?.control == nil)
        #expect(clearedSession?.authority == nil)
        // ...but the session itself is still visible as local evidence.
        #expect(clearedSession?.sessionId == "s1")
        #expect(clearedSession?.state == "attached")
        #expect(clearedSession?.bridgePid == 42)
    }

    /// The panel must name the Runtime Host it is connected to.
    ///
    /// `hostValueLabel` used to read `launch_readiness.stored_url`, which the
    /// native producer does not emit, so it returned "-" on every real machine
    /// and the "Remote control" fact quietly dropped its host. The fixtures
    /// still carried the retired key, so the rendered fixture looked more
    /// complete than the live product -- the failure was invisible exactly
    /// where it would have been caught.
    @Test
    func hostLabelComesFromTheRealtimeBlockTheProducerActuallyEmits() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-03T20:00:00Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: nil,
            realtime: RealtimeConnectionSnapshot(
                runtimeUrl: "https://david010.longhouse.ai",
                machineName: "cinder",
                tokenPath: nil
            ),
            launchReadiness: nil
        )

        #expect(snapshot.hostValueLabel == "david010")
    }

    @Test
    func hostLabelIsPlaceholderWhenNoRuntimeUrlIsKnown() {
        let snapshot = HealthSnapshot(
            schemaVersion: 1, collectedAt: "2026-08-03T20:00:00Z",
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: nil,
            realtime: nil, launchReadiness: nil
        )

        #expect(snapshot.hostValueLabel == "-")
    }

    /// The consumer-contract test: the recorded envelope decodes into the type
    /// the app actually uses.
    ///
    /// The producer end is held by the Rust test
    /// `desktop_envelope_matches_the_swift_consumer_fixture`, which compares the
    /// full recursive shape of the emitted envelope against this same fixture
    /// and is built from current source every run. Producer drift fails there,
    /// consumer drift fails here, and neither side needs a binary to be right.
    ///
    /// This deliberately does not go hunting for a built facade. It used to try
    /// `engine/target/{debug,release}/longhouse`, which was unsound in both
    /// directions: `longhouse local-health` shells out to the *paired*
    /// `longhouse-engine`, so an executable facade can sit next to a stale
    /// engine and emit last month's envelope. That either fails as a bogus
    /// `keyNotFound: severity` that reads like a product regression, or -- worse
    /// -- passes, reporting a live producer as verified when what really got
    /// checked was a binary nobody built on purpose. `verify-pair` does not
    /// rescue it either: it proves the facade and engine match each other, not
    /// that either matches the source tree, so a uniformly stale pair sails
    /// through. Whether the tree happens to be built is not this test's subject.
    ///
    /// Set `LONGHOUSE_HEALTH_BIN` to also run a specific binary end to end. That
    /// is opt-in because only the caller knows the binary is worth trusting.
    @Test
    func nativeProducerOutputDecodesIntoHealthSnapshot() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/native-desktop-health.json")
        let recorded = try HealthSnapshotDecoder.decode(data: Data(contentsOf: fixtureURL))
        #expect(recorded.severity == "green")
        #expect(recorded.managedSessions?.count == 1)
        #expect(recorded.managedSessions?.first?.normalizedUIPresence == "foreground_tui")
        #expect(recorded.realtime?.runtimeUrl != nil)
        #expect(recorded.transport?.status == "healthy")
        #expect(recorded.engineStatus?.payload != nil)
        let presentation = recorded.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 1_785_772_800))
        #expect(presentation.promotion == .inspect)
        #expect(presentation.headline == "Session discovery needs attention")

        guard let binary = ProcessInfo.processInfo.environment["LONGHOUSE_HEALTH_BIN"],
              FileManager.default.isExecutableFile(atPath: binary)
        else {
            return
        }

        let source = CLIHealthSnapshotSource(
            launchPath: binary,
            arguments: ["local-health", "--json"],
            commandTimeoutSeconds: 10
        )
        // The assertion is simply that this does not throw. A producer whose
        // output the app cannot decode fails here instead of in the menu bar.
        let snapshot = try source.load()
        #expect(snapshot.severity.isEmpty == false)
    }
    @Test
    func malformedStorageCounterKeepsNativeEnvelopeInspectable() throws {
        let data = Data("""
        {
          "schema_version": 1,
          "collected_at": "2026-08-04T20:00:00Z",
          "health_state": "degraded",
          "severity": "yellow",
          "headline": "Longhouse native health needs attention",
          "reasons": ["storage_v2_sources_proof_unknown"],
          "suggested_actions": ["Update Longhouse and inspect the retained source evidence."],
          "suggested_action_ids": ["inspect_storage_source"],
          "engine_status": {
            "path": "/tmp/engine-status.json",
            "exists": true,
            "fresh": true,
            "age_seconds": 1,
            "payload": {
              "storage_v2_outbox": {
                "blocked_source_count": "malformed",
                "unresolved_blocked_source_count": 0
              }
            }
          }
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)

        #expect(snapshot.storageBlockProofUnknown)
        #expect(snapshot.storageBlockRequiresRepair == false)
        #expect(snapshot.suggestedActionIds == ["inspect_storage_source"])
    }

    @Test
    func nonObjectStorageOutboxKeepsNativeEnvelopeActionable() throws {
        let data = Data("""
        {
          "schema_version": 1,
          "collected_at": "2026-08-04T20:00:00Z",
          "health_state": "broken",
          "severity": "red",
          "headline": "Source upload state unavailable",
          "reasons": ["storage_v2_outbox_unreadable"],
          "suggested_actions": ["Inspect the storage-v2 outbox error in engine-status.json."],
          "suggested_action_ids": ["inspect_storage_outbox"],
          "engine_status": {
            "exists": true,
            "fresh": true,
            "age_seconds": 1,
            "payload": {"storage_v2_outbox": "unreadable"}
          }
        }
        """.utf8)

        let snapshot = try HealthSnapshotDecoder.decode(data: data)

        #expect(snapshot.engineStatus?.payload?.storageV2Outbox?.malformedCounter == true)
        #expect(snapshot.menuBarPresentation(relativeTo: Date(timeIntervalSince1970: 0)).promotion == .repair)
    }

    @Test
    func projectionWithoutRuntimeHostAuthorityIsLeftAlone() {
        let localOnly = ManagedSessionSnapshot(
            sessionId: "s2", provider: "claude", workspaceLabel: "longhouse",
            branch: "main", state: "attached", phase: nil,
            lastActivityAt: nil, bridgeStatus: nil, bridgePid: nil,
            bridgeHeartbeatAt: nil, reasonCodes: []
        )
        let snapshot = makeHealthySnapshot(sessions: [localOnly])

        let cleared = snapshot.markingRuntimeHostProjectionUnavailable()

        #expect(cleared.managedSessions?.first?.sessionId == "s2")
        #expect(cleared.managedSessions?.first?.state == "attached")
    }

}

@MainActor
private func writeCachedSnapshot(headline: String, to url: URL) throws {
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    try encoder.encode(makeHealthySnapshot(headline: headline)).write(to: url)
}

private func makeHealthySnapshot(
    headline: String = "Longhouse shipping healthy",
    sessions: [ManagedSessionSnapshot]? = nil
) -> HealthSnapshot {
    return HealthSnapshot(
        schemaVersion: 1,
        collectedAt: "2026-05-05T12:00:00Z",
        healthState: "healthy",
        severity: "green",
        headline: headline,
        reasons: [],
        suggestedActions: [],
        service: nil,
        engineStatus: nil,
        outbox: nil,
        activitySummary: nil,
        managedSessions: sessions,
        launchReadiness: nil
    )
}

private func presentationSession(
    phase: String,
    control: SessionControlSnapshot? = nil
) -> ManagedSessionSnapshot {
    ManagedSessionSnapshot(
        sessionId: UUID().uuidString, provider: "codex", workspaceLabel: "longhouse",
        timelineTitle: "Review menu bar state", branch: "main", state: "attached",
        phase: phase, lastActivityAt: "1970-01-01T00:00:00Z", bridgeStatus: "ready",
        bridgePid: 42, bridgeHeartbeatAt: "1970-01-01T00:00:00Z",
        launchMode: "tui", uiAttached: true, uiPresence: "foreground_tui", reasonCodes: [],
        authority: control == nil ? nil : "runtime_host", control: control
    )
}

private func presentationSnapshot(
    reasons: [String] = [],
    sessions: [ManagedSessionSnapshot],
    severity: String = "green",
    archive: ArchiveBacklogStatus? = nil,
    storageBlocked: Int = 0,
    storageBlockKind: String? = nil,
    storageUnresolved: Int? = nil,
    storagePending: Int = 0,
    shippingProgress: ShippingProgressSnapshot? = nil,
    suggestedActionIds: [String] = [],
    isOffline: Bool = false,
    engineFresh: Bool = true,
    localProjection: LocalProjectionStatus? = nil,
    serviceStatus: String? = "running",
    heartbeatTransport: NativeHeartbeatTransportSnapshot? = nil,
    hostLink: HostLinkSnapshot? = nil
) -> HealthSnapshot {
    let resolvedStorageUnresolved = storageUnresolved ?? (
        storageBlockKind == "source_epoch_conflict" || storageBlockKind == "render_generation_revision_conflict"
            ? 0
            : storageBlockKind == "source_epoch_conflict_unresolved" ? 1 : nil
    )

    return HealthSnapshot(
        schemaVersion: 1, collectedAt: "1970-01-01T00:00:00Z",
        healthState: "healthy", severity: severity, headline: "Healthy",
        reasons: reasons, suggestedActions: [], suggestedActionIds: suggestedActionIds,
        service: serviceStatus.map { status in ServiceSnapshot(
            platform: "macos", status: status, serviceName: "com.longhouse.shipper",
            serviceFile: nil, logPath: nil
        ) },
        engineStatus: EngineStatusSnapshot(
            path: nil, exists: true, fresh: engineFresh, ageSeconds: engineFresh ? 1 : 600,
            payload: EngineStatusPayload(
                version: "test", daemonPid: 1, lastShipAt: "1970-01-01T00:00:00Z",
                spoolPendingCount: 0, spoolDeadCount: 0, archiveBacklog: archive,
                storageV2Outbox: StorageV2OutboxStatus(
                    pendingCount: storagePending, pendingBytes: 0, blockedSourceCount: storageBlocked,
                    unresolvedBlockedSourceCount: resolvedStorageUnresolved,
                    blockedBytes: 0, latestBlockKind: storageBlockKind, latestBlockDetail: nil,
                    byteLimit: 1_073_741_824, error: nil
                ),
                parseErrorCount1H: 0, diskFreeBytes: nil,
                isOffline: isOffline, localProjection: localProjection, shippingProgress: shippingProgress,
                recentDeadLetters: [], lastUpdated: "1970-01-01T00:00:00Z"
            ),
            error: nil
        ),
        outbox: OutboxSnapshot(path: nil, fileCount: 0, oldestAgeSeconds: nil),
        activitySummary: nil,
        managedSessions: sessions,
        heartbeatTransport: heartbeatTransport,
        launchReadiness: nil,
        hostLink: hostLink
    )
}
private func presentationHostLink(
    state: String,
    claimStartedAt: String? = nil,
    deadline: String? = nil,
    cutoff: String? = nil,
    lastAcknowledgedAt: String? = nil,
    freshHorizonSecs: Int? = 120
) -> HostLinkSnapshot {
    let claim = state == "updating" || state == "slow_update"
        ? HostLifecycleSnapshot(
            type: "host.lifecycle",
            state: "updating",
            expectedBackBy: state == "updating"
                ? "1970-01-01T01:00:00Z"
                : "1970-01-01T00:00:30Z",
            deadline: deadline ?? "1970-01-01T01:00:00Z",
            cutoff: cutoff ?? "1970-01-01T02:00:00Z"
        )
        : nil
    return HostLinkSnapshot(
        state: state,
        since: claimStartedAt ?? "1970-01-01T00:00:00Z",
        claim: claim,
        claimStartedAt: claimStartedAt,
        lastAcknowledgedAt: lastAcknowledgedAt ?? claimStartedAt,
        freshHorizonSecs: freshHorizonSecs,
        runtimeEpoch: "runtime-test"
    )
}

private func presentationHeartbeatFailure() -> NativeHeartbeatTransportSnapshot {
    NativeHeartbeatTransportSnapshot(
        state: "degraded",
        evidenceState: "applied",
        lastAttemptAt: "1970-01-01T00:00:04Z",
        lastSuccessAt: "1970-01-01T00:00:02Z",
        lastFailureAt: "1970-01-01T00:00:04Z",
        lastError: "POST timed out"
    )
}

private func presentationHeartbeatSuccess(lastSuccessAt: String) -> NativeHeartbeatTransportSnapshot {
    NativeHeartbeatTransportSnapshot(
        state: "healthy",
        evidenceState: "applied",
        lastAttemptAt: lastSuccessAt,
        lastSuccessAt: lastSuccessAt,
        lastFailureAt: nil,
        lastError: nil
    )
}

private func hostLinkSchemaCopy(_ key: String) throws -> String {
    let repositoryRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let schemaURL = repositoryRoot.appendingPathComponent("schemas/host_link.yml")
    let contents = try String(contentsOf: schemaURL, encoding: .utf8)
    let prefix = "  \(key): "
    guard let line = contents.split(separator: "\n").first(where: { $0.hasPrefix(prefix) }) else {
        throw NSError(domain: "HostLinkSchema", code: 1)
    }
    return String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
}
