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
        XCTAssertFalse(payload.payloadFingerprint.isEmpty)
        XCTAssertEqual(payload.rowCount, 2)
        XCTAssertEqual(payload.latestItemId, "ios-request-1")
    }

    func testPreparedPayloadForwardsSubagentsToToolRows() throws {
        let call = SessionEvent(
            id: 12,
            role: "assistant",
            contentText: nil,
            interactionKind: nil,
            toolName: "Task",
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: "call-1",
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
        let child = SessionSubagent(
            sessionId: "child-1",
            provider: "claude",
            parentToolCallId: "call-1",
            runId: nil,
            startedAt: "2026-05-02T20:00:01Z",
            lastActivityAt: "2026-05-02T20:00:02Z",
            endedAt: "2026-05-02T20:00:03Z",
            userMessages: 1,
            assistantMessages: 1,
            toolCalls: 2,
            title: "Worker",
            firstUserMessagePreview: nil,
            lastVisibleTextPreview: nil
        )
        let payload = WebTranscriptView.preparedPayload(
            timelineItems: [
                .tool(call: call, result: nil, pairing: .pending),
                .activityGroup(calls: [
                    ActivityCall(call: call, result: nil, pairing: .pending)
                ]),
            ],
            subagents: [child],
            submittedInputs: [],
            errorMessage: nil
        )

        let json = try JSONSerialization.jsonObject(
            with: Data(base64Encoded: payload.base64)!
        ) as? [String: Any]
        let rows = json?["items"] as? [[String: Any]]
        let workers = rows?.first?["subagents"] as? [[String: Any]]
        let groupedWorkers = rows?.dropFirst().first?["subagents"] as? [[String: Any]]
        XCTAssertEqual(workers?.first?["sessionId"] as? String, "child-1")
        XCTAssertEqual(workers?.first?["toolCalls"] as? Int, 2)
        XCTAssertEqual(groupedWorkers?.first?["sessionId"] as? String, "child-1")
    }

    func testPayloadRendersProviderNotificationAsCompactStatusItem() {
        let event = SessionEvent(
            id: 42,
            role: "system",
            contentText: "Background command \"Run the checks\" completed (exit code 0)",
            interactionKind: "provider_notification",
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:42Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
        let items = TimelineBuilder.build(events: [event])
        let rows = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])

        XCTAssertEqual(rows.map(\.kind), ["providerNotification"])
        XCTAssertEqual(rows.first?.body, event.contentText)
        XCTAssertNil(rows.first?.role)
    }

    func testWakeReceiptAndDurableEchoRenderAsProviderNotifications() {
        let wakeText = "Background task finished: the branch is ready"
        let receiptRows = WebTranscriptView.payloadItems(
            timelineItems: [],
            submittedInputs: [
                makeSubmittedInput(
                    text: wakeText,
                    clientRequestId: "wake:invocation-1:1",
                    serverInputId: nil,
                    origin: "wake"
                ),
            ]
        )
        XCTAssertEqual(receiptRows.map(\.kind), ["providerNotification"])
        XCTAssertNil(receiptRows.first?.role)
        XCTAssertEqual(receiptRows.first?.body, wakeText)

        let wakeEvent = makeUserEvent(
            id: 44,
            content: "provider's wake input",
            inputOrigin: SessionInputOrigin(
                authoredVia: .longhouse,
                origin: "wake",
                sessionInputId: nil,
                clientRequestId: "wake:invocation-1:1"
            )
        )
        let durableRows = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: [wakeEvent]),
            submittedInputs: []
        )
        XCTAssertEqual(durableRows.map(\.kind), ["providerNotification"])
        XCTAssertNil(durableRows.first?.role)
    }

    /// The transcript document collapses a notice from its text alone, so the
    /// payload must carry a long multi-line job result whole, not preview it.
    func testPayloadCarriesLongProviderNotificationInFull() {
        let text = "Background job bg_96 has completed.\n…\nered job: zerg-tenant-data-reserve\n"
            + String(repeating: "2026-09-30 15:24:21,974 [INFO] sauron.jobs.registry: Registered job\n", count: 25)
            + "[Output truncated. Showing first 4,000 characters.]\nFull output: artifact://335"
        let event = SessionEvent(
            id: 43,
            role: "system",
            contentText: text,
            interactionKind: "provider_notification",
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-09-30T15:24:22Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
        let rows = WebTranscriptView.payloadItems(timelineItems: TimelineBuilder.build(events: [event]), submittedInputs: [])

        XCTAssertEqual(rows.map(\.kind), ["providerNotification"])
        XCTAssertEqual(rows.first?.body, text)
        XCTAssertFalse(rows.first?.collapsed ?? true)
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

    func testPayloadLabelsUnconfirmedSubmittedInput() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [],
            submittedInputs: [
                makeSubmittedInput(
                    text: "maybe landed",
                    clientRequestId: "ios-request-1",
                    serverInputId: nil,
                    phase: .couldNotConfirm
                ),
            ]
        )

        XCTAssertEqual(rows.first?.kind, "submitted")
        XCTAssertEqual(rows.first?.status, "couldNotConfirm")
        XCTAssertEqual(rows.first?.subtitle, "Not confirmed")
    }

    func testPayloadLabelsSentSubmittedInput() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [],
            submittedInputs: [
                makeSubmittedInput(
                    text: "sent prompt",
                    clientRequestId: "ios-request-sent",
                    serverInputId: nil,
                    phase: .sent
                ),
            ]
        )

        XCTAssertEqual(rows.first?.status, "sent")
        XCTAssertEqual(rows.first?.subtitle, "Sent")
    }

    func testPayloadLabelsTurnEndedDecisionWithItsReason() {
        let rows = WebTranscriptView.payloadItems(
            timelineItems: [],
            submittedInputs: [
                makeSubmittedInput(
                    text: "keep going",
                    clientRequestId: "ios-turn-ended",
                    serverInputId: 7,
                    phase: .needsUserDecision,
                    lastError: "The active turn already ended."
                ),
            ]
        )

        XCTAssertEqual(rows.first?.status, "needsUserDecision")
        XCTAssertEqual(rows.first?.subtitle, "Needs choice — The active turn already ended.")
    }

    func testPayloadCarriesBoundedAttachmentSummaryOnOneSubmittedRow() {
        let input = SubmittedInput(
            id: "ios-request-attachments",
            clientRequestId: "ios-request-attachments",
            text: "describe this",
            intent: "auto",
            attachmentSummaries: [
                SubmittedInputAttachmentSummary(filename: "shot.jpg", mimeType: "image/jpeg", byteSize: 4)
            ],
            phase: .failed,
            serverInputId: nil,
            lastError: "unsupported attachment",
            createdAt: Date(timeIntervalSince1970: 0)
        )
        let rows = WebTranscriptView.payloadItems(timelineItems: [], submittedInputs: [input])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.attachments?.count, 1)
        XCTAssertEqual(rows.first?.attachments?.first?.filename, "shot.jpg")
        XCTAssertEqual(rows.first?.attachments?.first?.byteSize, 4)
    }


    func testPayloadCarriesPresentMediaRefsWithAbsoluteThumbnailURL() {
        let mediaRef = SessionEventMediaRef(
            sha256: "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
            mediaState: "present",
            mimeType: "image/png",
            byteSize: 1024,
            blobUrl: "/api/media/abc123/blob",
            thumbUrl: "/api/media/abc123/thumb",
            width: 2880,
            height: 1800,
            sourcePath: nil,
            sourceOffset: nil,
            jsonPointer: nil,
            originalKind: "data_url_backfill"
        )
        let rows = WebTranscriptView.payloadItems(
            serverURL: "https://david010.longhouse.ai",
            timelineItems: [
                .assistant(makeAssistantEvent(
                    id: 21,
                    content: "screenshot",
                    timestamp: "2026-05-02T20:00:05Z",
                    eventOrigin: nil,
                    mediaRefs: [
                        mediaRef,
                        mediaRef,
                    ]
                )),
            ],
            submittedInputs: []
        )

        XCTAssertEqual(rows.first?.media?.count, 1)
        XCTAssertEqual(rows.first?.media?.first?.sha256, "abc123def456abc123def456abc123def456abc123def456abc123def456abcd")
        XCTAssertEqual(rows.first?.media?.first?.url, "https://david010.longhouse.ai/api/media/abc123/thumb")
        XCTAssertEqual(rows.first?.media?.first?.blobUrl, "https://david010.longhouse.ai/api/media/abc123/blob")
        // The intrinsic size reaches the transcript so the row reserves layout.
        XCTAssertEqual(rows.first?.media?.first?.width, 2880)
        XCTAssertEqual(rows.first?.media?.first?.height, 1800)
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

    private func makeAssistantEvent(
        id: Int,
        content: String,
        timestamp: String,
        eventOrigin: String?,
        mediaRefs: [SessionEventMediaRef] = []
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
            eventOrigin: eventOrigin,
            mediaRefs: mediaRefs
        )
    }

    private func date(_ value: String) -> Date {
        LonghouseDateParser.parse(value) ?? Date(timeIntervalSince1970: 0)
    }
}
