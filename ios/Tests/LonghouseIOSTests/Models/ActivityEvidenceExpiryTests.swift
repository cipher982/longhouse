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

    @Test
    func ledgerStopsWorkAtExpiryEvenWhenViewerIsConnected() {
        let facts = makeSessionStateFacts(
            activity: "executing",
            activityValidUntil: "2026-08-23T12:10:00Z"
        )
        #expect(
            facts.ledgerEvidence(asOf: at("2026-08-23T12:11:00Z")) == .uncertain
        )
    }

    @Test
    func aLiveWindowKeepsItsClaimOnEveryViewerTransport() {
        // The socket is not provider evidence. `connecting` is the ordinary
        // first frame of every open and every return from the background, and
        // `disconnected` says only that updates are not arriving -- neither is a
        // statement about the provider, so neither may rewrite the claim.
        let facts = makeSessionStateFacts(
            activity: "executing",
            activityValidUntil: "2026-08-23T12:10:00Z"
        )
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:05:00Z")) == .working)
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:09:59Z")) == .working)
    }

    @Test
    func anIdleSessionStaysQuietOnEveryViewerTransport() {
        let facts = makeSessionStateFacts(activity: "quiescent", activityValidUntil: nil)
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:05:00Z")) == .quiet)
    }

    @Test
    func aPendingInteractionOutranksAnExpiredWindow() {
        let facts = makeSessionStateFacts(
            activity: "executing",
            pendingInteractionKind: "approval",
            activityValidUntil: "2026-08-23T12:10:00Z"
        )
        #expect(
            facts.ledgerEvidence(asOf: at("2026-08-23T12:11:00Z")) == .attention
        )
    }

    @Test
    func delegatedDetailWorkUsesItsOwnWindowAndPreservesParentAndInteractionPrecedence() {
        let primary = SessionStateLabel(
            key: "delegated_work", label: "Background", tone: "active", observedAt: nil
        )
        var facts = makeSessionStateFacts(
            activity: "quiescent",
            activityValidUntil: "2026-08-23T12:01:00Z",
            primaryOverride: primary
        )
        facts.delegation = SessionDelegationFacts(
            state: "pending", count: 1, kinds: ["subagent": 1], source: "claude_hook",
            observedAt: "2026-08-23T12:00:00Z", validUntil: "2026-08-23T12:30:00Z", items: nil
        )
        facts = facts.withMirroredSignal()
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:05:00Z")) == .working)
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:30:00Z")) == .uncertain)
        // The server serves delegated work only once the parent loop is
        // quiescent, so "delegated over an expired parent" is no longer an
        // input the app has to fence.

        var interaction = makeSessionStateFacts(
            activity: "quiescent", pendingInteractionKind: "question",
            activityValidUntil: "2026-08-23T12:01:00Z"
        )
        interaction.delegation = facts.delegation
        #expect(interaction.ledgerEvidence(asOf: at("2026-08-23T12:05:00Z")) == .attention)
    }

    @Test
    func aPayloadWithoutTheServedSignalReadsUncertain() {
        // An older host or cache: no served signal means no valid claim,
        // whatever the headline says. Missing evidence is uncertain, never
        // quiet, and the app does not rebuild the axis.
        var facts = makeSessionStateFacts(activity: "executing", activityValidUntil: "2026-08-23T12:30:00Z")
        facts.signal = nil
        let now = at("2026-08-23T12:05:00Z")
        #expect(facts.workClaimValidUntil == nil)
        #expect(facts.workClaimExpired(asOf: now))
        #expect(facts.servedSignal(asOf: now) == .unknown)
        #expect(facts.ledgerEvidence(asOf: now) == .uncertain)
    }

    @Test
    func startingUsesItsServedWorkClaimRatherThanTheQuietParentClock() {
        let primary = SessionStateLabel(key: "starting", label: "Starting", tone: "active", observedAt: nil)
        let facts = makeSessionStateFacts(
            activity: "quiescent", launchState: "dispatched", runLifecycle: "starting",
            activityValidUntil: "2026-08-23T12:01:00Z", primaryOverride: primary
        )
        #expect(facts.ledgerEvidence(asOf: at("2026-08-23T12:05:00Z")) == .working)
        #expect(facts.workClaimValidUntil == nil)
    }

    @Test
    func elapsedStopsAtExpiryWithoutCountingFutureValidity() {
        let now = at("2026-08-23T12:05:00Z")
        let future = at("2026-08-23T12:10:00Z")
        let expired = at("2026-08-23T12:04:00Z")
        #expect(RuntimeElapsed.observedEnd(validUntil: future, now: now) == now)
        #expect(RuntimeElapsed.observedEnd(validUntil: expired, now: now) == expired)
    }

    @Test
    func recoveryRequiresAnInterruptionAndANewProviderObservation() {
        var status = SessionLedgerNoticeState()
        let now = at("2026-08-23T12:05:00Z")
        let original = SessionProviderEvidenceIdentity(observedAt: "first", state: "executing", tool: "Read", source: "provider")
        let fresh = SessionProviderEvidenceIdentity(observedAt: "second", state: "executing", tool: "Read", source: "provider")
        status.observe(state: .uncertain, evidence: nil, resultAt: nil, now: now)
        status.observe(state: .working, evidence: original, resultAt: nil, now: now)
        #expect(status.notice == nil)
        status.observe(state: .uncertain, evidence: original, resultAt: nil, now: now)
        status.observe(state: .working, evidence: original, resultAt: nil, now: now)
        #expect(status.notice == nil)
        status.observe(state: .working, evidence: fresh, resultAt: nil, now: now)
        #expect(status.notice == .restored)
        let deadline = status.until
        status.observe(state: .working, evidence: fresh, resultAt: nil, now: now.addingTimeInterval(1))
        #expect(status.until == deadline)
        status.observe(state: .uncertain, evidence: fresh, resultAt: nil, now: now)
        #expect(status.notice == nil)
    }

    @Test
    func newWorkOrApprovalSupersedesCompletionNotices() {
        var status = SessionLedgerNoticeState()
        let now = at("2026-08-23T12:05:00Z")
        status.observe(state: .working, evidence: nil, resultAt: nil, now: now)
        status.observe(state: .quiet, evidence: nil, resultAt: "result-1", now: now)
        #expect(status.notice == .finished)
        status.observe(state: .working, evidence: nil, resultAt: "result-1", now: now)
        #expect(status.notice == nil)
        status.observe(state: .quiet, evidence: nil, resultAt: "result-2", now: now)
        status.observe(state: .attention, evidence: nil, resultAt: "result-2", now: now)
        #expect(status.notice == nil)
    }


    @Test
    func pendingInteractionKeepsAttentionAboveAnExpiredWindow() {
        let facts = makeSessionStateFacts(
            activity: "executing",
            pendingInteractionKind: "approval",
            activityValidUntil: "2026-08-23T12:10:00Z"
        )
        #expect(
            facts.ledgerEvidence(
                asOf: at("2026-08-23T12:11:00Z")
            ) == .attention
        )
    }
}
