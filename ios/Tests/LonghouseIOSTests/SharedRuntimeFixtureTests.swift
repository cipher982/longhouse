import XCTest
@testable import Longhouse

final class SharedRuntimeFixtureTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let cases: [RuntimeCase]
    }

    private struct RuntimeCase: Decodable {
        let name: String
        let session: SessionSummary
        let expectations: Expectations
    }

    private struct Expectations: Decodable {
        let managementLabel: String
        let statusLabel: String
        let statusTone: String
        let displayPhaseLabel: String
        let seenAt: String?
        let seenAtPrefix: String
    }

    func testSharedRuntimeFixtures() throws {
        let fixture = try loadFixture("basic-runtime-semantics.json")
        for testCase in fixture.cases {
            let session = testCase.session
            let expected = testCase.expectations

            XCTAssertEqual(session.managementLabel, expected.managementLabel, testCase.name)
            XCTAssertEqual(session.timelineStatusLabel, expected.statusLabel, testCase.name)
            XCTAssertEqual(session.timelineStatusTone, expected.statusTone, testCase.name)
            XCTAssertEqual(session.displayPhaseLabel, expected.displayPhaseLabel, testCase.name)
            XCTAssertEqual(session.timelineStatusSeenAt, expected.seenAt, testCase.name)
            XCTAssertEqual(session.timelineStatusSeenAtPrefix, expected.seenAtPrefix, testCase.name)
        }
    }

    private func loadFixture(_ name: String) throws -> Fixture {
        let fileURL = URL(fileURLWithPath: #filePath)
        let fixtureURL = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/session-runtime")
            .appendingPathComponent(name)
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder.snakeCase.decode(Fixture.self, from: data)
    }
}
