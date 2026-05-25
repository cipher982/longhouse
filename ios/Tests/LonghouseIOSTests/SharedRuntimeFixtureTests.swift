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
        let detailSession: SessionDetail
        let expectations: Expectations

        private enum CodingKeys: String, CodingKey {
            case name
            case session
            case expectations
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            name = try container.decode(String.self, forKey: .name)
            session = try container.decode(SessionSummary.self, forKey: .session)
            detailSession = try container.decode(SessionDetail.self, forKey: .session)
            expectations = try container.decode(Expectations.self, forKey: .expectations)
        }
    }

    private struct Expectations: Decodable {
        let managementLabel: String
        let statusLabel: String
        let statusTone: String
        let displayPhaseLabel: String
        let runtimeHeadline: String
        let runtimeDetail: String?
        let runtimeTone: String
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
            XCTAssertEqual(testCase.detailSession.runtimeHeadline, expected.runtimeHeadline, testCase.name)
            XCTAssertEqual(testCase.detailSession.runtimeDetail, expected.runtimeDetail, testCase.name)
            XCTAssertEqual(testCase.detailSession.runtimeTone, expected.runtimeTone, testCase.name)
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
