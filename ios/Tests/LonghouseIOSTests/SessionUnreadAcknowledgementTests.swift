import Testing
@testable import Longhouse

@MainActor
struct SessionUnreadAcknowledgementTests {
    @Test
    func acknowledgesOnlyUnreadResultRenderedInActiveScene() {
        let unread = makeSessionStateFacts(
            workingSet: "history",
            unread: true,
            lastResultAt: "2026-08-01T12:00:00Z",
            lastResultOutcome: "completed"
        )

        #expect(SessionViewModel.unreadReadThrough(facts: unread, sceneIsActive: true) == "2026-08-01T12:00:00Z")
        #expect(SessionViewModel.unreadReadThrough(facts: unread, sceneIsActive: false) == nil)

        let alreadyRead = makeSessionStateFacts(
            workingSet: "history",
            unread: false,
            lastResultAt: "2026-08-01T12:00:00Z",
            lastResultOutcome: "completed"
        )
        #expect(SessionViewModel.unreadReadThrough(facts: alreadyRead, sceneIsActive: true) == nil)
        #expect(SessionViewModel.unreadReadThrough(facts: nil, sceneIsActive: true) == nil)
    }
}
