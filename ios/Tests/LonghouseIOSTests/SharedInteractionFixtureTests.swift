import XCTest
@testable import Longhouse

final class SharedInteractionFixtureTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let cases: [InteractionCase]
    }

    private struct InteractionCase: Decodable {
        let name: String
        let session: SessionDetail
        let expectations: Expectations
    }

    private struct Expectations: Decodable {
        let iosCanSendLive: Bool
        let iosIsControlOffline: Bool
        let iosIsReadOnly: Bool
        let iosRuntimeCapabilityLabel: String
        let iosRuntimeCapabilityTone: String
        let iosControlHealthMessage: String?
        let iosDefaultInputIntent: String
        let iosComposerPlaceholder: String
    }

    func testSharedInteractionCapabilityFixtures() throws {
        let fixture = try loadFixture("capabilities.json")
        for testCase in fixture.cases {
            let session = testCase.session
            let expected = testCase.expectations

            XCTAssertEqual(session.canSendLive, expected.iosCanSendLive, testCase.name)
            XCTAssertEqual(session.isControlOffline, expected.iosIsControlOffline, testCase.name)
            XCTAssertEqual(session.isReadOnly, expected.iosIsReadOnly, testCase.name)
            XCTAssertEqual(session.runtimeCapabilityLabel, expected.iosRuntimeCapabilityLabel, testCase.name)
            XCTAssertEqual(session.runtimeCapabilityTone, expected.iosRuntimeCapabilityTone, testCase.name)
            XCTAssertEqual(session.controlHealthMessage, expected.iosControlHealthMessage, testCase.name)
            XCTAssertEqual(session.defaultInputIntent, expected.iosDefaultInputIntent, testCase.name)
            XCTAssertEqual(session.composerPlaceholder, expected.iosComposerPlaceholder, testCase.name)
        }
    }

    private func loadFixture(_ name: String) throws -> Fixture {
        let fileURL = URL(fileURLWithPath: #filePath)
        let fixtureURL = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/session-interaction")
            .appendingPathComponent(name)
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder.snakeCase.decode(Fixture.self, from: data)
    }
}
