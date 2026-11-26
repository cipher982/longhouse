import Foundation
import Testing

@testable import Longhouse

/// Which background work the session dock summarizes, and how. Each case is a detail one
/// of the background-task UI fixtures serves, so the UI tests only show that the dock
/// follows this decision (SessionChatBackgroundTaskUITests: a summary appears, opens its
/// sheet, and leaves after an empty snapshot).
struct SessionDelegationSummaryTests {
    private func summary(ofFixture fixture: String) -> SessionDelegationSummary {
        let detail = ChatUITestWorkspaceClient.makeDetail(
            sessionID: "ui-test-chat-session",
            events: [],
            title: ChatUITestWorkspaceClient.titleForFixture(fixture)
        )
        return SessionDelegationSummary(detail: detail, asOf: Date())
    }

    /// No summary line when there is no background work to speak of: the server reported
    /// none, its empty report has expired (on the server or here), or it reported an
    /// unknown state without ever having observed any work.
    @Test(arguments: [
        "background-tasks-empty",
        "background-tasks-local-expired-empty",
        "background-tasks-expired-empty",
        "background-tasks-unobserved-unknown",
    ])
    func noReportedWorkShowsNoSummary(fixture: String) {
        let summary = summary(ofFixture: fixture)
        #expect(summary.label == nil)
        #expect(!summary.replacesHeadline)
    }

    @Test
    func reportedWorkIsCountedUnderTheParentHeadline() {
        let summary = summary(ofFixture: "background-tasks")
        #expect(summary.label == "Background · 1 agent · 1 command")
        #expect(!summary.presentation.isUnknown)
        #expect(!summary.replacesHeadline, "the parent's own headline stays primary")
    }

    @Test
    func expiredPositiveEvidenceBecomesUnknownRatherThanDisappearing() {
        let summary = summary(ofFixture: "background-tasks-expired-positive")
        #expect(summary.label == "Background work status unknown")
        #expect(summary.presentation.isUnknown)
    }

}
