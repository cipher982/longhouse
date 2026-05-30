import XCTest
@testable import Longhouse

/// Golden regression guard for the transcript projection: it pins the output of
/// `TimelineBuilder.build` → `WebTranscriptView.payloadItems` (the logical block
/// model the WebKit renderer consumes) against a checked-in golden for hostile
/// real-world cases — markdown prose, paired tool with input+output, large
/// output truncation, a collapsed passive group, and a dropped/orphan result.
///
/// This is a PROJECTION regression guard (catches accidental drift in the
/// shared model), NOT a rendering oracle — it says nothing about how the blocks
/// look. After an intentional projection change, `rm` the golden file and
/// re-run; the test bootstraps it (and skips once), then asserts thereafter.
/// Review the regenerated diff before committing.
final class TranscriptPayloadGoldenTests: XCTestCase {

    private struct Fixture: Decodable {
        let name: String
        let projection: SessionProjectionResponse
    }

    func testHostileTranscriptPayloadMatchesGolden() throws {
        let fixture = try loadFixture("hostile-transcript.json")
        let items = TimelineBuilder.build(events: fixture.projection.items.compactMap(\.event))
        let payloadItems = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let actualData = try encoder.encode(payloadItems)
        let actual = String(decoding: actualData, as: UTF8.self)

        let goldenURL = fixturesRoot().appendingPathComponent("transcript-payload/hostile-transcript.golden.json")

        // Regenerate when the golden is missing (first run / intentional reset
        // via `rm` then re-run) — env vars don't reliably reach the xcodebuild
        // test runner here, so absence is the trigger. A present golden always
        // asserts. Under CI, a MISSING golden is a hard failure (never silently
        // bootstrap+skip), so a committed-golden gap or a broken #filePath
        // resolution can't pass as a skip.
        if !FileManager.default.fileExists(atPath: goldenURL.path) {
            let env = ProcessInfo.processInfo.environment
            let isCI = env["CI"] != nil || env["GITHUB_ACTIONS"] != nil
            if isCI {
                XCTFail("Golden missing under CI at \(goldenURL.path) — it must be committed; not bootstrapping.")
                return
            }
            try (actual + "\n").write(to: goldenURL, atomically: true, encoding: .utf8)
            throw XCTSkip("Bootstrapped golden at \(goldenURL.path) — review the diff and re-run; it will assert from now on.")
        }

        let expected = try String(contentsOf: goldenURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        XCTAssertEqual(
            actual,
            expected,
            "Transcript payload drifted from golden. If intentional, `rm` the golden, re-run to regenerate, and review the diff."
        )
    }

    // Spot-check key semantics directly (so a wrong golden can't silently pass).
    func testHostileTranscriptKeySemantics() throws {
        let fixture = try loadFixture("hostile-transcript.json")
        let items = TimelineBuilder.build(events: fixture.projection.items.compactMap(\.event))
        let payload = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])

        let kinds = payload.map(\.kind)
        XCTAssertEqual(kinds.first, "message", "leads with the user message")
        XCTAssertTrue(kinds.contains("passiveGroup"), "the two Read calls collapse into a passive group")

        // The two consecutive passive Reads collapse; getJiraIssue/Bash stay as tools.
        let passive = payload.first { $0.kind == "passiveGroup" }
        XCTAssertEqual(passive?.calls.count, 2, "passive group holds both Read calls")

        // The orphan/dropped result is surfaced (not erased) with a loud status.
        let dropped = payload.first { $0.status == "dropped" || $0.status == "orphan" }
        XCTAssertNotNil(dropped, "the dropped/orphan tool result must be represented")

        // Tool input JSON flows through to the payload input field.
        let jira = payload.first { $0.title == "getJiraIssue" }
        XCTAssertNotNil(jira?.input, "tool input summary/JSON is carried into the payload")
    }

    // MARK: - Fixture loading (mirrors SharedProjectionFixtureTests)

    private func fixturesRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // LonghouseIOSTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // ios
            .deletingLastPathComponent()   // repo root
            .appendingPathComponent("tests/fixtures")
    }

    private func loadFixture(_ name: String) throws -> Fixture {
        let url = fixturesRoot().appendingPathComponent("transcript-payload/\(name)")
        let data = try Data(contentsOf: url)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Fixture.self, from: data)
    }
}
