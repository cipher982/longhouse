import Foundation
import Testing

@testable import Longhouse

/// Locks in the `ConnectionState` state machine derived from
/// `(consecutiveRefreshFailures, lastUpdatedAt)`. The contract was last
/// touched in commit 486fce76 to keep cold-start single failures in
/// `.connecting` rather than flipping straight to `.reconnecting`.
struct ConnectionStateTests {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    // MARK: cold start (no successful poll on record)

    @Test
    func coldStartZeroFailuresIsConnecting() {
        #expect(ConnectionState.derive(failures: 0, lastUpdatedAt: nil) == .connecting)
    }

    @Test
    func coldStartOneFailureStaysConnecting() {
        // Critical: a single hiccup before we ever connected must NOT read
        // as "reconnecting" — there's nothing to reconnect to.
        #expect(ConnectionState.derive(failures: 1, lastUpdatedAt: nil) == .connecting)
    }

    @Test
    func coldStartTwoFailuresIsOffline() {
        #expect(ConnectionState.derive(failures: 2, lastUpdatedAt: nil) == .offline)
    }

    // MARK: warm (had a successful poll)

    @Test
    func warmZeroFailuresIsHealthy() {
        #expect(ConnectionState.derive(failures: 0, lastUpdatedAt: now) == .healthy)
    }

    @Test
    func warmOneFailureIsReconnecting() {
        #expect(ConnectionState.derive(failures: 1, lastUpdatedAt: now) == .reconnecting)
    }

    @Test
    func warmTwoFailuresIsOffline() {
        #expect(ConnectionState.derive(failures: 2, lastUpdatedAt: now) == .offline)
    }

    // MARK: boundaries / defensive

    @Test
    func warmBoundaryAtThreeFailuresStaysOffline() {
        // Off-by-one guard: anything past the 2 threshold must remain offline.
        #expect(ConnectionState.derive(failures: 3, lastUpdatedAt: now) == .offline)
        #expect(ConnectionState.derive(failures: 99, lastUpdatedAt: now) == .offline)
    }

    @Test
    func coldStartBoundaryAtThreeFailuresStaysOffline() {
        #expect(ConnectionState.derive(failures: 3, lastUpdatedAt: nil) == .offline)
    }

    @Test
    func negativeFailureCountResolvesToKnownState() {
        // Defensive: negative counts shouldn't produce an undefined state.
        // Cold start: < 2 → .connecting (treated as "no failures yet").
        #expect(ConnectionState.derive(failures: -1, lastUpdatedAt: nil) == .connecting)
        // Warm: hits the `default:` arm of the switch → .offline. This is a
        // mild quirk (negative ought to be impossible) but it's the actual
        // shipped behavior; encoding it so an accidental change is caught.
        #expect(ConnectionState.derive(failures: -1, lastUpdatedAt: now) == .offline)
    }

    // MARK: transitions

    @Test
    func transitionHealthyToReconnectingToOffline() {
        let warm: Date? = now
        let states: [ConnectionState] = [0, 1, 2].map {
            ConnectionState.derive(failures: $0, lastUpdatedAt: warm)
        }
        #expect(states == [.healthy, .reconnecting, .offline])
    }

    @Test
    func transitionColdStartConnectingThroughFirstFailureThenOffline() {
        let cold: Date? = nil
        let states: [ConnectionState] = [0, 1, 2].map {
            ConnectionState.derive(failures: $0, lastUpdatedAt: cold)
        }
        #expect(states == [.connecting, .connecting, .offline])
    }
}
