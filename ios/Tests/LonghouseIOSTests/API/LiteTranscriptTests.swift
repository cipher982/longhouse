import Foundation
import Testing
@testable import Longhouse

/// A lite mobile-tail page (`detail=lite`) decodes to the same transcript as
/// the full page it was cut from, except for the bodies it sent as previews.
/// Both fixtures are server output: `test_transcript_lite.py` pins the lite
/// one to `lite_projection(full)`.
struct LiteTranscriptTests {
    private func fixture(_ name: String) throws -> Data {
        try Data(contentsOf: RepoRoot.url()
            .appendingPathComponent("tests/fixtures/session-detail")
            .appendingPathComponent(name))
    }

    private func decodePair() throws -> (full: SessionMobileTailResponse, lite: SessionMobileTailResponse) {
        (
            try LonghouseAPI.decodeSessionMobileTail(fixture("lite-mobile-tail.full.json")),
            try LonghouseAPI.decodeSessionMobileTail(fixture("lite-mobile-tail.json"))
        )
    }

    @Test
    func liteDecodesToTheFullTranscriptShape() throws {
        let (full, lite) = try decodePair()

        #expect(lite.projection.items.map(\.id) == full.projection.items.map(\.id))
        #expect(lite.projection.items.map(\.sessionId) == full.projection.items.map(\.sessionId))
        #expect(lite.events.map(\.cursor) == full.events.map(\.cursor))
        #expect(lite.events.map(\.timestamp) == full.events.map(\.timestamp))
        #expect(lite.events.map(\.contentText) == full.events.map(\.contentText))
        #expect(lite.events.map(\.toolName) == full.events.map(\.toolName))
        #expect(lite.events.map(\.toolCallId) == full.events.map(\.toolCallId))
        #expect(lite.pageAnchor == "tail")
    }

    @Test
    func uncutEventsKeepTheirWholeBodiesAndPresentation() throws {
        let (full, lite) = try decodePair()
        let fullById = Dictionary(uniqueKeysWithValues: full.events.map { ($0.id, $0) })

        for event in lite.events where event.liteBodyCursor == nil {
            let source = try #require(fullById[event.id])
            #expect(event.toolInputValue == source.toolInputValue, "input of \(event.id)")
            #expect(event.toolOutputText == source.toolOutputText, "output of \(event.id)")
            #expect(event.toolPresentation == source.toolPresentation, "presentation of \(event.id)")
        }
        // Edits and final answers are never cut: their whole input is what renders.
        let edit = try #require(lite.events.first { $0.toolName == "Edit" })
        #expect(!edit.toolInputTruncated)
        let answer = try #require(lite.events.first { $0.toolName == "StructuredOutput" })
        #expect(!answer.toolInputTruncated)
        #expect(TimelineBuilder.finalAnswerText(for: answer) == TimelineBuilder.finalAnswerText(
            for: try #require(fullById[answer.id])
        ))
    }

    @Test
    func cutEventsSayWhatWasCutAndWhereTheBodyIs() throws {
        let (full, lite) = try decodePair()
        let fullById = Dictionary(uniqueKeysWithValues: full.events.map { ($0.id, $0) })

        let command = try #require(lite.events.first { $0.id == "evt-3" })
        #expect(command.toolInputTruncated)
        #expect(command.liteBodyCursor == "cursor-3")

        let output = try #require(lite.events.first { $0.id == "evt-4" })
        #expect(output.toolOutputTruncated)
        #expect(output.toolOutputOriginalChars == fullById["evt-4"]?.toolOutputText?.count)
        #expect((output.toolOutputText?.count ?? 0) < (fullById["evt-4"]?.toolOutputText?.count ?? 0))
        // The wrapper header survives the cut, so the exit code still parses.
        #expect(ShellSalienceClassifier.parseExitCode(output.toolOutputText) == 0)

        let failure = try #require(lite.events.first { $0.id == "evt-8" })
        #expect(failure.toolOutputFailed)
    }

    @Test
    func aFullPageFromAnOlderServerDecodesUnchanged() throws {
        let data = try fixture("lite-mobile-tail.full.json")
        #expect(try LiteTranscript.hydrateMobileTail(data) == data)
    }

    @Test
    func mobileTailAsksForLitePagesAndSaysWhenItWantsADelta() throws {
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))
        let url = LonghouseAPI.sessionMobileTailURL(
            baseURL: baseURL,
            id: "session-1",
            limit: 50,
            cursor: "cursor-9",
            anchor: "start"
        )
        let items = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
        #expect(items.contains(URLQueryItem(name: "detail", value: "lite")))
        #expect(items.contains(URLQueryItem(name: "anchor", value: "start")))
        #expect(items.contains(URLQueryItem(name: "cursor", value: "cursor-9")))

        let bodies = LonghouseAPI.sessionEventBodiesURL(baseURL: baseURL, id: "session-1", cursors: ["a", "b"])
        #expect(bodies.path == "/api/timeline/sessions/session-1/event-bodies")
        #expect(URLComponents(url: bodies, resolvingAgainstBaseURL: false)?.queryItems == [
            URLQueryItem(name: "cursor", value: "a"),
            URLQueryItem(name: "cursor", value: "b"),
        ])
    }

    @Test
    func eventBodiesDecodeToFullBodies() throws {
        let json = #"""
        {"events": [{"id": "evt-4", "cursor": "cursor-4", "content_text": null, "tool_name": "Bash",
          "tool_input_json": null, "tool_output_text": "the whole output", "tool_presentation": null}],
         "missing": ["cursor-gone"]}
        """#
        let response = try LonghouseAPI.decodeSessionEventBodies(Data(json.utf8))
        #expect(response.events.map(\.cursor) == ["cursor-4"])
        #expect(response.events.first?.toolOutputText == "the whole output")
        #expect(response.missing == ["cursor-gone"])
    }

    // MARK: - Cut rows never mislead

    @Test
    func aCutCommandNeverDemotesToNoise() throws {
        let lite = try LonghouseAPI.decodeSessionMobileTail(fixture("lite-mobile-tail.json"))
        let call = try #require(lite.events.first { $0.id == "evt-3" })
        let result = try #require(lite.events.first { $0.id == "evt-4" })
        #expect(TimelineBuilder.shellSalience(call: call, result: result) == nil)
    }

    @Test
    func aStructuredFailureJudgedOnTheWholeOutputStillFails() throws {
        let lite = try LonghouseAPI.decodeSessionMobileTail(fixture("lite-mobile-tail.json"))
        let call = try #require(lite.events.first { $0.id == "evt-7" })
        let result = try #require(lite.events.first { $0.id == "evt-8" })
        #expect(TimelineBuilder.isFailed(call: call, result: result))
    }

    @Test
    func aCutEditNamesItsFileWithoutACount() {
        let edit = SessionEvent(
            id: "e",
            role: "assistant",
            contentText: nil,
            toolName: "Edit",
            toolInputJSON: ["file_path": .string("/repo/a.swift"), "old_string": .string("a"), "new_string": .string("b\nc")],
            toolOutputText: nil,
            toolCallId: "c",
            toolCallState: .completed,
            timestamp: "2026-10-06T12:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            cursor: "cursor-e",
            toolInputTruncated: true
        )
        let stat = EditSummary.stat(for: edit)
        #expect(stat.fileName == "a.swift")
        #expect(!stat.hasStat)
        #expect(EditSummary.format(stat) == "a.swift")
    }
}

/// What WebKit receives for cut rows, before and after their full bodies load.
struct LiteTranscriptPayloadTests {
    private func liteTail() throws -> SessionMobileTailResponse {
        try LonghouseAPI.decodeSessionMobileTail(Data(contentsOf: RepoRoot.url()
            .appendingPathComponent("tests/fixtures/session-detail/lite-mobile-tail.json")))
    }

    private func fullOutput() throws -> String? {
        try LonghouseAPI.decodeSessionMobileTail(Data(contentsOf: RepoRoot.url()
            .appendingPathComponent("tests/fixtures/session-detail/lite-mobile-tail.full.json")))
            .events.first { $0.id == "evt-4" }?.toolOutputText
    }

    @Test
    func aCutRowCarriesItsBodyCursorsUntilTheBodiesLoad() throws {
        let items = TimelineBuilder.build(items: try liteTail().projection.items)
        let preview = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])
        // The command may render on its own or inside an activity group; find
        // the row by the cut it carries rather than by its id shape.
        let row = try #require(preview.first { $0.bodyCursors?.contains("cursor-3") == true })
        #expect(row.bodyCursors?.starts(with: ["cursor-3", "cursor-4"]) == true)
        #expect(row.bodyState == "preview")

        var bodies = LiteBodyState()
        bodies.loading = ["cursor-3", "cursor-4"]
        let loading = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [], liteBodies: bodies)
        #expect(loading.first { $0.id == row.id }?.bodyState == "loading")

        let whole = try #require(try fullOutput())
        bodies.loading = []
        bodies.bodies = [
            "cursor-3": SessionEventBody(id: "evt-3", cursor: "cursor-3", toolInputJson: .object(["command": .string("rg -n")]), toolOutputText: nil, toolPresentation: nil),
            "cursor-4": SessionEventBody(id: "evt-4", cursor: "cursor-4", toolInputJson: nil, toolOutputText: whole, toolPresentation: nil),
        ]
        let loaded = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [], liteBodies: bodies)
        let full = try #require(loaded.first { $0.id == row.id })
        #expect(full.bodyCursors?.contains("cursor-3") != true)
        #expect(full.bodyCursors?.contains("cursor-4") != true)
        let outputs = [full.output] + full.calls.map(\.output)
        #expect(outputs.contains(whole))
    }

    @Test
    func aBodyTheServerNoLongerHasSaysSo() throws {
        let items = TimelineBuilder.build(items: try liteTail().projection.items)
        let preview = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])
        let row = try #require(preview.first { $0.bodyCursors?.contains("cursor-3") == true })
        var bodies = LiteBodyState()
        bodies.unavailable = Set(row.bodyCursors ?? [])
        let payload = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [], liteBodies: bodies)
        #expect(payload.first { $0.id == row.id }?.bodyState == "unavailable")
    }

    @Test
    func aFailedBodyReadSaysSoInsteadOfLoadingForever() throws {
        let items = TimelineBuilder.build(items: try liteTail().projection.items)
        let preview = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])
        let row = try #require(preview.first { $0.bodyCursors?.contains("cursor-3") == true })
        var bodies = LiteBodyState()
        bodies.failed = ["cursor-3"]
        let payload = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [], liteBodies: bodies)
        #expect(payload.first { $0.id == row.id }?.bodyState == "failed")
        bodies.loading = ["cursor-3"]
        let retrying = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [], liteBodies: bodies)
        #expect(retrying.first { $0.id == row.id }?.bodyState == "loading")
    }

    @Test
    func deltaAnchorsSkipRunningToolsAndProvisionalRows() {
        func item(_ index: Int, state: ToolCallState? = nil, origin: String = "durable", cursor: Bool = true) -> SessionProjectionItem {
            let event = SessionEvent(
                id: "e\(index)",
                role: "assistant",
                contentText: "row \(index)",
                toolName: state == nil ? nil : "Bash",
                toolInputJSON: nil,
                toolOutputText: nil,
                toolCallId: state == nil ? nil : "call-\(index)",
                toolCallState: state,
                timestamp: "2026-10-06T12:00:\(String(format: "%02d", index % 60))Z",
                inActiveContext: true,
                isHeadBranch: true,
                inputOrigin: nil,
                eventOrigin: origin,
                cursor: cursor ? "c\(index)" : nil
            )
            return SessionProjectionItem(
                kind: "event", sessionId: "s", timestamp: event.timestamp, event: event,
                continuedFromSessionId: nil, continuationKind: nil, originLabel: nil,
                parentOriginLabel: nil, parentContinuationKind: nil, branchedFromEventId: nil
            )
        }
        let rows = (0..<40).map { item($0) }
        // 20 rows behind the newest.
        #expect(SessionViewModel.deltaAnchorIndex(in: rows) == 19)
        // Before the oldest tool call still running.
        var running = rows
        running[12] = item(12, state: .running)
        #expect(SessionViewModel.deltaAnchorIndex(in: running) == 11)
        // Never a provisional row or one without a cursor.
        var provisional = rows
        provisional[19] = item(19, origin: "live_provisional")
        provisional[18] = item(18, cursor: false)
        #expect(SessionViewModel.deltaAnchorIndex(in: provisional) == 17)
        // Nothing to anchor on: read the latest window.
        #expect(SessionViewModel.deltaAnchorIndex(in: []) == nil)
        var runningFirst = rows
        runningFirst[0] = item(0, state: .running)
        #expect(SessionViewModel.deltaAnchorIndex(in: runningFirst) == nil)
    }
}
