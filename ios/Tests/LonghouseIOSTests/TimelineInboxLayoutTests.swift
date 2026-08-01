import Foundation
import Testing
@testable import Longhouse

struct TimelineInboxLayoutTests {
    @Test
    func partitionsCanonicalFactsWithoutDuplication() {
        let needs = session(
            id: "needs",
            facts: makeSessionStateFacts(
                activity: "blocked",
                pendingInteractionKind: "permission",
                workingSet: "open"
            )
        )
        let quietOpen = session(
            id: "quiet-open",
            facts: makeSessionStateFacts(activity: "quiescent", workingSet: "open")
        )
        let openWithOldUnread = session(
            id: "open-unread",
            facts: makeSessionStateFacts(
                activity: "thinking",
                workingSet: "open",
                unread: true,
                lastResultAt: "2026-08-01T12:00:00Z",
                lastResultOutcome: "completed"
            )
        )
        let resultOlder = session(
            id: "result-older",
            facts: makeSessionStateFacts(
                activity: "quiescent",
                workingSet: "history",
                unread: true,
                lastResultAt: "2026-08-01T10:00:00Z",
                lastResultOutcome: "completed"
            )
        )
        let resultNewer = session(
            id: "result-newer",
            facts: makeSessionStateFacts(
                activity: "quiescent",
                workingSet: "history",
                unread: true,
                lastResultAt: "2026-08-01T11:00:00Z",
                lastResultOutcome: "failed"
            )
        )
        let recent = session(
            id: "recent",
            facts: makeSessionStateFacts(activity: "quiescent", workingSet: "history")
        )

        let layout = buildTimelineInboxLayout([
            recent, resultOlder, openWithOldUnread, needs, resultNewer, quietOpen,
        ])

        #expect(layout.needsYou.map(\.id) == ["needs"])
        #expect(layout.newResults.map(\.id) == ["result-newer", "result-older"])
        #expect(layout.open.map(\.id) == ["open-unread", "quiet-open"])
        #expect(layout.recent.map(\.id) == ["recent"])

        let allIDs = layout.needsYou + layout.newResults + layout.open + layout.recent
        #expect(allIDs.count == Set(allIDs.map(\.id)).count)
        #expect(Set(allIDs.map(\.id)) == Set([
            "needs", "quiet-open", "open-unread", "result-older", "result-newer", "recent",
        ]))
    }

    @Test
    func stalledWithoutExplicitInteractionStaysOutOfNeedsYou() {
        let stalled = session(
            id: "stalled",
            facts: makeSessionStateFacts(activity: "stalled", workingSet: "open")
        )

        let layout = buildTimelineInboxLayout([stalled])

        #expect(layout.needsYou.isEmpty)
        #expect(layout.open.map(\.id) == ["stalled"])
    }

    private func session(id: String, facts: SessionStateFacts) -> SessionSummary {
        SessionSummary(
            id: id,
            title: "Session \(id)",
            presenceState: facts.activityState,
            provider: "codex",
            project: "longhouse",
            lastActivityAt: "2026-08-01T12:00:00Z",
            homeLabel: "cube",
            timelineAnchorAt: "2026-08-01T12:00:00Z",
            runtimeDisplay: runtimeDisplay(for: facts),
            stateFacts: facts
        )
    }

    private func runtimeDisplay(for facts: SessionStateFacts) -> SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: "fixture",
            signalTier: "fixture",
            state: facts.activityState,
            tone: facts.activityState,
            headline: facts.activityState,
            detail: nil,
            phaseLabel: facts.activityState,
            compactToolLabel: nil,
            isLive: facts.workingSet == "open",
            isExecuting: facts.activityState == "executing",
            needsAttention: facts.pendingInteractionKind != nil,
            isIdle: facts.activityState == "quiescent",
            isStalled: facts.activityState == "stalled",
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: "recent",
            lifecycle: facts.workingSet,
            hostState: "online",
            terminalReason: nil
        )
    }
}
