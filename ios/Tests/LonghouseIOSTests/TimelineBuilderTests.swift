import XCTest
@testable import Longhouse

final class TimelineBuilderTests: XCTestCase {
    private func event(
        id: Int,
        role: String,
        text: String? = nil,
        tool: String? = nil,
        input: [String: JSONValue]? = nil,
        output: String? = nil,
        callId: String? = nil,
        ts: String = "2026-04-18T18:00:00Z"
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: text,
            toolName: tool,
            toolInputJSON: input,
            toolOutputText: output,
            toolCallId: callId,
            timestamp: ts,
            inActiveContext: true,
            isHeadBranch: true
        )
    }

    func testPairsAssistantToolWithResultByCallId() {
        let events = [
            event(id: 1, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 2, role: "tool", output: "3 matches", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 1)
        guard case .tool(let call, let result) = items[0] else {
            return XCTFail("Expected .tool case")
        }
        XCTAssertEqual(call.id, 1)
        XCTAssertEqual(result?.id, 2)
    }

    func testSplitsAssistantWithBothTextAndTool() {
        let events = [
            event(id: 1, role: "assistant", text: "Let me check.", tool: "Grep", callId: "t1"),
            event(id: 2, role: "tool", output: "hit", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .assistant(let prose) = items[0] else {
            return XCTFail("Expected .assistant first")
        }
        XCTAssertEqual(prose.contentText, "Let me check.")
        guard case .tool(_, let result) = items[1] else {
            return XCTFail("Expected .tool second")
        }
        XCTAssertEqual(result?.id, 2)
    }

    func testOrphanToolWithNoMatchingCall() {
        let events = [
            event(id: 1, role: "tool", output: "stray", callId: "ghost"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 1)
        guard case .orphanTool(let e) = items[0] else {
            return XCTFail("Expected orphan")
        }
        XCTAssertEqual(e.id, 1)
    }

    func testPendingToolCallHasNilResult() {
        let events = [
            event(id: 1, role: "assistant", tool: "Bash", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        guard case .tool(_, let result) = items[0] else {
            return XCTFail("Expected tool")
        }
        XCTAssertNil(result)
    }

    func testSystemEventsAreFiltered() {
        let events = [
            event(id: 1, role: "system", text: "startup context"),
            event(id: 2, role: "user", text: "hi"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 1)
        guard case .user = items[0] else {
            return XCTFail("Expected user only")
        }
    }

    func testInputSummaryForBashPicksFirstLine() {
        let ev = event(
            id: 1,
            role: "assistant",
            tool: "Bash",
            input: ["command": .string("ssh clifford 'ls'\necho done")],
            callId: "t1"
        )
        XCTAssertEqual(TimelineBuilder.inputSummary(for: ev), "ssh clifford 'ls'")
    }

    func testInputSummaryForGrepReturnsPattern() {
        let ev = event(
            id: 1,
            role: "assistant",
            tool: "Grep",
            input: ["pattern": .string("auth_handler")],
            callId: "t1"
        )
        XCTAssertEqual(TimelineBuilder.inputSummary(for: ev), "auth_handler")
    }

    func testInputSummaryForReadReturnsBasename() {
        let ev = event(
            id: 1,
            role: "assistant",
            tool: "Read",
            input: ["file_path": .string("/repo/src/auth/oauth.ts")],
            callId: "t1"
        )
        XCTAssertEqual(TimelineBuilder.inputSummary(for: ev), "oauth.ts")
    }

    func testDurationFormatting() {
        XCTAssertEqual(TimelineBuilder.formatDuration(0.042), "42ms")
        XCTAssertEqual(TimelineBuilder.formatDuration(2.12), "2.1s")
        XCTAssertEqual(TimelineBuilder.formatDuration(89), "1m 29s")
    }

    func testDurationComputedFromTimestamps() {
        let call = event(id: 1, role: "assistant", tool: "Grep", callId: "t1", ts: "2026-04-18T18:00:00Z")
        let result = event(id: 2, role: "tool", output: "x", callId: "t1", ts: "2026-04-18T18:00:02.5Z")
        let secs = TimelineBuilder.durationSeconds(call: call, result: result)
        XCTAssertNotNil(secs)
        XCTAssertEqual(secs!, 2.5, accuracy: 0.05)
    }

    func testStableIDsAcrossBuilds() {
        let events = [
            event(id: 1, role: "user", text: "hi"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "x", callId: "t1"),
        ]
        let a = TimelineBuilder.build(events: events)
        let b = TimelineBuilder.build(events: events)
        XCTAssertEqual(a.map(\.id), b.map(\.id))
        XCTAssertEqual(a[1].id, "tool:2")  // stable even after result attaches
    }
}
