import Foundation
import Testing

@testable import Longhouse

/// The activity axis must stop claiming work once its evidence window passes.
///
/// A view holding a snapshot renders whatever it last received for as long as it
/// stays on screen. A wedged turn sends no further frame, so without expiry a
/// correct server and a "Working" bar coexist indefinitely -- ten hours, in the
/// incident this came from.
struct ActivityEvidenceExpiryTests {
    private func at(_ iso: String) -> Date {
        guard let date = LonghouseDateParser.parse(iso) else {
            fatalError("fixture timestamp did not parse: \(iso)")
        }
        return date
    }

    @Test
    func evidenceInsideItsWindowStillReportsWork() {
        let facts = makeSessionStateFacts(activity: "executing", activityValidUntil: "2026-08-23T12:10:00Z")
        #expect(facts.activityEvidenceIsLive(asOf: at("2026-08-23T12:05:00Z")))
    }

    @Test
    func evidencePastItsWindowStopsReportingWork() {
        let facts = makeSessionStateFacts(activity: "executing", activityValidUntil: "2026-08-23T12:10:00Z")
        #expect(!facts.activityEvidenceIsLive(asOf: at("2026-08-23T22:10:00Z")))
    }

    @Test
    func aMissingWindowIsNotAnExpiredWindow() {
        // Inventing an expiry would hide live activity.
        let facts = makeSessionStateFacts(activity: "executing", activityValidUntil: nil)
        #expect(facts.activityEvidenceIsLive(asOf: at("2030-01-01T00:00:00Z")))
    }

    @Test
    func anUnparseableWindowIsNotAnExpiredWindow() {
        let facts = makeSessionStateFacts(activity: "executing", activityValidUntil: "not-a-timestamp")
        #expect(facts.activityEvidenceIsLive(asOf: at("2030-01-01T00:00:00Z")))
    }

    @Test
    func fractionalSecondWindowsParse() {
        let facts = makeSessionStateFacts(activity: "executing", activityValidUntil: "2026-08-23T12:10:00.123456Z")
        #expect(facts.activityEvidenceIsLive(asOf: at("2026-08-23T12:09:00Z")))
        #expect(!facts.activityEvidenceIsLive(asOf: at("2026-08-23T12:11:00Z")))
    }
}
