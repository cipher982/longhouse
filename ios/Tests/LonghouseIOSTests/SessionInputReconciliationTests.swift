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

    @Test
    func pendingIntentRoundTripsAttachmentBytesAndIsAccountScoped() {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-pending-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = PendingInputStore(directory: directory)
        let intent = PendingInputIntent(
            clientRequestId: "ios-request-1",
            serverURL: "https://tenant.example",
            authGeneration: "login-1",
            sessionId: "session-1",
            text: "keep this",
            intent: "auto",
            attachments: [
                PendingInputIntent.Attachment(
                    id: UUID(),
                    filename: "note.txt",
                    data: Data("attachment bytes".utf8),
                    mimeType: "text/plain"
                ),
            ],
            createdAt: Date(timeIntervalSince1970: 1_000)
        )

        #expect(store.save(intent))
        #expect(store.load(
            serverURL: "https://tenant.example/",
            sessionId: "session-1",
            authGeneration: "login-1"
        ) == [intent])
        #expect(store.load(
            serverURL: "https://tenant.example",
            sessionId: "session-1",
            authGeneration: "login-2"
        ).isEmpty)
        #expect(store.load(
            serverURL: "https://other.example",
            sessionId: "session-1",
            authGeneration: "login-1"
        ).isEmpty)
    }

    @Test
    func pendingStoreKeepsMultipleOperationIdentitiesSeparate() {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-pending-many-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = PendingInputStore(directory: directory)
        let first = PendingInputIntent(
            clientRequestId: "ios-request-1",
            serverURL: "https://tenant.example",
            authGeneration: "login-1",
            sessionId: "session-1",
            text: "first",
            intent: "auto",
            attachments: [],
            createdAt: Date(timeIntervalSince1970: 1_000)
        )
        let second = PendingInputIntent(
            clientRequestId: "ios-request-2",
            serverURL: "https://tenant.example",
            authGeneration: "login-1",
            sessionId: "session-1",
            text: "second",
            intent: "auto",
            attachments: [],
            createdAt: Date(timeIntervalSince1970: 1_001)
        )

        #expect(store.save(first))
        #expect(store.save(second))
        #expect(store.load(
            serverURL: "https://tenant.example",
            sessionId: "session-1",
            authGeneration: "login-1"
        ).map(\.clientRequestId) == ["ios-request-1", "ios-request-2"])
        store.remove(first)
        #expect(store.load(
            serverURL: "https://tenant.example",
            sessionId: "session-1",
            authGeneration: "login-1"
        ) == [second])
    }
    @Test
    func deliveringReceiptRemainsUnconfirmed() {
        #expect(SessionInputReceiptDisposition.from(status: "delivering") == .couldNotConfirm)
        #expect(
            SessionInputReceiptDisposition.from(status: "failed", error: "delivery_unknown")
                == .couldNotConfirm
        )
        #expect(SessionInputReceiptDisposition.from(status: "queued") == .accepted)
        #expect(SessionInputOutcome(rawValue: "unknown") == .unknown)
    }
    }
