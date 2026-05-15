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
    }

    func testSharedProjectionFixtures() throws {
        for fixtureName in ["tool-pairing-fifo.json", "context-boundary-noise-collapse.json"] {
            let fixture = try loadFixture(fixtureName)
            let items = TimelineBuilder.build(events: fixture.projection.items.compactMap(\.event))

            XCTAssertEqual(summarizeRows(items), fixture.expectations.rows, fixtureName)
            XCTAssertEqual(toolInteractionCount(items), fixture.expectations.toolCount, fixtureName)
            XCTAssertEqual(noiseGroupCount(items), fixture.expectations.noiseGroupCount, fixtureName)
            XCTAssertEqual(orphanToolIds(items), fixture.expectations.orphanToolIds, fixtureName)
        }
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
                    pairing: nil,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil
                )
            case .tool(let call, let result, let pairing):
                return ExpectedRow(
                    kind: "tool",
                    role: nil,
                    eventId: nil,
                    toolName: call.toolName,
                    callEventId: call.id,
                    resultEventId: result?.id,
                    pairing: pairing.rawValue,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil
                )
            case .orphanTool(let event):
                return ExpectedRow(
                    kind: "orphan_tool",
                    role: nil,
                    eventId: nil,
                    toolName: event.toolName,
                    callEventId: nil,
                    resultEventId: event.id,
                    pairing: nil,
                    toolNames: nil,
                    callEventIds: nil,
                    resultEventIds: nil,
                    pairings: nil
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
                    callEventIds: calls.map(\.call.id),
                    resultEventIds: calls.map { $0.result?.id },
                    pairings: calls.map { $0.pairing.rawValue }
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
            case .user, .assistant:
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
                return event.id
            }
            return nil
        }
    }

}
