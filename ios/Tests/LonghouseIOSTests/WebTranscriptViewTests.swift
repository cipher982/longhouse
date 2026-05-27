import Foundation
import XCTest

@testable import Longhouse

final class WebTranscriptViewTests: XCTestCase {
    func testPreparedPayloadReportsDiagnosticsFacts() {
        let payload = WebTranscriptView.preparedPayload(
            timelineItems: [
                .user(makeUserEvent(
                    id: 11,
                    content: "server projected text",
                    inputOrigin: nil
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "queued text",
                    clientRequestId: "ios-request-1",
                    serverInputId: nil
                ),
            ],
            errorMessage: nil
        )

        XCTAssertGreaterThan(payload.payloadByteSize, 0)
        XCTAssertFalse(payload.base64.isEmpty)
        XCTAssertEqual(payload.rowCount, 2)
        XCTAssertEqual(payload.latestItemId, "ios-request-1")
    }

    func testPayloadSuppressesSubmittedInputWhenDurableLonghouseEventHasSameSessionInputId() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [
                .user(makeUserEvent(
                    id: 11,
                    content: "server projected text",
                    inputOrigin: SessionInputOrigin(
                        authoredVia: .longhouse,
                        sessionInputId: 7,
                        clientRequestId: nil
                    )
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "optimistic local text",
                    clientRequestId: "ios-local",
                    serverInputId: 7
                ),
            ]
        )

        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.kind, "message")
        XCTAssertEqual(rows.first?.body, "server projected text")
        XCTAssertEqual(rows.first?.origin, "longhouse")
    }

    func testPayloadSuppressesSubmittedInputWhenDurableLonghouseEventHasSameClientRequestId() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [
                .user(makeUserEvent(
                    id: 11,
                    content: "server projected text",
                    inputOrigin: SessionInputOrigin(
                        authoredVia: .longhouse,
                        sessionInputId: nil,
                        clientRequestId: "ios-request-1"
                    )
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "optimistic local text",
                    clientRequestId: "ios-request-1",
                    serverInputId: nil
                ),
            ]
        )

        XCTAssertEqual(rows.map(\.kind), ["message"])
        XCTAssertEqual(rows.first?.body, "server projected text")
    }

    func testPayloadKeepsSubmittedInputWhenDurableEventHasNoMatchingIdentity() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [
                .user(makeUserEvent(
                    id: 11,
                    content: "same visible text",
                    inputOrigin: nil
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "same visible text",
                    clientRequestId: "ios-request-1",
                    serverInputId: nil
                ),
            ]
        )

        XCTAssertEqual(rows.map(\.kind), ["message", "submitted"])
        XCTAssertEqual(rows.map(\.body), ["same visible text", "same visible text"])
    }

    private func makeSubmittedInput(
        text: String,
        clientRequestId: String,
        serverInputId: Int?
    ) -> SubmittedInput {
        SubmittedInput(
            id: clientRequestId,
            clientRequestId: clientRequestId,
            text: text,
            intent: "auto",
            phase: .sent,
            serverInputId: serverInputId,
            lastError: nil,
            createdAt: Date(timeIntervalSince1970: 0)
        )
    }

    private func makeUserEvent(
        id: Int,
        content: String,
        inputOrigin: SessionInputOrigin?,
        isHeadBranch: Bool = true
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "user",
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: isHeadBranch,
            inputOrigin: inputOrigin
        )
    }
}
