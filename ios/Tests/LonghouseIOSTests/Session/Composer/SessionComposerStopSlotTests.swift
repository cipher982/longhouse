import Foundation
import Testing
@testable import Longhouse

@MainActor
struct SessionComposerStopSlotTests {
    @Test
    func emptyDraftWithAvailableStopShowsStop() {
        #expect(SessionComposerControlState.sendSlotShowsStop(stopAvailable: true, hasContent: false, armed: false))
    }

    @Test
    func draftKeepsSendUnlessStopIsArmed() {
        #expect(!SessionComposerControlState.sendSlotShowsStop(stopAvailable: true, hasContent: true, armed: false))
        // Armed from the + menu with a draft present: the confirm tap needs the slot.
        #expect(SessionComposerControlState.sendSlotShowsStop(stopAvailable: true, hasContent: true, armed: true))
    }

    @Test
    func unavailableStopNeverTakesTheSlot() {
        #expect(!SessionComposerControlState.sendSlotShowsStop(stopAvailable: false, hasContent: false, armed: true))
    }

    @Test
    func noticePrefersStopErrorThenFailedThenQueuedThenSent() {
        #expect(SessionComposerControlState.statusNotice(
            stopError: "Could not stop", failedCount: 2, queuedCount: 1, queuedElsewhereCount: 0, sent: true
        ) == .stopError)
        #expect(SessionComposerControlState.statusNotice(
            stopError: nil, failedCount: 2, queuedCount: 1, queuedElsewhereCount: 0, sent: true
        ) == .failed(count: 2))
        #expect(SessionComposerControlState.statusNotice(
            stopError: nil, failedCount: 0, queuedCount: 1, queuedElsewhereCount: 1, sent: true
        ) == .queued(total: 1, elsewhere: 1))
        #expect(SessionComposerControlState.statusNotice(
            stopError: nil, failedCount: 0, queuedCount: 0, queuedElsewhereCount: 0, sent: true
        ) == .sent)
        #expect(SessionComposerControlState.statusNotice(
            stopError: nil, failedCount: 0, queuedCount: 0, queuedElsewhereCount: 0, sent: false
        ) == nil)
    }

    @Test
    func confirmWindowIsShortEnoughToNotLinger() {
        #expect(SessionComposerControlState.stopConfirmWindow >= 2)
        #expect(SessionComposerControlState.stopConfirmWindow <= 5)
    }

    @Test
    func headlineNamesToolOnlyAsAWord() {
        #expect(SessionRuntimeDock.headlineNamesTool("Using Bash", tool: "Bash"))
        #expect(SessionRuntimeDock.headlineNamesTool("using bash", tool: "Bash"))
        #expect(!SessionRuntimeDock.headlineNamesTool("Bashful thinking", tool: "Bash"))
        #expect(!SessionRuntimeDock.headlineNamesTool("Thinking", tool: "Bash"))
    }
}
