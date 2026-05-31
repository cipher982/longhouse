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
        toolCallState: ToolCallState? = nil,
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
            toolCallState: toolCallState,
            timestamp: ts,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    func testPairsAssistantToolWithResultByCallId() {
        let events = [
            event(id: 1, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 2, role: "tool", output: "3 matches", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 1)
        guard case .tool(let call, let result, let pairing) = items[0] else {
            return XCTFail("Expected .tool case")
        }
        XCTAssertEqual(call.id, 1)
        XCTAssertEqual(result?.id, 2)
        XCTAssertEqual(pairing, .id)
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
        guard case .tool(_, let result, _) = items[1] else {
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
        guard case .tool(_, let result, let pairing) = items[0] else {
            return XCTFail("Expected tool")
        }
        XCTAssertNil(result)
        XCTAssertEqual(pairing, .id)
    }

    func testPairsToolWithoutCallIdByFIFO() {
        let events = [
            event(id: 1, role: "assistant", tool: "Grep"),
            event(id: 2, role: "tool", output: "3 matches"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 1)
        guard case .tool(let call, let result, let pairing) = items[0] else {
            return XCTFail("Expected FIFO-paired tool")
        }
        XCTAssertEqual(call.id, 1)
        XCTAssertEqual(result?.id, 2)
        XCTAssertEqual(pairing, .fifo)
    }

    func testMismatchedCallIdDoesNotConsumeFIFOCall() {
        let events = [
            event(id: 1, role: "assistant", tool: "Bash"),
            event(id: 2, role: "tool", output: "stray", callId: "missing"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .tool(let call, let result, let pairing) = items[0] else {
            return XCTFail("Expected pending FIFO call")
        }
        XCTAssertEqual(call.id, 1)
        XCTAssertNil(result)
        XCTAssertEqual(pairing, .pending)
        guard case .orphanTool(let orphan) = items[1] else {
            return XCTFail("Expected mismatched tool result to stay orphaned")
        }
        XCTAssertEqual(orphan.id, 2)
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
            input: ["command": .string("ssh deploy-host 'ls'\necho done")],
            callId: "t1"
        )
        XCTAssertEqual(TimelineBuilder.inputSummary(for: ev), "ssh deploy-host 'ls'")
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

    func testDroppedReadsServerEmittedToolCallState() {
        let dropped = event(id: 1, role: "assistant", tool: "Read", callId: "t1", toolCallState: .dropped)
        XCTAssertTrue(TimelineBuilder.isDropped(call: dropped))

        let running = event(id: 2, role: "assistant", tool: "Read", callId: "t2", toolCallState: .running)
        XCTAssertFalse(TimelineBuilder.isDropped(call: running))

        let completed = event(id: 3, role: "assistant", tool: "Read", callId: "t3", toolCallState: .completed)
        XCTAssertFalse(TimelineBuilder.isDropped(call: completed))

        let unset = event(id: 4, role: "assistant", tool: "Read", callId: "t4")
        XCTAssertFalse(TimelineBuilder.isDropped(call: unset))
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
        XCTAssertEqual(a[1].id, "tool:2")  // single passive stays as .tool
    }

    // MARK: - Passive collapse

    func testSinglePassiveCallStaysAsTool() {
        let events = [
            event(id: 1, role: "user", text: "hi"),
            event(id: 2, role: "assistant", tool: "Read", input: ["file_path": .string("/a.swift")], callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .tool = items[1] else {
            return XCTFail("Single passive call should not collapse into a passiveGroup")
        }
    }

    func testConsecutivePassivesCollapseIntoGroup() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Grep", input: ["pattern": .string("a")], callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Glob", input: ["pattern": .string("*.swift")], callId: "t2"),
            event(id: 5, role: "tool", output: "1 match", callId: "t2"),
            event(id: 6, role: "assistant", tool: "LS", callId: "t3"),
            event(id: 7, role: "tool", output: "ok", callId: "t3"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .passiveGroup(let calls) = items[1] else {
            return XCTFail("Expected passive group")
        }
        XCTAssertEqual(calls.count, 3)
        XCTAssertEqual(calls.map(\.call.id), [2, 4, 6])
        XCTAssertEqual(calls.map(\.result?.id), [3, 5, 7])
    }

    func testActiveToolInMiddleSplitsIntoTwoGroups() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Glob", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "assistant", tool: "Bash", input: ["command": .string("ls")], callId: "t3"),
            event(id: 7, role: "tool", output: "files", callId: "t3"),
            event(id: 8, role: "assistant", tool: "Grep", callId: "t4"),
            event(id: 9, role: "tool", output: "ok", callId: "t4"),
            event(id: 10, role: "assistant", tool: "Glob", callId: "t5"),
            event(id: 11, role: "tool", output: "ok", callId: "t5"),
        ]
        let items = TimelineBuilder.build(events: events)
        // user, passiveGroup(2,4), tool(6=Bash), passiveGroup(8,10)
        XCTAssertEqual(items.count, 4)
        guard case .user = items[0] else { return XCTFail("item 0") }
        guard case .passiveGroup(let first) = items[1] else { return XCTFail("item 1") }
        XCTAssertEqual(first.map(\.call.id), [2, 4])
        guard case .tool(let bashCall, _, _) = items[2] else { return XCTFail("item 2") }
        XCTAssertEqual(bashCall.toolName, "Bash")
        guard case .passiveGroup(let second) = items[3] else { return XCTFail("item 3") }
        XCTAssertEqual(second.map(\.call.id), [8, 10])
    }

    func testBashDoesNotCollapse() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Bash", input: ["command": .string("ls")], callId: "t1"),
            event(id: 3, role: "tool", output: "a", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Bash", input: ["command": .string("pwd")], callId: "t2"),
            event(id: 5, role: "tool", output: "/", callId: "t2"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 3)
        guard case .tool = items[1], case .tool = items[2] else {
            return XCTFail("Bash calls should remain as individual .tool rows")
        }
    }

    func testTaskDoesNotCollapse() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Task", input: ["prompt": .string("do it")], callId: "t1"),
            event(id: 3, role: "tool", output: "done", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Task", input: ["prompt": .string("next")], callId: "t2"),
            event(id: 5, role: "tool", output: "done", callId: "t2"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 3)
        guard case .tool(let a, _, _) = items[1], a.toolName == "Task" else {
            return XCTFail("Task should stay as .tool")
        }
        guard case .tool(let b, _, _) = items[2], b.toolName == "Task" else {
            return XCTFail("Task should stay as .tool")
        }
    }

    func testCodexPassiveNamesAlsoCollapse() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "grep", input: ["pattern": .string("x")], callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "list_files", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "assistant", tool: "find", callId: "t3"),
            event(id: 7, role: "tool", output: "ok", callId: "t3"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .passiveGroup(let calls) = items[1] else {
            return XCTFail("Codex passive names should collapse")
        }
        XCTAssertEqual(calls.count, 3)
        XCTAssertEqual(calls.map(\.call.toolName), ["grep", "list_files", "find"])
    }

    func testUserMessageBreaksPassiveRun() {
        let events = [
            event(id: 1, role: "user", text: "first"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Glob", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "user", text: "second"),
            event(id: 7, role: "assistant", tool: "Grep", callId: "t3"),
            event(id: 8, role: "tool", output: "ok", callId: "t3"),
            event(id: 9, role: "assistant", tool: "Glob", callId: "t4"),
            event(id: 10, role: "tool", output: "ok", callId: "t4"),
        ]
        let items = TimelineBuilder.build(events: events)
        // user, passiveGroup(2,4), user, passiveGroup(7,9)
        XCTAssertEqual(items.count, 4)
        guard case .passiveGroup(let first) = items[1] else { return XCTFail("first group") }
        XCTAssertEqual(first.map(\.call.id), [2, 4])
        guard case .user = items[2] else { return XCTFail("second user") }
        guard case .passiveGroup(let second) = items[3] else { return XCTFail("second group") }
        XCTAssertEqual(second.map(\.call.id), [7, 9])
    }

    func testAssistantProseBetweenPassivesDoesNotCollapseAcross() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Read", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", text: "Let me also search."),
            event(id: 5, role: "assistant", tool: "Grep", callId: "t2"),
            event(id: 6, role: "tool", output: "ok", callId: "t2"),
            event(id: 7, role: "assistant", tool: "Glob", callId: "t3"),
            event(id: 8, role: "tool", output: "ok", callId: "t3"),
        ]
        let items = TimelineBuilder.build(events: events)
        // user, tool(Read single), assistant prose, passiveGroup(Grep, Glob)
        XCTAssertEqual(items.count, 4)
        guard case .tool(let readCall, _, _) = items[1], readCall.toolName == "Read" else {
            return XCTFail("Read alone stays as .tool")
        }
        guard case .assistant = items[2] else { return XCTFail("assistant prose") }
        guard case .passiveGroup(let grp) = items[3] else { return XCTFail("second group") }
        XCTAssertEqual(grp.map(\.call.id), [5, 7])
    }

    func testPassiveGroupStableID() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Glob", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
        ]
        let a = TimelineBuilder.build(events: events)
        let b = TimelineBuilder.build(events: events)
        XCTAssertEqual(a.map(\.id), b.map(\.id))
        XCTAssertEqual(a[1].id, "passive:2")
    }
}
