import CoreGraphics
import Foundation
import XCTest

@testable import Longhouse

final class WebTranscriptViewTests: XCTestCase {
    func testDocumentBaseURLOnlyAcceptsWebOrigins() {
        XCTAssertEqual(
            WebTranscriptView.documentBaseURL("https://example.longhouse.ai"),
            URL(string: "https://example.longhouse.ai")
        )
        XCTAssertEqual(
            WebTranscriptView.documentBaseURL("http://127.0.0.1:8000"),
            URL(string: "http://127.0.0.1:8000")
        )
        // Anything else leaves the transcript on about:blank rather than
        // granting it an origin — the app container most of all.
        XCTAssertNil(WebTranscriptView.documentBaseURL("file:///var/mobile/Containers"))
        XCTAssertNil(WebTranscriptView.documentBaseURL("longhouse://session/1"))
        XCTAssertNil(WebTranscriptView.documentBaseURL("not a url at all"))
        XCTAssertNil(WebTranscriptView.documentBaseURL(nil))
    }

    func testRetryRevisionChangesIdentityForUnchangedPayload() {
        let initial = WebTranscriptView.ContentIdentity(
            serverURL: "https://example.longhouse.ai",
            revision: 7,
            transcriptReadThrough: nil,
            retryRevision: 0
        )
        let retry = WebTranscriptView.ContentIdentity(
            serverURL: "https://example.longhouse.ai",
            revision: 7,
            transcriptReadThrough: nil,
            retryRevision: 1
        )

        XCTAssertNotEqual(initial, retry)
    }

    func testPreparedPayloadReportsDiagnosticsFacts() {
        let payload = WebTranscriptView.preparedPayload(
            projectionItems: [projectionItem(makeUserEvent(id: 11, content: "server projected text", inputOrigin: nil))],
            submittedInputs: [makeSubmittedInput(text: "queued text", clientRequestId: "ios-request-1", serverInputId: nil)],
            errorMessage: nil
        )

        XCTAssertGreaterThan(payload.payloadByteSize, 0)
        XCTAssertFalse(payload.base64.isEmpty)
        XCTAssertFalse(payload.payloadFingerprint.isEmpty)
        // The document builds rows; native only knows how many events it sent.
        XCTAssertEqual(payload.rowCount, 1)
    }

    func testPayloadEnvelopeCarriesProjectionOverlayAndServerURL() throws {
        let data = WebTranscriptView.payloadData(
            serverURL: "https://example.longhouse.ai",
            projectionItems: [projectionItem(makeUserEvent(id: 11, content: "hello", inputOrigin: nil))],
            subagents: [],
            submittedInputs: [makeSubmittedInput(text: "queued text", clientRequestId: "ios-request-1", serverInputId: nil, phase: .queued)],
            errorMessage: nil,
            liteBodies: LiteBodyState()
        )
        let root = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let projection = try XCTUnwrap(root["projection"] as? [String: Any])
        XCTAssertEqual(projection["serverUrl"] as? String, "https://example.longhouse.ai")
        let items = try XCTUnwrap(projection["items"] as? [[String: Any]])
        let event = try XCTUnwrap(items.first?["event"] as? [String: Any])
        XCTAssertEqual(event["content_text"] as? String, "hello")
        XCTAssertEqual(event["is_head_branch"] as? Bool, true)
        let overlay = try XCTUnwrap(projection["overlay"] as? [[String: Any]])
        let row = try XCTUnwrap(overlay.first?["row"] as? [String: Any])
        XCTAssertEqual(row["kind"] as? String, "submitted")
        XCTAssertEqual(row["body"] as? String, "queued text")
        XCTAssertEqual(row["status"] as? String, "queued")
        XCTAssertEqual(row["subtitle"] as? String, "Queued \u{00B7} sends after this turn")
        XCTAssertEqual(overlay.first?["placedAtSendTime"] as? Bool, false)
        XCTAssertEqual(overlay.first?["phase"] as? String, "queued")
    }

    func testOverlayDropsSubmittedInputWhenDurableEventHasSameSessionInputId() {
        let rows = WebTranscriptView.overlayRows(
            projectionItems: [projectionItem(makeUserEvent(
                id: 11, content: "server projected text",
                inputOrigin: SessionInputOrigin(authoredVia: .longhouse, sessionInputId: 7, clientRequestId: nil)
            ))],
            submittedInputs: [makeSubmittedInput(text: "optimistic local text", clientRequestId: "ios-local", serverInputId: 7)]
        )
        XCTAssertTrue(rows.isEmpty)
    }

    func testOverlayDropsSubmittedInputWhenDurableEventHasSameClientRequestId() {
        let rows = WebTranscriptView.overlayRows(
            projectionItems: [projectionItem(makeUserEvent(
                id: 11, content: "server projected text",
                inputOrigin: SessionInputOrigin(authoredVia: .longhouse, sessionInputId: nil, clientRequestId: "ios-local")
            ))],
            submittedInputs: [makeSubmittedInput(text: "optimistic local text", clientRequestId: "ios-local", serverInputId: nil)]
        )
        XCTAssertTrue(rows.isEmpty)
    }

    func testOverlayKeepsSubmittedInputWhenDurableEventHasNoMatchingIdentity() {
        let rows = WebTranscriptView.overlayRows(
            projectionItems: [projectionItem(makeUserEvent(
                id: 11, content: "someone else",
                inputOrigin: SessionInputOrigin(authoredVia: .longhouse, sessionInputId: 9, clientRequestId: "other")
            ))],
            submittedInputs: [makeSubmittedInput(text: "mine", clientRequestId: "ios-local", serverInputId: 7)]
        )
        XCTAssertEqual(rows.map(\.row.body), ["mine"])
    }

    func testOverlayLabelsEachSubmittedPhase() {
        func subtitle(_ phase: SubmittedInputPhase, _ error: String? = nil) -> String? {
            WebTranscriptView.overlayRows(
                projectionItems: [],
                submittedInputs: [makeSubmittedInput(text: "t", clientRequestId: "r", serverInputId: nil, phase: phase, lastError: error)]
            ).first?.row.subtitle
        }
        XCTAssertEqual(subtitle(.couldNotConfirm), "Not confirmed")
        XCTAssertEqual(subtitle(.sent), "Sent")
        XCTAssertEqual(subtitle(.working), "Sent")
        XCTAssertEqual(subtitle(.needsUserDecision, "The active turn already ended."), "Needs choice \u{2014} The active turn already ended.")
        XCTAssertEqual(subtitle(.failed, "provider_auth_missing: run login"), "Not delivered \u{2014} provider_auth_missing: run login")
    }

    func testOverlayCarriesBoundedAttachmentSummaryAndPlacementFacts() {
        var input = SubmittedInput(
            id: "ios-request-attachments",
            clientRequestId: "ios-request-attachments",
            text: "describe this",
            intent: "auto",
            attachmentSummaries: [SubmittedInputAttachmentSummary(filename: "shot.jpg", mimeType: "image/jpeg", byteSize: 4)],
            phase: .failed,
            serverInputId: nil,
            lastError: "unsupported attachment",
            createdAt: Date(timeIntervalSince1970: 5)
        )
        input.placedAtSendTime = true
        let rows = WebTranscriptView.overlayRows(projectionItems: [], submittedInputs: [input])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.row.attachments?.first?.filename, "shot.jpg")
        XCTAssertEqual(rows.first?.row.status, "failed placed")
        XCTAssertEqual(rows.first?.createdAtMs, 5000)
        XCTAssertEqual(rows.first?.placedAtSendTime, true)
    }

    func testOverlayTurnsWakeAndCloseReceiptsIntoProviderNotifications() {
        let rows = WebTranscriptView.overlayRows(
            projectionItems: [],
            submittedInputs: [
                makeSubmittedInput(text: "Task done", clientRequestId: "wake:1", serverInputId: nil, origin: "wake"),
                makeSubmittedInput(text: "Session closed", clientRequestId: "close:1", serverInputId: nil, origin: "longhouse"),
            ]
        )
        XCTAssertEqual(rows.map(\.row.kind), ["providerNotification", "providerNotification"])
        XCTAssertEqual(rows.map(\.row.origin), [nil, "longhouse"])
        XCTAssertEqual(rows.map(\.row.wake), [true, nil])
    }

    private func projectionItem(_ event: SessionEvent) -> SessionProjectionItem {
        SessionProjectionItem(
            kind: "event", sessionId: "session-1", timestamp: event.timestamp, event: event,
            continuedFromSessionId: nil, continuationKind: nil, originLabel: nil,
            parentOriginLabel: nil, parentContinuationKind: nil, branchedFromEventId: nil
        )
    }

    private func makeSubmittedInput(
        text: String,
        clientRequestId: String,
        serverInputId: Int?,
        origin: String = "user",
        phase: SubmittedInputPhase = .sent,
        lastError: String? = nil,
        createdAt: Date = Date(timeIntervalSince1970: 0)
    ) -> SubmittedInput {
        SubmittedInput(
            id: clientRequestId,
            clientRequestId: clientRequestId,
            text: text,
            origin: origin,
            intent: "auto",
            phase: phase,
            serverInputId: serverInputId,
            lastError: lastError,
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
}
