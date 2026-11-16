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
        #expect(output.toolOutputText?.contains("Process exited with code 0") == true)

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
}

/// What WebKit receives for cut rows: the served events with their cursors,
/// and the bodies loaded so far. The document (shared TypeScript model) decides
/// each row's body state; web/.../presentTranscript.test.ts pins that.
struct LiteTranscriptPayloadTests {
    private func liteTail() throws -> SessionMobileTailResponse {
        try LonghouseAPI.decodeSessionMobileTail(Data(contentsOf: RepoRoot.url()
            .appendingPathComponent("tests/fixtures/session-detail/lite-mobile-tail.json")))
    }

    private func payload(_ liteBodies: LiteBodyState) throws -> [String: Any] {
        let data = WebTranscriptView.payloadData(
            serverURL: "https://example.test",
            projectionItems: try liteTail().projection.items,
            subagents: [],
            submittedInputs: [],
            errorMessage: nil,
            liteBodies: liteBodies
        )
        let root = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try #require(root["projection"] as? [String: Any])
    }

    @Test
    func cutRowsKeepTheirCursorsAndLoadedBodiesTravelBesideThem() throws {
        var bodies = LiteBodyState()
        bodies.loading = ["cursor-3"]
        bodies.failed = ["cursor-9"]
        bodies.bodies = [
            "cursor-4": SessionEventBody(id: "evt-4", cursor: "cursor-4", toolInputJson: nil, toolOutputText: "whole", toolPresentation: nil)
        ]
        let projection = try payload(bodies)
        let items = try #require(projection["items"] as? [[String: Any]])
        let events = items.compactMap { $0["event"] as? [String: Any] }
        // Wire keys are snake_case, the shape the shared model reads.
        #expect(events.contains { $0["cursor"] as? String == "cursor-3" && ($0["tool_input_truncated"] != nil || $0["tool_output_truncated"] != nil) })
        #expect(events.allSatisfy { $0["contentText"] == nil && $0["toolName"] == nil })
        let lite = try #require(projection["liteBodies"] as? [String: Any])
        #expect(lite["loading"] as? [String] == ["cursor-3"])
        #expect(lite["failed"] as? [String] == ["cursor-9"])
        let loaded = try #require(lite["bodies"] as? [[String: Any]])
        #expect(loaded.first?["tool_output_text"] as? String == "whole")
        #expect(projection["serverUrl"] as? String == "https://example.test")
    }

    @Test
    func toolInputKeysAreProviderOwnedAndNeverRewritten() throws {
        let projection = try payload(LiteBodyState())
        let items = try #require(projection["items"] as? [[String: Any]])
        let edit = try #require(items.compactMap { $0["event"] as? [String: Any] }.first { $0["tool_name"] as? String == "Edit" })
        let input = try #require(edit["tool_input_json"] as? [String: Any])
        #expect(input.keys.contains("file_path"))
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
