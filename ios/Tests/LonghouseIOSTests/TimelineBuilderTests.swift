import XCTest
@testable import Longhouse

private struct ActivitySummaryFixtureFile: Decodable {
    struct FixtureCase: Decodable {
        let name: String
        let calls: [FixtureCall]
        let expected: String
    }

    struct FixtureCall: Decodable {
        struct Operation: Decodable {
            let key: String
            let label: String
            let count: Int
        }

        let category: String
        let operations: [Operation]?
    }

    let cases: [FixtureCase]
}

final class TimelineBuilderTests: XCTestCase {
    func testExactAliasesAlwaysApplyAndUnknownsStayRaw() {
        XCTAssertEqual(ToolTiers.resolve("view_file").label, "Read")
        XCTAssertEqual(ToolTiers.resolve("CallDynamicTool").label, "CallDynamicTool")
    }

    private func event(
        id: Int,
        role: String,
        text: String? = nil,
        tool: String? = nil,
        input: [String: JSONValue]? = nil,
        output: String? = nil,
        callId: String? = nil,
        toolCallState: ToolCallState? = nil,
        toolPresentation: ToolPresentation? = nil,
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
            toolPresentation: toolPresentation,
            timestamp: ts,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    func testSharedShellActivitySummaryContract() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("../../../config/shell-activity-summary-fixtures.json")
            .standardizedFileURL
        let fixtures = try JSONDecoder().decode(
            ActivitySummaryFixtureFile.self,
            from: Data(contentsOf: fixtureURL)
        )

        for fixture in fixtures.cases {
            let calls = fixture.calls.enumerated().map { index, call in
                let toolName: String
                switch call.category {
                case "read": toolName = "Read"
                case "edit": toolName = "Edit"
                case "wait": toolName = "write_stdin"
                default: toolName = "shell"
                }
                let operations = call.operations?.map { operation in
                    ShellSummaryOperation(
                        key: operation.key,
                        label: operation.label,
                        executable: operation.label.split(separator: " ").first.map(String.init) ?? operation.label,
                        subcommands: [],
                        count: operation.count
                    )
                } ?? []
                let summary = operations.isEmpty ? nil : ShellCommandSummary(
                    version: 1,
                    confidence: "syntactic",
                    operations: operations,
                    candidateCount: operations.count,
                    truncated: false,
                    dynamic: false,
                    parseError: nil,
                    parserId: "fixture",
                    shapeRegistryVersion: 1
                )
                let presentation = ToolPresentation(
                    version: 2,
                    disposition: "direct",
                    toolName: toolName,
                    sourceToolName: toolName,
                    executionMethod: nil,
                    label: toolName,
                    icon: "$",
                    color: "tertiary",
                    tier: "noise",
                    aggregate: call.category == "wait" ? "wait" : nil,
                    mcpNamespace: nil,
                    toolInputValue: .object([:]),
                    ruleId: "fixture",
                    wrapperRecedes: false,
                    children: [],
                    shellSummary: summary
                )
                let callEvent = event(
                    id: index * 2 + 1,
                    role: "assistant",
                    tool: toolName,
                    callId: "fixture-\(index)",
                    toolPresentation: presentation
                )
                let resultEvent = event(
                    id: index * 2 + 2,
                    role: "tool",
                    output: "ok",
                    callId: "fixture-\(index)"
                )
                return ActivityCall(call: callEvent, result: resultEvent, pairing: .id)
            }
            XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), fixture.expected, fixture.name)
        }
    }

    func testCodexPollingWrappersBecomeOneWaitGroup() {
        let presentation = ToolPresentation(
            version: 1,
            disposition: "parsed",
            toolName: "write_stdin",
            sourceToolName: "exec",
            executionMethod: "exec",
            label: "Wait",
            icon: "…",
            color: "tertiary",
            tier: "noise",
            aggregate: "wait",
            mcpNamespace: nil,
            toolInputValue: .object(["session_id": .int(42), "chars": .string("")]),
            ruleId: "codex:exec:single-child:v1",
            wrapperRecedes: true,
            children: []
        )
        var events: [SessionEvent] = []
        for index in 0..<6 {
            let callId = "wait-\(index)"
            events.append(event(id: index * 2 + 1, role: "assistant", tool: "exec", callId: callId, toolPresentation: presentation))
            events.append(event(id: index * 2 + 2, role: "tool", output: "still running", callId: callId))
        }

        let items = TimelineBuilder.build(events: events)

        XCTAssertEqual(items.count, 1)
        guard case .activityGroup(let calls) = items[0] else { return XCTFail("expected wait group") }
        XCTAssertEqual(calls.count, 6)
        XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), "Waited 6")
        let failed = event(
            id: 99,
            role: "tool",
            output: "Process exited with code 1\nOutput:\nfailed",
            callId: "wait-0"
        )
        XCTAssertFalse(TimelineBuilder.isActivityEligible(call: events[0], result: failed, pairing: .id))
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
        XCTAssertEqual(call.id, "1")
        XCTAssertEqual(result?.id, "2")
        XCTAssertEqual(pairing, .id)
    }

    func testQuestionsAndStructuredFailuresStayOutsideActivityGroups() {
        let result = event(id: 2, role: "tool", output: "answered", callId: "q1")
        let question = event(id: 1, role: "assistant", tool: "request_user_input", callId: "q1")
        XCTAssertFalse(TimelineBuilder.isActivityEligible(call: question, result: result, pairing: .id))

        let edit = event(id: 3, role: "assistant", tool: "Edit", callId: "e1")
        let failed = event(id: 4, role: "tool", output: #"{"ok":false,"error":"denied"}"#, callId: "e1")
        XCTAssertFalse(TimelineBuilder.isActivityEligible(call: edit, result: failed, pairing: .id))
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
        XCTAssertEqual(result?.id, "2")
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
        XCTAssertEqual(e.id, "1")
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
        XCTAssertEqual(call.id, "1")
        XCTAssertEqual(result?.id, "2")
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
        XCTAssertEqual(call.id, "1")
        XCTAssertNil(result)
        XCTAssertEqual(pairing, .pending)
        guard case .orphanTool(let orphan) = items[1] else {
            return XCTFail("Expected mismatched tool result to stay orphaned")
        }
        XCTAssertEqual(orphan.id, "2")
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

    func testInputSummaryForProjectedPatchReturnsFiles() {
        let presentation = ToolPresentation(
            version: 1,
            disposition: "parsed",
            toolName: "apply_patch",
            sourceToolName: "exec",
            executionMethod: "exec",
            label: "Edited",
            icon: "E",
            color: "brand",
            tier: "action",
            aggregate: nil,
            mcpNamespace: nil,
            toolInputValue: .object([
                "patch": .string("*** Begin Patch\n*** Update File: app.py\n*** Add File: test_app.py\n*** End Patch")
            ]),
            ruleId: "codex:exec:single-child:v1",
            wrapperRecedes: true,
            children: []
        )
        let ev = event(id: 1, role: "assistant", tool: "exec", callId: "t1", toolPresentation: presentation)

        XCTAssertEqual(TimelineBuilder.inputSummary(for: ev), "app.py + 1 file")
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

    func testAskUserQuestionPayloadRendersAsQuestionNotDroppedTool() {
        let question = event(
            id: 1,
            role: "assistant",
            tool: "AskUserQuestion",
            input: [
                "questions": .array([
                    .object([
                        "id": .string("image_scope"),
                        "header": .string("Image scope"),
                        "question": .string("How should I run the full image download?"),
                        "options": .array([
                            .object([
                                "label": .string("ibsrv first, then external"),
                                "description": .string("Download MBWorld-hosted images first."),
                            ]),
                            .object([
                                "label": .string("Both back-to-back"),
                                "description": .string("Queue both image sets in one run."),
                            ]),
                        ]),
                    ]),
                ]),
            ],
            callId: "toolu-question",
            toolCallState: .dropped
        )
        let items = TimelineBuilder.build(events: [question])
        let payload = WebTranscriptView.payloadItems(timelineItems: items, submittedInputs: [])

        XCTAssertEqual(payload.count, 1)
        XCTAssertEqual(payload[0].kind, "question")
        XCTAssertEqual(payload[0].title, "Image scope")
        XCTAssertEqual(payload[0].subtitle, "Answer in terminal")
        XCTAssertEqual(payload[0].status, "waiting")
        XCTAssertEqual(payload[0].body, "How should I run the full image download?")
        XCTAssertEqual(payload[0].calls.map(\.title), ["ibsrv first, then external", "Both back-to-back"])
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

    // MARK: - Activity collapse

    func testSinglePassiveCallStaysAsTool() {
        let events = [
            event(id: 1, role: "user", text: "hi"),
            event(id: 2, role: "assistant", tool: "Read", input: ["file_path": .string("/a.swift")], callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .tool = items[1] else {
            return XCTFail("Single call should not acquire an activity wrapper")
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
        guard case .activityGroup(let calls) = items[1] else {
            return XCTFail("Expected passive group")
        }
        XCTAssertEqual(calls.count, 3)
        XCTAssertEqual(calls.map(\.call.id), ["2", "4", "6"])
        XCTAssertEqual(calls.map(\.result?.id), ["3", "5", "7"])
    }

    func testCompletedShellMutationJoinsTheActivityRun() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Glob", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "assistant", tool: "Bash", input: ["command": .string("rm -rf build")], callId: "t3"),
            event(id: 7, role: "tool", output: "removed", callId: "t3"),
            event(id: 8, role: "assistant", tool: "Grep", callId: "t4"),
            event(id: 9, role: "tool", output: "ok", callId: "t4"),
            event(id: 10, role: "assistant", tool: "Glob", callId: "t5"),
            event(id: 11, role: "tool", output: "ok", callId: "t5"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .user = items[0] else { return XCTFail("item 0") }
        guard case .activityGroup(let calls) = items[1] else { return XCTFail("item 1") }
        XCTAssertEqual(calls.map(\.call.id), ["2", "4", "6", "8", "10"])
        XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), "Searched 2 · Listed 2 · Ran 1")
    }

    func testCompletedOpaqueBashCallsCollapse() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Bash", input: ["command": .string("make test")], callId: "t1"),
            event(id: 3, role: "tool", output: "passed", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Bash", input: ["command": .string("touch marker")], callId: "t2"),
            event(id: 5, role: "tool", output: "", callId: "t2"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .activityGroup(let calls) = items[1] else { return XCTFail("expected activity group") }
        XCTAssertEqual(calls.count, 2)
    }

    func testTasksCollapseAsCalledActivity() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Task", input: ["prompt": .string("do it")], callId: "t1"),
            event(id: 3, role: "tool", output: "done", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Task", input: ["prompt": .string("next")], callId: "t2"),
            event(id: 5, role: "tool", output: "done", callId: "t2"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .activityGroup(let calls) = items[1] else { return XCTFail("expected activity group") }
        XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), "Called 2")
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
        guard case .activityGroup(let calls) = items[1] else {
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
        guard case .activityGroup(let first) = items[1] else { return XCTFail("first group") }
        XCTAssertEqual(first.map(\.call.id), ["2", "4"])
        guard case .user = items[2] else { return XCTFail("second user") }
        guard case .activityGroup(let second) = items[3] else { return XCTFail("second group") }
        XCTAssertEqual(second.map(\.call.id), ["7", "9"])
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
        guard case .activityGroup(let grp) = items[3] else { return XCTFail("second group") }
        XCTAssertEqual(grp.map(\.call.id), ["5", "7"])
        XCTAssertEqual(TimelineBuilder.activitySummary(for: grp), "Searched 1 · Listed 1")
    }

    func testReadJoinsConsecutiveExplorationRun() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Read", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Grep", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "assistant", tool: "Glob", callId: "t3"),
            event(id: 7, role: "tool", output: "ok", callId: "t3"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .activityGroup(let calls) = items[1] else {
            return XCTFail("Read+Grep+Glob should collapse into one exploration run")
        }
        XCTAssertEqual(calls.map(\.call.toolName), ["Read", "Grep", "Glob"])
        XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), "Searched 1 · Read 1 · Listed 1")
    }

    func testWebFetchJoinsActivityRun() {
        let events = [
            event(id: 1, role: "user", text: "go"),
            event(id: 2, role: "assistant", tool: "Grep", callId: "t1"),
            event(id: 3, role: "tool", output: "ok", callId: "t1"),
            event(id: 4, role: "assistant", tool: "Grep", callId: "t2"),
            event(id: 5, role: "tool", output: "ok", callId: "t2"),
            event(id: 6, role: "assistant", tool: "WebFetch", callId: "t3"),
            event(id: 7, role: "tool", output: "html", callId: "t3"),
        ]
        let items = TimelineBuilder.build(events: events)
        XCTAssertEqual(items.count, 2)
        guard case .activityGroup(let calls) = items[1] else { return XCTFail("activity group") }
        XCTAssertEqual(calls.count, 3)
        XCTAssertEqual(TimelineBuilder.activitySummary(for: calls), "Searched 2 · Viewed 1")
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
        XCTAssertEqual(a[1].id, "activity:2")
    }
}
