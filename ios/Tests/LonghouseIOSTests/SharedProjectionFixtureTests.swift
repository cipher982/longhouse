import XCTest
@testable import Longhouse

final class SharedProjectionFixtureTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let projection: SessionProjectionResponse
        let expectations: Expectations
    }

    private struct Expectations: Decodable {
        let rows: [ExpectedRow]
        let toolCount: Int
        let noiseGroupCount: Int
        let orphanToolIds: [Int]
    }

    private struct ExpectedRow: Decodable, Equatable {
        let kind: String
        let role: String?
        let eventId: Int?
        let toolName: String?
        let callEventId: Int?
        let resultEventId: Int?
        let pairing: String?
        let toolNames: [String]?
        let callEventIds: [Int]?
        let resultEventIds: [Int?]?
        let pairings: [String]?
        let actionKind: String?
        let provider: String?
    }

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

    func testSharedProjectionFixtures() throws {
        for fixtureName in [
            "tool-pairing-fifo.json",
            "context-boundary-noise-collapse.json",
            "session-action-interrupt.json",
            "exploration-run-web-breaks.json",
            "parallel-tool-id-pairing.json",
        ] {
            let fixture = try loadFixture(fixtureName)
            let items = TimelineBuilder.build(items: fixture.projection.items)

            XCTAssertEqual(summarizeRows(items), fixture.expectations.rows, fixtureName)
            XCTAssertEqual(toolInteractionCount(items), fixture.expectations.toolCount, fixtureName)
            XCTAssertEqual(noiseGroupCount(items), fixture.expectations.noiseGroupCount, fixtureName)
            XCTAssertEqual(orphanToolIds(items), fixture.expectations.orphanToolIds, fixtureName)
        }
    }

    func testSharedTranscriptPreviewFixtures() throws {
        let fixture = try loadTranscriptPreviewFixture()
        for fixtureCase in fixture.cases {
            let durableEvents = fixtureCase.projection.items.compactMap(\.event)
            let visibleEvents = TranscriptPreviewProjection.visibleEvents(
                durableEvents: durableEvents,
                preview: fixtureCase.session.transcriptPreview
            )
            let items = TimelineBuilder.build(events: visibleEvents)
            let messages = messageRows(items)

            XCTAssertEqual(messages.ids, fixtureCase.expectations.renderedEventIds, fixtureCase.name)
            XCTAssertEqual(messages.texts, fixtureCase.expectations.renderedMessageTexts, fixtureCase.name)
            XCTAssertEqual(messages.ids.contains(where: { $0 < 0 }), fixtureCase.expectations.rendersPreview, fixtureCase.name)
        }
    }

    func testLiveToolPreviewPreservesTerminalMetadata() {
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

        let event = TranscriptPreviewProjection.visibleEvents(durableEvents: [], preview: preview).first

        XCTAssertEqual(event?.toolName, "exec")
        XCTAssertEqual(event?.toolInputJSON?["command"], .string("pwd"))
        XCTAssertEqual(event?.toolOutputText, "/tmp/project\n")
        XCTAssertEqual(event?.toolCallId, "exec-1")
        XCTAssertEqual(event?.toolCallState, .completed)
    }

    private func loadFixture(_ name: String) throws -> Fixture {
        let fileURL = URL(fileURLWithPath: #filePath)
        let fixtureURL = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/session-projection")
            .appendingPathComponent(name)
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder.snakeCase.decode(Fixture.self, from: data)
    }

    private func loadTranscriptPreviewFixture() throws -> TranscriptPreviewFixture {
        let fileURL = URL(fileURLWithPath: #filePath)
        let fixtureURL = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/session-transcript-preview/rendering.json")
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder.snakeCase.decode(TranscriptPreviewFixture.self, from: data)
    }

    private func summarizeRows(_ items: [TimelineItem]) -> [ExpectedRow] {
        items.map { item in
            switch item {
            case .user(let event), .assistant(let event):
                return ExpectedRow(
                    kind: "message",
                    role: event.role,
                    eventId: event.legacyNumericId,
                    toolName: nil,
                    callEventId: nil,
                    resultEventId: nil,
                    pairing: nil,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil,
                    actionKind: nil,
                    provider: nil
                )
            case .action(let action, _):
                return ExpectedRow(
                    kind: "action",
                    role: nil,
                    eventId: action.eventId,
                    toolName: nil,
                    callEventId: nil,
                    resultEventId: nil,
                    pairing: nil,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil,
                    actionKind: action.kind,
                    provider: action.provider
                )
            case .tool(let call, let result, let pairing):
                return ExpectedRow(
                    kind: "tool",
                    role: nil,
                    eventId: nil,
                    toolName: call.toolName,
                    callEventId: call.legacyNumericId,
                    resultEventId: result?.legacyNumericId,
                    pairing: pairing.rawValue,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil,
                    actionKind: nil,
                    provider: nil
                )
            case .orphanTool(let event):
                return ExpectedRow(
                    kind: "orphan_tool",
                    role: nil,
                    eventId: nil,
                    toolName: event.toolName,
                    callEventId: nil,
                    resultEventId: event.legacyNumericId,
                    pairing: nil,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil,
                    actionKind: nil,
                    provider: nil
                )
            case .passiveGroup(let calls):
                return ExpectedRow(
                    kind: "noise_group",
                    role: nil,
                    eventId: nil,
                    toolName: nil,
                    callEventId: nil,
                    resultEventId: nil,
                    pairing: nil,
                    toolNames: calls.map { $0.call.toolName ?? "" },
                    callEventIds: calls.compactMap(\.call.legacyNumericId),
                    resultEventIds: calls.map { $0.result?.legacyNumericId },
                    pairings: calls.map { $0.pairing.rawValue },
                    actionKind: nil,
                    provider: nil
                )
            }
        }
    }

    private func toolInteractionCount(_ items: [TimelineItem]) -> Int {
        items.reduce(0) { count, item in
            switch item {
            case .tool, .orphanTool:
                return count + 1
            case .passiveGroup(let calls):
                return count + calls.count
            case .user, .assistant, .action:
                return count
            }
        }
    }

    private func noiseGroupCount(_ items: [TimelineItem]) -> Int {
        items.reduce(0) { count, item in
            if case .passiveGroup = item {
                return count + 1
            }
            return count
        }
    }

    private func orphanToolIds(_ items: [TimelineItem]) -> [Int] {
        items.compactMap { item in
            if case .orphanTool(let event) = item {
                return event.legacyNumericId
            }
            return nil
        }
    }

    private func messageRows(_ items: [TimelineItem]) -> (ids: [Int], texts: [String]) {
        let events = items.compactMap { item -> SessionEvent? in
            switch item {
            case .user(let event), .assistant(let event):
                return event
            case .tool, .orphanTool, .passiveGroup, .action:
                return nil
            }
        }
        let compatibilityIds = events.compactMap { event -> Int? in
            if let legacy = event.legacyNumericId { return legacy }
            guard event.id.hasPrefix("synthetic:preview:"),
                  let previewId = Int(event.id.dropFirst("synthetic:preview:".count))
            else { return nil }
            return -abs(previewId)
        }
        return (compatibilityIds, events.map { $0.contentText ?? "" })
    }

}
