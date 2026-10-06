import Foundation
import Testing

@testable import Longhouse

struct TimelineConnectivityTests {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    @Test
    func activeEOFChurnWithSnapshotRecoveryDoesNotShowWarning() {
        var state = loadedFreshState()

        for offset in [1.0, 2.0, 3.0] {
            state.apply(.streamDisconnected(.serverEOF), now: now.addingTimeInterval(offset))
            #expect(state.reachability == .reachable)
            #expect(state.banner(at: now.addingTimeInterval(offset)) == .none)

            state.apply(.streamSignal(.reconnected), now: now.addingTimeInterval(offset + 0.1))
            #expect(state.banner(at: now.addingTimeInterval(offset + 0.1)) == .none)

            state.apply(.snapshotSucceeded(hasLoadedData: true), now: now.addingTimeInterval(offset + 0.2))
            #expect(state.reachability == .reachable)
            #expect(state.consecutiveSnapshotFailures == 0)
            #expect(state.banner(at: now.addingTimeInterval(offset + 0.2)) == .none)
        }
    }

    @Test
    func repeatedStreamCancellationsWhileSnapshotsSucceedDoNotShowWarning() {
        var state = loadedFreshState()

        state.apply(.streamDisconnected(.cancelled), now: now.addingTimeInterval(10))
        state.apply(.streamDisconnected(.cancelled), now: now.addingTimeInterval(20))

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(20)) == .none)

        state.apply(.snapshotSucceeded(hasLoadedData: true), now: now.addingTimeInterval(21))

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(21)) == .none)
    }

    @Test
    func streamWatchdogReconnectWhileFreshDoesNotShowWarning() {
        var state = loadedFreshState()

        state.apply(.streamDisconnected(.watchdogStop), now: now.addingTimeInterval(45))

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(45)) == .none)
    }

    @Test
    func streamErrorsCannotOverrideSuccessfulSnapshot() {
        var state = TimelineConnectivityState(
            reachability: .degraded,
            consecutiveSnapshotFailures: 2,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true
        )

        #expect(state.banner(at: now) == .degraded)

        state.apply(.streamDisconnected(.networkError), now: now.addingTimeInterval(1))
        state.apply(.snapshotSucceeded(hasLoadedData: true), now: now.addingTimeInterval(2))

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .none)
    }

    @Test
    func repeatedSnapshotFailureWithStaleDataShowsDegraded() {
        var state = TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true
        )

        state.apply(.snapshotFailed, now: now)
        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))

        #expect(state.reachability == .degraded)
        #expect(state.consecutiveSnapshotFailures == 2)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .degraded)
    }

    @Test
    func authFailureIsSeparateFromOffline() {
        var state = loadedFreshState()

        state.apply(.authFailed, now: now)

        #expect(state.reachability == .authRequired)
        #expect(state.banner(at: now) == .authRequired)
    }

    @Test
    func backgroundForegroundLifecycleStopDoesNotChangeProductHealth() {
        var state = loadedFreshState()

        state.apply(.lifecycleStopped, now: now.addingTimeInterval(1))
        state.apply(.streamDisconnected(.clientStop), now: now.addingTimeInterval(2))

        #expect(state.reachability == .reachable)
        #expect(state.consecutiveSnapshotFailures == 0)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .none)
    }

    @Test
    func staleGenerationDisconnectCannotMutateProductHealth() {
        var state = loadedFreshState()

        state.apply(
            .authFailed,
            now: now.addingTimeInterval(1),
            eventGeneration: 1,
            currentGeneration: 2
        )
        state.apply(
            .snapshotFailed,
            now: now.addingTimeInterval(2),
            eventGeneration: 1,
            currentGeneration: 2
        )

        #expect(state.reachability == .reachable)
        #expect(state.consecutiveSnapshotFailures == 0)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .none)
    }

    @Test
    func staleGenerationStreamAuthFailureCannotMutateProductHealth() {
        var state = loadedFreshState()

        state.apply(
            .streamDisconnected(.authFailure),
            now: now.addingTimeInterval(1),
            eventGeneration: 1,
            currentGeneration: 2
        )

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)
    }

    @Test
    func waitingForConnectivityDiagnosticDoesNotMeanOffline() {
        var state = loadedFreshState()

        state.apply(.streamDisconnected(.waitingForConnectivity), now: now.addingTimeInterval(10))

        #expect(state.reachability == .reachable)
        #expect(state.banner(at: now.addingTimeInterval(10)) == .none)
    }

    @Test
    func freshnessUsesInjectedClockBoundaries() {
        let state = loadedFreshState()

        #expect(state.freshness(at: now.addingTimeInterval(90)) == .fresh)
        #expect(state.freshness(at: now.addingTimeInterval(91)) == .aging)
        #expect(state.freshness(at: now.addingTimeInterval(180)) == .aging)
        #expect(state.freshness(at: now.addingTimeInterval(181)) == .stale)
    }

    @Test
    func noDataOfflineThresholdIsTwoSnapshotFailures() {
        var state = TimelineConnectivityState()

        state.apply(.snapshotFailed, now: now)
        #expect(state.reachability == .degraded)
        #expect(state.banner(at: now) == .none)

        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))
        #expect(state.reachability == .offline)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .offline)
    }

    @Test
    func cacheLoadedProvidesFreshnessWithoutReachability() {
        var state = TimelineConnectivityState()

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: now.addingTimeInterval(-60)), now: now)

        #expect(state.reachability == .unknown)
        #expect(state.hasLoadedData)
        #expect(state.hasFreshnessEvidence)
        #expect(state.freshness(at: now) == .fresh)
        #expect(state.banner(at: now) == .none)
    }

    @Test
    func emptySnapshotSuccessStillProvidesFreshness() {
        var state = TimelineConnectivityState()

        state.apply(.snapshotSucceeded(hasLoadedData: false), now: now)

        #expect(state.reachability == .reachable)
        #expect(!state.hasLoadedData)
        #expect(state.hasFreshnessEvidence)
        #expect(state.freshness(at: now.addingTimeInterval(1)) == .fresh)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)
    }

    @Test
    func firstConnectDoesNotMakeEmptyColdStartLookFresh() {
        var state = TimelineConnectivityState()

        state.apply(.streamSignal(.firstConnected), now: now)

        #expect(state.freshness(at: now) == .unknown)
        #expect(state.banner(at: now) == .none)
    }

    @Test
    func firstConnectDoesNotFreshenStaleCache() {
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        state.apply(.streamSignal(.firstConnected), now: now.addingTimeInterval(1))

        #expect(state.lastUpdatedAt == savedAt)
        #expect(state.freshness(at: now.addingTimeInterval(1)) == .stale)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)
    }

    @Test
    func heartbeatDoesNotFreshenStaleCache() {
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        state.apply(.streamSignal(.heartbeat), now: now.addingTimeInterval(1))

        #expect(state.lastUpdatedAt == savedAt)
        #expect(state.freshness(at: now.addingTimeInterval(1)) == .stale)
    }

    @Test
    func singleSnapshotFailureOnStaleCacheStaysSilent() {
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        // One failed snapshot is a retry, not a fault. There is no
        // "Updating" strip to leak, and transport churn cannot escalate it.
        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))
        #expect(state.reachability == .degraded)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)

        state.apply(.streamSignal(.heartbeat), now: now.addingTimeInterval(2))

        #expect(state.banner(at: now.addingTimeInterval(2)) == .none)
    }

    @Test
    func staleCacheNeedsRepeatedSnapshotFailuresBeforeDegradedBanner() {
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))

        #expect(state.reachability == .degraded)
        #expect(state.consecutiveSnapshotFailures == 1)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)

        state.apply(.snapshotFailed, now: now.addingTimeInterval(2))

        #expect(state.reachability == .degraded)
        #expect(state.consecutiveSnapshotFailures == 2)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .degraded)
    }

    @Test
    func streamDisconnectAloneNeverDrivesAVisibleBanner() {
        // The exact bug class: pure transport churn on a stale cache, before
        // any snapshot has resolved, must stay silent.
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        #expect(state.banner(at: now) == .none)

        for offset in [1.0, 2.0, 3.0] {
            state.apply(.streamDisconnected(.serverEOF), now: now.addingTimeInterval(offset))
            #expect(state.reachability == .unknown)
            #expect(state.banner(at: now.addingTimeInterval(offset)) == .none)
        }
    }

    @Test
    func reconnectDoesNotStampFreshnessUntilBootstrapOrRealEvent() {
        var state = TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true
        )

        state.apply(.streamSignal(.reconnected), now: now)

        #expect(state.freshness(at: now) == .stale)
        #expect(state.lastUpdatedAt == now.addingTimeInterval(-300))

        state.apply(.snapshotSucceeded(hasLoadedData: true), now: now.addingTimeInterval(1))
        #expect(state.freshness(at: now.addingTimeInterval(1)) == .fresh)
    }

    @Test
    func unsatisfiedNetworkPathOnlyShowsOfflineWhenDataIsNotFresh() {
        var fresh = loadedFreshState()
        fresh.apply(.networkPathChanged(.unsatisfied), now: now.addingTimeInterval(10))
        #expect(fresh.reachability == .reachable)
        #expect(fresh.banner(at: now.addingTimeInterval(10)) == .none)

        var stale = TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true
        )
        stale.apply(.networkPathChanged(.unsatisfied), now: now)
        #expect(stale.reachability == .offline)
        #expect(stale.banner(at: now) == .offline)
    }

    @Test
    func networkPathFlapsDoNotEraseAuthRequired() {
        var state = TimelineConnectivityState(
            reachability: .authRequired,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true
        )

        state.apply(.networkPathChanged(.unsatisfied), now: now)
        state.apply(.networkPathChanged(.satisfied), now: now.addingTimeInterval(1))

        #expect(state.reachability == .authRequired)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .authRequired)
    }

    @Test
    func agingDataWhileSnapshotsFailNeverShowsAnUpdatingStrip() {
        // The whole point of the rewrite: the timeline is a live stream, so
        // the 90-180s window between "fresh" and "stale" is silence, not a
        // yellow bar. Silence lasts until failures repeat on stale data.
        var state = TimelineConnectivityState(
            reachability: .degraded,
            consecutiveSnapshotFailures: 1,
            lastUpdatedAt: now.addingTimeInterval(-120),
            hasLoadedData: true
        )

        #expect(state.freshness(at: now) == .aging)
        #expect(state.banner(at: now) == .none)

        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))
        #expect(state.banner(at: now.addingTimeInterval(1)) == .none)

        // Same failure count, but the data has now crossed into stale.
        #expect(state.banner(at: now.addingTimeInterval(200)) == .degraded)
    }

    @Test
    func offlineWithAgingDataSaysOfflineRatherThanGoingQuiet() {
        // Offline is hard evidence. Once the data stops being fresh we name
        // it instead of showing a frozen timeline with no explanation.
        let state = TimelineConnectivityState(
            reachability: .offline,
            consecutiveSnapshotFailures: 2,
            lastUpdatedAt: now.addingTimeInterval(-120),
            hasLoadedData: true
        )

        #expect(state.freshness(at: now) == .aging)
        #expect(state.banner(at: now) == .offline)
    }

    @Test
    func hostUpdateClaimWaitsTwoSecondsBeforeRendering() {
        var state = TimelineConnectivityState()
        state.apply(.hostLifecycle(hostLifecycle()), now: now)

        #expect(state.banner(at: now.addingTimeInterval(1.999)) == .none)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .updating)
    }

    @Test
    func actionWaitingOnRuntimeRestartShowsUpdateImmediately() {
        var state = TimelineConnectivityState()
        state.apply(.runtimeRestarting(hostLifecycle()), now: now)

        #expect(state.banner(at: now) == .updating)
        #expect(state.hostUpdate.waitingForAction)
    }
    @Test
    func authenticationRequirementOverridesAHostUpdateClaim() {
        var state = TimelineConnectivityState()
        state.apply(.hostLifecycle(hostLifecycle()), now: now)
        state.apply(.authFailed, now: now.addingTimeInterval(2))

        #expect(state.banner(at: now.addingTimeInterval(3)) == .authRequired)
    }

    @Test
    func claimRenewalKeepsItsStartAndSlowUpdateShowsElapsedTime() {
        var state = TimelineConnectivityState()
        state.apply(
            .hostLifecycle(hostLifecycle(expectedBackBy: now.addingTimeInterval(3))),
            now: now
        )

        #expect(state.banner(at: now.addingTimeInterval(2)) == .updating)
        #expect(state.banner(at: now.addingTimeInterval(3)) == .slowUpdate(elapsed: "3s"))

        let renewed = hostLifecycle(
            runtimeEpoch: "candidate",
            expectedBackBy: now.addingTimeInterval(2)
        )
        state.apply(.hostLifecycle(renewed), now: now.addingTimeInterval(4))

        #expect(state.hostUpdate.claimStartedAt == now)
        #expect(state.banner(at: now.addingTimeInterval(4)) == .slowUpdate(elapsed: "4s"))
    }

    @Test
    func epochAloneDoesNotEndClaimButServingEvidenceDoes() {
        var state = TimelineConnectivityState()
        state.apply(.hostLifecycle(hostLifecycle()), now: now)
        state.apply(
            .servingEvidence(.admission(nil, runtimeEpoch: "candidate")),
            now: now.addingTimeInterval(2)
        )
        #expect(state.banner(at: now.addingTimeInterval(2)) == .updating)

        state.apply(
            .servingEvidence(.admission(.pending, runtimeEpoch: "candidate")),
            now: now.addingTimeInterval(3)
        )
        #expect(state.banner(at: now.addingTimeInterval(3)) == .updating)

        state.apply(
            .servingEvidence(.admission(.open, runtimeEpoch: "candidate")),
            now: now.addingTimeInterval(4)
        )
        #expect(state.banner(at: now.addingTimeInterval(4)) == .none)
    }

    @Test
    func servingLifecycleAndAcceptedWriteEndClaim() {
        var eventState = TimelineConnectivityState()
        eventState.apply(.hostLifecycle(hostLifecycle()), now: now)
        eventState.apply(.hostLifecycle(hostLifecycle(state: .serving)), now: now.addingTimeInterval(1))
        #expect(eventState.banner(at: now.addingTimeInterval(1)) == .none)

        var writeState = TimelineConnectivityState()
        writeState.apply(.hostLifecycle(hostLifecycle()), now: now)
        writeState.apply(.servingEvidence(.writeAccepted), now: now.addingTimeInterval(1))
        #expect(writeState.banner(at: now.addingTimeInterval(1)) == .none)
    }

    private func hostLifecycle(
        state: HostLifecycleState = .updating,
        runtimeEpoch: String = "runtime-a",
        attemptId: String = "attempt-1",
        expectedBackBy: Date? = nil,
        deadline: Date? = nil,
        cutoff: Date? = nil
    ) -> HostLifecycle {
        HostLifecycle(
            state: state,
            runtimeEpoch: runtimeEpoch,
            attemptId: attemptId,
            phase: "drain",
            expectedBackBy: expectedBackBy.map(timestamp),
            deadline: deadline.map(timestamp),
            cutoff: cutoff.map(timestamp)
        )
    }

    private func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    private func loadedFreshState() -> TimelineConnectivityState {
        TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now,
            hasLoadedData: true
        )
    }
}
