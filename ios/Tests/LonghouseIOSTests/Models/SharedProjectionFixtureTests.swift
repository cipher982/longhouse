import XCTest
@testable import Longhouse

final class SharedProjectionFixtureTests: XCTestCase {
    private struct TranscriptPreviewFixture: Decodable {
        let cases: [TranscriptPreviewCase]
    }

    private struct TranscriptPreviewCase: Decodable {
        let name: String
        let session: TranscriptPreviewSession
        let projection: SessionProjectionResponse
        let expectations: TranscriptPreviewExpectations
    }

    private struct TranscriptPreviewSession: Decodable {
        let id: String
        let transcriptPreview: SessionTranscriptPreview?
    }

    private struct TranscriptPreviewExpectations: Decodable {
        let renderedEventIds: [Int]
        let renderedMessageTexts: [String]
        let rendersPreview: Bool
    }

    func testSharedTranscriptPreviewFixtures() throws {
        let fixture = try loadTranscriptPreviewFixture()
        for fixtureCase in fixture.cases {
            let durableEvents = fixtureCase.projection.items.compactMap(\.event)
            let visibleEvents = TranscriptPreviewProjection.visibleEvents(
                durableEvents: durableEvents,
                preview: fixtureCase.session.transcriptPreview
            )
            let messages = messageRows(visibleEvents)

            XCTAssertEqual(messages.ids, fixtureCase.expectations.renderedEventIds, fixtureCase.name)
            XCTAssertEqual(messages.texts, fixtureCase.expectations.renderedMessageTexts, fixtureCase.name)
            XCTAssertEqual(messages.ids.contains(where: { $0 < 0 }), fixtureCase.expectations.rendersPreview, fixtureCase.name)
        }
    }

    func testLiveToolPreviewPreservesTerminalMetadata() throws {
        let preview = SessionTranscriptPreview(
            eventId: 7,
            text: "/tmp/project",
            role: "assistant",
            toolName: "exec",
            toolInputJSON: ["command": .string("pwd")],
            toolOutputText: "/tmp/project\n",
            toolCallId: "exec-1",
            toolCallState: .completed,
            eventOrigin: "live_provisional",
            timestamp: "2026-07-16T18:00:00Z",
            isProvisional: true,
            isComplete: true,
            contentCursor: "codex_console_live:exec-1:2",
            isStale: false,
            staleReason: nil
        )

        let events = TranscriptPreviewProjection.visibleEvents(durableEvents: [], preview: preview)

        // The preview becomes a call and its result; the document pairs them.
        XCTAssertEqual(events.count, 2)
        let call = try XCTUnwrap(events.first)
        let result = try XCTUnwrap(events.last)
        XCTAssertEqual(call.toolName, "exec")
        XCTAssertEqual(call.toolInputJSON?["command"], .string("pwd"))
        XCTAssertEqual(call.toolCallId, "exec-1")
        XCTAssertEqual(call.toolCallState, .completed)
        XCTAssertEqual(result.toolCallId, "exec-1")
        XCTAssertEqual(result.toolOutputText, "/tmp/project\n")
    }

    private func loadTranscriptPreviewFixture() throws -> TranscriptPreviewFixture {
        let fixtureURL = RepoRoot.url()
            .appendingPathComponent("tests/fixtures/session-transcript-preview/rendering.json")
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder.snakeCase.decode(TranscriptPreviewFixture.self, from: data)
    }

    private func messageRows(_ events: [SessionEvent]) -> (ids: [Int], texts: [String]) {
        let messages = events.filter { $0.role == "user" || $0.role == "assistant" }
            .filter { $0.toolName == nil && $0.interactionKind == nil }
        let compatibilityIds = messages.compactMap { event -> Int? in
            if let legacy = event.legacyNumericId { return legacy }
            guard event.id.hasPrefix("synthetic:preview:"),
                  let previewId = Int(event.id.dropFirst("synthetic:preview:".count))
            else { return nil }
            return -abs(previewId)
        }
        return (compatibilityIds, messages.map { $0.contentText ?? "" })
    }
}
