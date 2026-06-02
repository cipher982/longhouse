import CoreGraphics
import Foundation
import XCTest

@testable import Longhouse

final class WebTranscriptViewTests: XCTestCase {
    func testBottomInsetIncludesTabBarAreaWhenKeyboardIsClosed() {
        let inset = SessionBottomInsetCalculator.bottomInset(
            viewportFrame: CGRect(x: 0, y: 100, width: 393, height: 620),
            surfaceFrame: CGRect(x: 0, y: 610, width: 393, height: 120),
            cardFrame: CGRect(x: 12, y: 620, width: 369, height: 110),
            keyboardPresented: false,
            screenMaxY: 852
        )

        XCTAssertEqual(inset, 269)
    }

    func testBottomInsetDoesNotCountKeyboardHeightWhenKeyboardIsPresented() {
        let inset = SessionBottomInsetCalculator.bottomInset(
            viewportFrame: CGRect(x: 0, y: 100, width: 393, height: 510),
            surfaceFrame: CGRect(x: 0, y: 500, width: 393, height: 120),
            cardFrame: CGRect(x: 12, y: 510, width: 369, height: 110),
            keyboardPresented: true,
            screenMaxY: 852
        )

        XCTAssertEqual(inset, 147)
    }

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

    func testPayloadPlacesSentInputBeforeLiveProvisionalAssistantPreview() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [
                .user(makeUserEvent(
                    id: 11,
                    content: "prior durable text",
                    inputOrigin: nil
                )),
                .assistant(makeAssistantEvent(
                    id: -99,
                    content: "streaming answer",
                    timestamp: "2026-05-02T20:00:05Z",
                    eventOrigin: "live_provisional"
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "new local prompt",
                    clientRequestId: "ios-request-1",
                    serverInputId: 7
                ),
            ]
        )

        XCTAssertEqual(rows.map(\.kind), ["message", "submitted", "message"])
        XCTAssertEqual(rows.map(\.body), ["prior durable text", "new local prompt", "streaming answer"])
    }

    func testPayloadKeepsNewQueuedInputAfterExistingLivePreview() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [
                .assistant(makeAssistantEvent(
                    id: -99,
                    content: "already streaming",
                    timestamp: "2026-05-02T20:00:05Z",
                    eventOrigin: "live_provisional"
                )),
            ],
            submittedInputs: [
                makeSubmittedInput(
                    text: "queue after this turn",
                    clientRequestId: "ios-request-1",
                    serverInputId: 7,
                    phase: .queued,
                    createdAt: date("2026-05-02T20:00:06Z")
                ),
            ]
        )

        XCTAssertEqual(rows.map(\.kind), ["message", "submitted"])
        XCTAssertEqual(rows.map(\.body), ["already streaming", "queue after this turn"])
    }

    private func makeSubmittedInput(
        text: String,
        clientRequestId: String,
        serverInputId: Int?,
        phase: SubmittedInputPhase = .sent,
        createdAt: Date = Date(timeIntervalSince1970: 0)
    ) -> SubmittedInput {
        SubmittedInput(
            id: clientRequestId,
            clientRequestId: clientRequestId,
            text: text,
            intent: "auto",
            phase: phase,
            serverInputId: serverInputId,
            lastError: nil,
            createdAt: createdAt
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

    private func makeAssistantEvent(
        id: Int,
        content: String,
        timestamp: String,
        eventOrigin: String?
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "assistant",
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            eventOrigin: eventOrigin
        )
    }

    private func date(_ value: String) -> Date {
        LonghouseDateParser.parse(value) ?? Date(timeIntervalSince1970: 0)
    }
}
