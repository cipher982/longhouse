import Foundation
import Testing

@testable import Longhouse

@MainActor
struct SessionInputReconciliationTests {
    private func input(_ requestId: String, phase: SubmittedInputPhase = .sent, serverInputId: Int? = nil) -> SubmittedInput {
        SubmittedInput(
            id: requestId,
            clientRequestId: requestId,
            text: "ship it",
            intent: "auto",
            phase: phase,
            serverInputId: serverInputId,
            lastError: nil,
            createdAt: Date(timeIntervalSince1970: 1_000)
        )
    }

    private func receipt(_ requestId: String, eventId: String?) -> SessionInputReceipt {
        SessionInputReceipt(clientRequestId: requestId, intent: "auto", status: "delivered", createdAt: nil, eventId: eventId)
    }

    private func userEvent(id: String, origin: SessionInputOrigin?, text: String = "ship it") -> SessionEvent {
        SessionEvent(
            id: id,
            role: "user",
            contentText: text,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-09-01T12:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: origin,
            cursor: id
        )
    }

    @Test
    func linkedReceiptResolvesSendEvenWhenEchoIsOffThePage() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1"), input("req-2")],
            events: [],
            receipts: [receipt("req-1", eventId: "echo-1"), receipt("req-2", eventId: nil)]
        )
        #expect(resolved == ["req-1"])
    }

    @Test
    func stampedEventResolvesSendWithoutReceipt() {
        let origin = SessionInputOrigin(authoredVia: .longhouse, sessionInputId: nil, clientRequestId: "req-1")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1")],
            events: [userEvent(id: "echo-1", origin: origin)],
            receipts: []
        )
        #expect(resolved == ["req-1"])
    }

    @Test
    func identicalTextWithoutIdentityNeverResolves() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1")],
            events: [userEvent(id: "echo-1", origin: nil), userEvent(id: "echo-2", origin: nil)],
            receipts: [receipt("req-9", eventId: "echo-1")]
        )
        #expect(resolved.isEmpty)
    }

    @Test
    func decisionRowsStayUntilTheirOwnPathClearsThem() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1", phase: .needsUserDecision)],
            events: [],
            receipts: [receipt("req-1", eventId: "echo-1")]
        )
        #expect(resolved.isEmpty)
    }
}
