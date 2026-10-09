import Foundation
import Testing

@testable import Longhouse

@MainActor
struct SessionInputReconciliationTests {
    private func input(
        _ requestId: String,
        phase: SubmittedInputPhase = .sent,
        serverInputId: Int? = nil,
        turnId: String? = nil
    ) -> SubmittedInput {
        SubmittedInput(
            id: requestId,
            clientRequestId: requestId,
            text: "ship it",
            intent: "auto",
            phase: phase,
            serverInputId: serverInputId,
            turnId: turnId,
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
        let older = userEvent("older", text: "earlier", at: "2026-10-08T04:00:00Z")
        let offPage = served("req-1", text: "from yesterday", at: "2026-10-07T23:00:00Z", eventId: "echo-1")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1"), input("req-2")],
            events: [older],
            receipts: [offPage, receipt("req-2", eventId: nil)]
        )
        #expect(resolved == ["req-1"])
    }

    @Test
    func linkedSteerInsideAToolCallTailWithNoUserRowKeepsItsRow() {
        // The window holds only tool events, so its start has to come from
        // them. The steer is newer than that start and its echo is not loaded.
        let tool = assistantEvent("tool-1", at: "2026-10-08T04:10:00Z")
        let steer = served("steer", text: "voice never worked", at: "2026-10-08T04:28:41Z", intent: "steer", eventId: "steer-echo")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("steer")],
            events: [tool],
            receipts: [steer]
        )
        #expect(resolved.isEmpty)
    }

    @Test
    func linkedEchoOlderThanAToolOnlyWindowResolves() {
        let tool = assistantEvent("tool-1", at: "2026-10-08T04:10:00Z")
        let oldEcho = served("old", text: "earlier", at: "2026-10-08T04:00:00Z", eventId: "old-echo")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("old")],
            events: [tool],
            receipts: [oldEcho]
        )
        #expect(resolved == ["old"])
    }

    @Test
    func linkedSteerKeepsItsRowWhenNoUserRowIsLoadedYet() {
        // A long tool-call tail can load with no user row at all. Nothing is
        // known about the window then, so the steer's echo may still be coming.
        let steer = served("steer", text: "voice never worked", at: "2026-10-08T04:28:41Z", intent: "steer", eventId: "steer-echo")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("steer")],
            events: [],
            receipts: [steer]
        )
        #expect(resolved.isEmpty)
    }

    @Test
    func linkedReceiptWithUnreadableTimeKeepsItsRow() {
        // Placement needs a readable time, so a row whose receipt cannot be
        // placed must not be dropped either.
        let unreadable = served("unreadable", text: "no time", at: "not-a-date", eventId: "echo-unreadable")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("unreadable")],
            events: [userEvent("older", text: "earlier", at: "2026-10-08T04:00:00Z")],
            receipts: [unreadable]
        )
        #expect(resolved.isEmpty)
    }

    @Test
    func linkedSteerWhoseEchoIsNotYetLoadedKeepsItsRow() {
        // Mid-turn steer: the server linked its echo, but the echo is newer
        // than the loaded window and not in it yet. Dropping the row here is
        // the bug that made a follow-up vanish during tool calls.
        let older = userEvent("older", text: "earlier", at: "2026-10-08T04:00:00Z")
        let steer = served("steer", text: "voice never worked", at: "2026-10-08T04:28:41Z", intent: "steer", eventId: "steer-echo", turnState: "active")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("steer")],
            events: [older],
            receipts: [steer]
        )
        #expect(resolved.isEmpty)
        #expect(UnrecordedInputs.shownByTranscript(receipts: [steer], userEvents: [older]).isEmpty)
    }

    @Test
    func linkedSteerResolvesExactlyOnceWhenItsEchoLoads() {
        // Settled delivery, so a missing shown-check would place a second row.
        let steerEcho = userEvent("steer-echo", text: "voice never worked", at: "2026-10-08T04:28:42Z")
        let steer = served("steer", text: "voice never worked", at: "2026-10-08T04:28:41Z", intent: "steer", eventId: "steer-echo")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("steer")],
            events: [steerEcho],
            receipts: [steer]
        )
        #expect(resolved == ["steer"])
        let placed = UnrecordedInputs.placedInputs(receipts: [steer], userEvents: [steerEcho], excluding: [])
        #expect(placed.isEmpty)
    }

    @Test
    func linkedEchoOlderThanTheLoadedWindowStaysSettled() {
        let older = userEvent("older", text: "earlier", at: "2026-10-08T04:00:00Z")
        let oldEcho = served("old", text: "from yesterday", at: "2026-10-07T23:00:00Z", eventId: "old-echo")
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("old")],
            events: [older],
            receipts: [oldEcho]
        )
        #expect(resolved == ["old"])
    }

    @Test
    func activeConsoleReceiptResolvesOnceItsEchoIsLinked() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1", phase: .working, turnId: "turn-1")],
            events: [userEvent("echo-1", text: "ship it", at: "2026-10-08T04:00:00Z")],
            receipts: [receipt("req-1", eventId: "echo-1")]
        )
        #expect(resolved == ["req-1"])
    }

    @Test
    func queuedConsoleTurnKeepsItsRowUntilItStarts() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("req-1", phase: .queued, turnId: "turn-1")],
            events: [],
            receipts: [receipt("req-1", eventId: "echo-1")]
        )
        #expect(resolved.isEmpty)
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

    private func served(
        _ requestId: String,
        text: String = "ship it",
        at createdAt: String,
        intent: String = "auto",
        eventId: String? = nil,
        origin: String = "user",
        turnState: String? = "completed"
    ) -> SessionInputReceipt {
        SessionInputReceipt(
            clientRequestId: requestId,
            intent: intent,
            status: "delivered",
            createdAt: createdAt,
            eventId: eventId,
            text: text,
            origin: origin,
            turnState: turnState
        )
    }

    private func assistantEvent(_ id: String, at timestamp: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "assistant",
            contentText: nil,
            toolName: "bash",
            toolInputJSON: nil,
            toolOutputText: "ok",
            toolCallId: "call-\(id)",
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            cursor: id
        )
    }

    private func userEvent(_ id: String, text: String, at timestamp: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "user",
            contentText: text,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            cursor: id
        )
    }

    /// Bug C: a send that settled without a transcript row hands this phone's
    /// "Sent" row over to the served receipt; a lost or running one keeps it.
    @Test
    func settledSendWithoutTranscriptRowHandsOverToItsReceipt() {
        let resolved = SessionViewModel.resolvedSubmittedInputIds(
            submittedInputs: [input("done"), input("lost", phase: .failed), input("running", phase: .working, turnId: "t")],
            events: [],
            receipts: [
                served("done", at: "2026-10-07T00:46:24Z"),
                served("lost", at: "2026-10-07T13:17:33Z", turnState: "failed"),
                served("running", at: "2026-10-07T15:01:28Z", turnState: "active"),
            ]
        )
        #expect(resolved == ["done"])
    }

    /// Bug C: the lost 13:17 send and its 15:01 resend share text; the one
    /// transcript row stands for the resend only.
    @Test
    func transcriptRowStandsForTheNewestSameTextReceiptBeforeIt() {
        let shown = UnrecordedInputs.shownByTranscript(
            receipts: [
                served("ios-lost", text: "keep pushing", at: "2026-10-07T13:17:33Z"),
                served("web-resend", text: "keep pushing", at: "2026-10-07T15:01:28Z"),
            ],
            userEvents: [userEvent("e1", text: "keep  pushing", at: "2026-10-07T15:01:30Z")]
        )
        #expect(shown == ["web-resend"])
    }

    @Test
    func servedReceiptsTheTranscriptLacksArePlacedOnce() {
        let placed = UnrecordedInputs.placedInputs(
            receipts: [
                served("ios-steer", text: "TLDR please", at: "2026-10-07T04:35:20Z", intent: "steer", turnState: nil),
                served("ios-lost", text: "keep pushing", at: "2026-10-07T13:17:33Z", turnState: "failed"),
                served("web-linked", text: "linked", at: "2026-10-07T03:37:05Z", eventId: "evt-1"),
                served("ios-echoed", text: "echoed", at: "2026-10-07T05:18:54Z"),
                served("ios-mine", text: "mine", at: "2026-10-07T06:00:00Z"),
                served("ios-running", text: "running", at: "2026-10-07T07:00:00Z", turnState: "active"),
            ],
            userEvents: [userEvent("e1", text: "echoed", at: "2026-10-07T05:18:56Z")],
            excluding: ["ios-mine"]
        )
        #expect(placed.map(\.clientRequestId) == ["ios-steer", "ios-lost"])
        #expect(placed.map(\.phase) == [.sent, .failed])
        #expect(placed.allSatisfy { $0.placedAtSendTime })
    }

    @Test
    func placedReceiptStandsAtItsSendTimeAmongTranscriptRows() {
        let items = TimelineBuilder.build(events: [
            userEvent("e1", text: "first", at: "2026-10-07T04:00:00Z"),
            userEvent("e2", text: "second", at: "2026-10-07T05:00:00Z"),
        ])
        let placed = UnrecordedInputs.placedInputs(
            receipts: [
                served("ios-steer", text: "TLDR please", at: "2026-10-07T04:35:20Z", intent: "steer", turnState: nil),
                served("ios-old", text: "before the page", at: "2026-10-07T03:00:00Z"),
            ],
            userEvents: [],
            excluding: []
        )
        // While an older page is unloaded, the send before it waits for it.
        let withheld = UnrecordedInputs.placedInputs(
            receipts: [
                served("ios-steer", text: "TLDR please", at: "2026-10-07T04:35:20Z", intent: "steer", turnState: nil),
                served("ios-old", text: "before the page", at: "2026-10-07T03:00:00Z"),
            ],
            userEvents: [],
            excluding: [],
            loadedFrom: LonghouseDateParser.parse("2026-10-07T04:00:00Z")
        )
        #expect(withheld.map(\.clientRequestId) == ["ios-steer"])

        let rows = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: placed)
        // With every row loaded, a send older than the first row leads.
        #expect(rows.map(\.body) == ["before the page", "first", "TLDR please", "second"])
        #expect(rows[2].status == "sent placed")
    }

    @Test
    func backgroundCompletionReceiptsStayAtTheirTimeAsNewMessagesArrive() {
        let receipts = [
            served("wake:second", text: "Second task finished", at: "2026-10-08T04:30:00Z", origin: "wake"),
            served("wake:first", text: "First task finished", at: "2026-10-08T04:10:00Z", origin: "wake"),
            served("close:1", text: "Session closed", at: "2026-10-08T04:40:00Z", origin: "longhouse"),
        ]
        let placed = UnrecordedInputs.placedInputs(receipts: receipts, userEvents: [], excluding: [])
        let events = [
            userEvent("before", text: "Before tasks", at: "2026-10-08T04:00:00Z"),
            userEvent("between", text: "Between tasks", at: "2026-10-08T04:20:00Z"),
            userEvent("after", text: "After tasks", at: "2026-10-08T04:50:00Z"),
        ]
        let rows = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: events),
            submittedInputs: placed
        )
        #expect(rows.map(\.body) == [
            "Before tasks", "First task finished", "Between tasks",
            "Second task finished", "Session closed", "After tasks",
        ])
        let updated = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: events + [
                userEvent("newest", text: "New message", at: "2026-10-08T05:00:00Z")
            ]),
            submittedInputs: placed
        )
        #expect(updated.map(\.body) == rows.map(\.body) + ["New message"])
        #expect(rows.filter { $0.kind == "providerNotification" }.map(\.origin) == [nil, nil, "longhouse"])
    }

    @Test
    func backgroundReceiptOutsideLoadedPageWaitsForOlderHistory() {
        let receipts = [
            served("wake:old", text: "Old task finished", at: "2026-10-08T03:00:00Z", origin: "wake"),
            served("wake:loaded", text: "Recent task finished", at: "2026-10-08T04:10:00Z", origin: "wake"),
        ]
        let placed = UnrecordedInputs.placedInputs(
            receipts: receipts, userEvents: [], excluding: [],
            loadedFrom: LonghouseDateParser.parse("2026-10-08T04:00:00Z")
        )
        #expect(placed.map(\.clientRequestId) == ["wake:loaded"])
        let all = UnrecordedInputs.placedInputs(receipts: receipts, userEvents: [], excluding: [])
        #expect(Set(all.map(\.clientRequestId)) == ["wake:old", "wake:loaded"])
    }

    @Test
    func echoedBackgroundReceiptIsNotDuplicatedAndUndatedNoticeIsRetained() {
        let echo = userEvent(
            id: "echo",
            origin: SessionInputOrigin(authoredVia: .longhouse, origin: "wake", sessionInputId: nil, clientRequestId: "wake:echo"),
            text: "Task finished"
        )
        let placed = UnrecordedInputs.placedInputs(
            receipts: [
                served("wake:echo", text: "Task finished", at: echo.timestamp, eventId: "echo", origin: "wake"),
                served("wake:undated", text: "Undated completion", at: "not-a-date", origin: "wake"),
            ],
            userEvents: [echo], excluding: []
        )
        let rows = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: [echo]), submittedInputs: placed
        )
        #expect(rows.map(\.body) == ["Task finished", "Undated completion"])
        #expect(placed.map(\.clientRequestId) == ["wake:undated"])
        #expect(placed.first?.placedAtSendTime == false)
    }

    private func notification(_ id: String, text: String, at timestamp: String) -> SessionEvent {
        SessionEvent(
            id: id, role: "system", contentText: text, interactionKind: "provider_notification",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil,
            toolCallId: nil, toolCallState: nil, timestamp: timestamp,
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        )
    }

    @Test
    func nativeCompletionReplacesOnlyItsUnambiguousWakeReceipt() {
        let body = "Background command \"Run checks\" completed (exit code 0)"
        let events = [notification("native", text: body, at: "2026-10-08T04:10:00Z")]
        let placed = UnrecordedInputs.placedInputs(
            receipts: [
                served("wake:echo", text: "Background task finished: \(body)", at: "2026-10-08T04:10:04.107Z", origin: "wake"),
                served("wake:later", text: "Background task finished: \(body)", at: "2026-10-08T04:11:00Z", origin: "wake"),
                served("wake:different", text: "Background task finished: Another command", at: "2026-10-08T04:10:01Z", origin: "wake"),
            ],
            userEvents: [], notificationEvents: events, excluding: []
        )
        #expect(placed.map(\.clientRequestId) == ["wake:later", "wake:different"])
        let rows = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: events), submittedInputs: placed
        )
        #expect(rows.map(\.body) == [body, "Background task finished: Another command", "Background task finished: \(body)"])
    }

    @Test
    func truncatedTaskAndAgentWakeSummariesKeepOneCompleteNativeResult() {
        // The server's scalar boundary splits this multi-scalar emoji.
        let summary = String(repeating: "x", count: 511) + "👩‍💻 full result beyond the receipt summary"
        let receiptSummary = String(summary.unicodeScalars.prefix(512))
        for prefix in ["Background task finished: ", "Background agent finished: "] {
            let event = notification("native-long", text: summary, at: "2026-10-08T04:10:00Z")
            let inputs = UnrecordedInputs.placedInputs(
                receipts: [served("wake:long", text: prefix + receiptSummary, at: "2026-10-08T04:10:01Z", origin: "wake")],
                userEvents: [], notificationEvents: [event], excluding: []
            )
            let rows = WebTranscriptView.payloadItems(
                timelineItems: TimelineBuilder.build(events: [event]), submittedInputs: inputs
            )
            #expect(rows.map(\.body) == [summary])
        }
    }

    @Test
    func wrappedAndEngineCappedSummariesKeepOneNativeResult() {
        // The server folds whitespace in the native row; the receipt keeps the raw newline.
        let wrapped = "Background command \"Run checks\"\n  completed (exit code 0)"
        let wrappedEvent = notification("native-wrapped", text: "Background command \"Run checks\" completed (exit code 0)", at: "2026-10-08T04:10:00Z")
        let wrappedInputs = UnrecordedInputs.placedInputs(
            receipts: [served("wake:wrapped", text: "Background task finished: \(wrapped)", at: "2026-10-08T04:10:01Z", origin: "wake")],
            userEvents: [], notificationEvents: [wrappedEvent], excluding: []
        )
        #expect(wrappedInputs.isEmpty)
        // The engine keeps 180 scalars of a Claude task summary.
        let full = String(repeating: "y", count: 200) + " done"
        let capped = String(full.unicodeScalars.prefix(180))
        let cappedEvent = notification("native-capped", text: full, at: "2026-10-08T04:10:00Z")
        let cappedInputs = UnrecordedInputs.placedInputs(
            receipts: [served("wake:capped", text: "Background agent finished: " + capped, at: "2026-10-08T04:10:01Z", origin: "wake")],
            userEvents: [], notificationEvents: [cappedEvent], excluding: []
        )
        #expect(cappedInputs.isEmpty)
    }

    @Test
    func anUncappedSummaryNeverMatchesALongerNativeResultByPrefix() {
        let event = notification("native", text: "Run checks and deploy", at: "2026-10-08T04:10:00Z")
        let inputs = UnrecordedInputs.placedInputs(
            receipts: [served("wake:short", text: "Background task finished: Run checks", at: "2026-10-08T04:10:01Z", origin: "wake")],
            userEvents: [], notificationEvents: [event], excluding: []
        )
        #expect(inputs.map(\.clientRequestId) == ["wake:short"])
    }

    @Test
    func identicalTruncatedSummariesDoNotCollapseDifferentNativeResults() {
        let shared = String(repeating: "x", count: 512)
        let events = [
            notification("native-a", text: shared + " result A", at: "2026-10-08T04:10:00Z"),
            notification("native-b", text: shared + " result B", at: "2026-10-08T04:10:01Z"),
        ]
        let inputs = UnrecordedInputs.placedInputs(
            receipts: [served("wake:ambiguous", text: "Background task finished: " + shared, at: "2026-10-08T04:10:02Z", origin: "wake")],
            userEvents: [], notificationEvents: events, excluding: []
        )
        let rows = WebTranscriptView.payloadItems(
            timelineItems: TimelineBuilder.build(events: events), submittedInputs: inputs
        )
        #expect(rows.map(\.body) == [shared + " result A", shared + " result B", "Background task finished: " + shared])
    }

    @Test
    func ambiguousOrOutOfWindowCompletionsNeverEraseWakeEvidence() {
        let body = "Repeated command completed"
        let receipts = [
            served("wake:one", text: "Background task finished: \(body)", at: "2026-10-08T04:10:01Z", origin: "wake"),
            served("wake:two", text: "Background task finished: \(body)", at: "2026-10-08T04:10:02Z", origin: "wake"),
        ]
        let oneNotice = [notification("native", text: body, at: "2026-10-08T04:10:00Z")]
        let ambiguousReceipts = UnrecordedInputs.placedInputs(
            receipts: receipts, userEvents: [], notificationEvents: oneNotice, excluding: []
        )
        #expect(ambiguousReceipts.map(\.clientRequestId) == ["wake:one", "wake:two"])
        let ambiguousEvents = UnrecordedInputs.placedInputs(
            receipts: [receipts[1]], userEvents: [],
            notificationEvents: oneNotice + [notification("native-again", text: body, at: "2026-10-08T04:10:01Z")],
            excluding: []
        )
        #expect(ambiguousEvents.map(\.clientRequestId) == ["wake:two"])
        let outsideWindow = UnrecordedInputs.placedInputs(
            receipts: [served("wake:late", text: "Background task finished: \(body)", at: "2026-10-08T04:10:05.001Z", origin: "wake")],
            userEvents: [], notificationEvents: oneNotice, excluding: []
        )
        #expect(outsideWindow.map(\.clientRequestId) == ["wake:late"])
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
    func pendingIntentRoundTripsModelAndAttachmentBytesAndIsAccountScoped() {
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
            model: "model-at-send",
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
        let loaded = store.load(
            serverURL: "https://tenant.example/",
            sessionId: "session-1",
            authGeneration: "login-1"
        )
        #expect(loaded == [intent])
        #expect(loaded.first?.model == "model-at-send")
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
    func legacyReceiptRowDefaultsToAcceptedAndPreservesDeliveryStatus() throws {
        let data = try #require("""
        {
          "client_request_id": "req-legacy",
          "intent": "auto",
          "status": "cancelled",
          "event_id": null
        }
        """.data(using: .utf8))
        let receipt = try JSONDecoder.snakeCase.decode(SessionInputReceiptState.self, from: data)
        #expect(receipt.disposition == .accepted)
        #expect(receipt.deliveryStatus == "cancelled")
    }
    @Test
    func runtimeDrainingAPIErrorIsKnownPreDispatchRefusal() {
        let error = LonghouseAPIError.structured(
            status: 503,
            errorCode: "runtime_draining",
            message: "Runtime is restarting."
        )
        #expect(error.isRuntimeDraining)
        #expect(!error.isProviderDeliveryUnknown)
    }
}
