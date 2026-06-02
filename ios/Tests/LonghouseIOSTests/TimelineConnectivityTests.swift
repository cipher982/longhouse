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
            hasLoadedData: true,
            recoveryActive: true
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
            hasLoadedData: true,
            recoveryActive: false
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
        #expect(state.freshness(at: now) == .fresh)
        #expect(state.banner(at: now) == .none)
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
    func heartbeatDoesNotClearRecoveryWithoutFreshData() {
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        // Recovery is driven by a real product-health signal (a failed
        // snapshot), never by transport churn. A stream disconnect alone
        // must not set recovery (Rule 5).
        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        state.apply(.snapshotFailed, now: now.addingTimeInterval(1))
        #expect(state.recoveryActive)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .degraded)

        state.apply(.streamSignal(.heartbeat), now: now.addingTimeInterval(2))

        #expect(state.recoveryActive)
        #expect(state.banner(at: now.addingTimeInterval(2)) == .degraded)
    }

    @Test
    func streamDisconnectAloneNeverDrivesAVisibleBanner() {
        // The exact bug class: pure transport churn on a stale cache, before
        // any snapshot has resolved, must stay silent. No recovery flip, no
        // Updating strip.
        var state = TimelineConnectivityState()
        let savedAt = now.addingTimeInterval(-300)

        state.apply(.cacheLoaded(hasLoadedData: true, savedAt: savedAt), now: now)
        #expect(state.banner(at: now) == .none)

        for offset in [1.0, 2.0, 3.0] {
            state.apply(.streamDisconnected(.serverEOF), now: now.addingTimeInterval(offset))
            #expect(state.reachability == .unknown)
            #expect(state.recoveryActive == false)
            #expect(state.banner(at: now.addingTimeInterval(offset)) == .none)
        }
    }

    @Test
    func reconnectDoesNotStampFreshnessUntilBootstrapOrRealEvent() {
        var state = TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now.addingTimeInterval(-300),
            hasLoadedData: true,
            recoveryActive: true
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
            hasLoadedData: true,
            recoveryActive: false
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
            hasLoadedData: true,
            recoveryActive: false
        )

        state.apply(.networkPathChanged(.unsatisfied), now: now)
        state.apply(.networkPathChanged(.satisfied), now: now.addingTimeInterval(1))

        #expect(state.reachability == .authRequired)
        #expect(state.banner(at: now.addingTimeInterval(1)) == .authRequired)
    }

    private func loadedFreshState() -> TimelineConnectivityState {
        TimelineConnectivityState(
            reachability: .reachable,
            consecutiveSnapshotFailures: 0,
            lastUpdatedAt: now,
            hasLoadedData: true,
            recoveryActive: false
        )
    }
}
