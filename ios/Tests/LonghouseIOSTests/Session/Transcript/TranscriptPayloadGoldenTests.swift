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
        XCTAssertTrue(kinds.contains("activityGroup"), "the shell read and two searches collapse into an activity group")

        // The read-only Bash call and two consecutive searches collapse;
        // getJiraIssue stays as a full tool row.
        let activity = payload.first { $0.kind == "activityGroup" }
        XCTAssertEqual(activity?.calls.count, 4, "activity group holds all completed supporting calls")

        // The orphan/dropped result is surfaced (not erased) with a loud status.
        let dropped = payload.first { $0.status == "dropped" || $0.status == "orphan" }
        XCTAssertNotNil(dropped, "the dropped/orphan tool result must be represented")

        // Tool input JSON flows through to the payload input field.
        let jira = payload.first { $0.title == "getJiraIssue" }
        XCTAssertNotNil(jira?.input, "tool input summary/JSON is carried into the payload")
    }

    func testClaudeFidelityPayloadCarriesNewSignalsAndNestedSubagents() throws {
        let subagents = [
            SessionSubagent(
                sessionId: "sub-1",
                provider: "claude",
                parentToolCallId: "toolu_agent_1",
                runId: nil,
                startedAt: "2026-11-15T10:00:00Z",
                lastActivityAt: "2026-11-15T10:02:00Z",
                endedAt: nil,
                userMessages: 1,
                assistantMessages: 2,
                toolCalls: 5,
                title: "Parent task",
                firstUserMessagePreview: nil,
                lastVisibleTextPreview: nil,
                agentType: "general-purpose",
                description: "Audit runtime dock",
                model: "claude-opus-5-5",
                isFork: true,
                spawnDepth: 1,
                liveStatus: "running",
                durationMs: 120_000,
                totalTokens: 54_000
            ),
            SessionSubagent(
                sessionId: "sub-2",
                provider: "claude",
                parentToolCallId: nil,
                runId: nil,
                startedAt: "2026-11-15T10:00:30Z",
                lastActivityAt: nil,
                endedAt: nil,
                userMessages: 1,
                assistantMessages: 1,
                toolCalls: 2,
                title: "Child task",
                firstUserMessagePreview: nil,
                lastVisibleTextPreview: nil,
                parentAgentId: "sub-1",
                agentType: "Explore",
                description: "Inspect Swift files",
                isFork: false,
                spawnDepth: 2,
                liveStatus: "waiting",
                durationMs: 45_000,
                totalTokens: 12_500
            ),
        ]
        let events = [
            SessionEvent(
                id: 1,
                role: "system",
                contentText: nil,
                interactionKind: "provider_reasoning",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:00:00Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                reasoningWithheld: true
            ),
            SessionEvent(
                id: 2,
                role: "assistant",
                contentText: "Checking child subagent status.",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:00:02Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                messagePhase: "commentary"
            ),
            SessionEvent(
                id: 3,
                role: "assistant",
                contentText: nil,
                toolName: "Agent",
                toolInputJSON: ["description": .string("Audit runtime dock"), "subagent_type": .string("general-purpose")],
                toolOutputText: "Working on sub-1 output",
                toolCallId: "toolu_agent_1",
                toolCallState: .running,
                timestamp: "2026-11-15T10:00:03Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                toolIntent: "Audit Claude subagent parity"
            ),
            SessionEvent(
                id: 4,
                role: "system",
                contentText: "Agent finished",
                interactionKind: "provider_notification",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:01:00Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                taskNotification: ["status": .string("completed"), "duration_ms": .int(5000), "subagent_tokens": .int(12000)]
            ),
            SessionEvent(
                id: 5,
                role: "system",
                contentText: "Waiting for 2 background agents to finish",
                interactionKind: "provider_notification",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:01:05Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                waitingBackgroundAgents: 2
            ),
            SessionEvent(
                id: 6,
                role: "system",
                contentText: "Overloaded",
                interactionKind: "provider_notification",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:01:10Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                apiError: ["message": .string("Overloaded"), "retry_attempt": .int(1), "max_retries": .int(3)]
            ),
            SessionEvent(
                id: 7,
                role: "assistant",
                contentText: "Done.",
                toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil, toolCallState: nil,
                timestamp: "2026-11-15T10:02:00Z", inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                turnEnd: SessionTurnEnd(
                    durationMs: 12000,
                    endedAt: "2026-11-15T10:02:00Z",
                    messageCount: nil,
                    outcome: "completed",
                    stopHookContinued: false,
                    stopHookSummary: "All clean"
                ),
                messagePhase: "final_answer"
            ),
        ]

        let timeline = TimelineBuilder.build(events: events, subagents: subagents)
        let payload = WebTranscriptView.payloadItems(
            timelineItems: timeline,
            subagents: subagents,
            submittedInputs: []
        )

        XCTAssertEqual(payload.count, 7)
        XCTAssertEqual(payload[0].reasoningWithheld, true)
        XCTAssertEqual(payload[1].phase, "commentary")
        XCTAssertEqual(payload[2].toolIntent, "Audit Claude subagent parity")
        XCTAssertNotNil(payload[2].startedAtMs)
        XCTAssertEqual(payload[2].output, "Working on sub-1 output")
        XCTAssertEqual(payload[2].subagents?.map(\.sessionId), ["sub-1", "sub-2"])
        XCTAssertEqual(payload[2].subagents?.map { $0.depth ?? 0 }, [0, 1])
        XCTAssertEqual(payload[2].subagents?.first?.isFork, true)
        XCTAssertNotNil(payload[3].taskNotification)
        XCTAssertEqual(payload[4].waitingBackgroundAgents, 2)
        XCTAssertNotNil(payload[5].apiError)
        XCTAssertTrue(payload[6].turnEnd?.doneAt.contains("All clean") == true)
    }

    // MARK: - Fixture loading (mirrors SharedProjectionFixtureTests)

    private func fixturesRoot() -> URL {
        RepoRoot.url()
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
