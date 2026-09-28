import Foundation
import Testing

@testable import Longhouse

/// A stale observation is served as what was last seen, not as a current
/// state. Its age is the reader's real question, so the dock renders it beside
/// the headline instead of behind the evidence disclosure.
struct RuntimeObservationAgeTests {
    private let now = Date(timeIntervalSince1970: 1_789_000_000)

    private func ago(_ seconds: TimeInterval) -> Date {
        now.addingTimeInterval(-seconds)
    }

    @Test
    func agesReadAtHumanScales() {
        #expect(RuntimeElapsed.ageLabel(from: ago(12 * 60), to: now) == "12m ago")
        #expect(RuntimeElapsed.ageLabel(from: ago(3 * 3_600), to: now) == "3h ago")
        #expect(RuntimeElapsed.ageLabel(from: ago(3 * 86_400), to: now) == "3d ago")
    }

    @Test
    func aFreshObservationDoesNotReadAsZero() {
        #expect(RuntimeElapsed.ageLabel(from: ago(20), to: now) == "just now")
        #expect(RuntimeElapsed.ageLabel(from: now, to: now) == "just now")
    }

    @Test
    func aFutureObservationYieldsNoAgeRatherThanANegativeOne() {
        #expect(RuntimeElapsed.ageLabel(from: now.addingTimeInterval(300), to: now) == nil)
    }
}
