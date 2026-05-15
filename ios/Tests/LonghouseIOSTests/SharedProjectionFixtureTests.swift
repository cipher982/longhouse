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
    }

    func testToolPairingFIFOFixture() throws {
        let fixture = try loadFixture("tool-pairing-fifo.json")
        let items = TimelineBuilder.build(events: fixture.projection.items.compactMap(\.event))

        XCTAssertEqual(summarizeRows(items), fixture.expectations.rows)
        XCTAssertEqual(toolRows(items).count, fixture.expectations.toolCount)
        XCTAssertEqual(orphanToolIds(items), fixture.expectations.orphanToolIds)
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

    private func summarizeRows(_ items: [TimelineItem]) -> [ExpectedRow] {
        items.map { item in
            switch item {
            case .user(let event), .assistant(let event):
                return ExpectedRow(
                    kind: "message",
                    role: event.role,
                    eventId: event.id,
                    toolName: nil,
                    callEventId: nil,
                    resultEventId: nil,
                    pairing: nil
                )
            case .tool(let call, let result):
                return ExpectedRow(
                    kind: "tool",
                    role: nil,
                    eventId: nil,
                    toolName: call.toolName,
                    callEventId: call.id,
                    resultEventId: result?.id,
                    pairing: toolPairing(call: call, result: result)
                )
            case .orphanTool(let event):
                return ExpectedRow(
                    kind: "orphan_tool",
                    role: nil,
                    eventId: nil,
                    toolName: event.toolName,
                    callEventId: nil,
                    resultEventId: event.id,
                    pairing: nil
                )
            case .passiveGroup:
                XCTFail("This fixture does not expect grouped rows yet")
                return ExpectedRow(
                    kind: "passive_group",
                    role: nil,
                    eventId: nil,
                    toolName: nil,
                    callEventId: nil,
                    resultEventId: nil,
                    pairing: nil
                )
            }
        }
    }

    private func toolRows(_ items: [TimelineItem]) -> [TimelineItem] {
        items.filter { item in
            switch item {
            case .tool, .orphanTool:
                return true
            case .passiveGroup(let calls):
                return !calls.isEmpty
            case .user, .assistant:
                return false
            }
        }
    }

    private func orphanToolIds(_ items: [TimelineItem]) -> [Int] {
        items.compactMap { item in
            if case .orphanTool(let event) = item {
                return event.id
            }
            return nil
        }
    }

    private func toolPairing(call: SessionEvent, result: SessionEvent?) -> String {
        if let callId = call.toolCallId, !callId.isEmpty {
            return "id"
        }
        if result != nil {
            return "fifo"
        }
        return "pending"
    }
}
