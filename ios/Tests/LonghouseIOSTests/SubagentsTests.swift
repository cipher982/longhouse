import XCTest
@testable import Longhouse

/// Mirrors web/src/lib/sessionWorkspace/__tests__/subagents.test.ts. The two
/// surfaces read the same session, so the binding must agree case for case.
final class SubagentsTests: XCTestCase {
    private static let workflowResult = """
    Workflow launched in background. Task ID: wkjlcxlhn
    Transcript dir: /Users/d/.claude/projects/-p/02b4c48f/subagents/workflows/wf_4f413c80-395
    Script file: /Users/d/.claude/projects/-p/workflows/scripts/cp-wf_4f413c80-395.js
    """

    private func child(
        sessionId: String = "child-1",
        parentToolCallId: String? = nil,
        runId: String? = nil,
        startedAt: String? = "2026-08-25T03:30:00Z",
        endedAt: String? = "2026-08-25T03:31:00Z",
        title: String? = nil,
        firstUserMessagePreview: String? = nil,
        toolCalls: Int = 12
    ) -> SessionSubagent {
        SessionSubagent(
            sessionId: sessionId,
            provider: "claude",
            parentToolCallId: parentToolCallId,
            runId: runId,
            startedAt: startedAt,
            lastActivityAt: endedAt,
            endedAt: endedAt,
            userMessages: 1,
            assistantMessages: 3,
            toolCalls: toolCalls,
            title: title,
            firstUserMessagePreview: firstUserMessagePreview,
            lastVisibleTextPreview: nil
        )
    }

    func testReadsRunIdTheParentToolResultNames() {
        XCTAssertEqual(Subagents.runIds(inToolOutput: Self.workflowResult), ["wf_4f413c80-395"])
        XCTAssertEqual(Subagents.runIds(inToolOutput: "Workflow launched. Task ID: wkjlcxlhn"), [])
        XCTAssertEqual(Subagents.runIds(inToolOutput: nil), [])
    }

    func testBindsTaskChildByTheToolCallItsSidecarNamed() {
        let task = child(sessionId: "task", parentToolCallId: "toolu_a")
        let bound = Subagents.children(from: [task], toolCallId: "toolu_a", toolOutputText: nil)
        XCTAssertEqual(bound.map(\.sessionId), ["task"])
    }

    func testBindsWholeWorkflowRunThroughTheParentToolResult() {
        let workers = [
            child(sessionId: "w1", runId: "wf_4f413c80-395", startedAt: "2026-08-25T03:30:00Z"),
            child(sessionId: "w2", runId: "wf_4f413c80-395", startedAt: "2026-08-25T03:29:00Z"),
        ]
        let bound = Subagents.children(from: workers, toolCallId: "toolu_wf", toolOutputText: Self.workflowResult)
        XCTAssertEqual(bound.map(\.sessionId), ["w2", "w1"])
    }

    func testFailsClosedOnAnUnnamedRun() {
        let orphan = child(sessionId: "w1", runId: "wf_other")
        XCTAssertTrue(Subagents.children(from: [orphan], toolCallId: "toolu_wf", toolOutputText: Self.workflowResult).isEmpty)
        XCTAssertTrue(Subagents.children(from: [orphan], toolCallId: nil, toolOutputText: nil).isEmpty)
    }

    func testNeverBindsByProximity() {
        let worker = child(sessionId: "w1", runId: "wf_4f413c80-395")
        XCTAssertTrue(
            Subagents.children(from: [worker], toolCallId: "toolu_unrelated", toolOutputText: "ran a command").isEmpty
        )
    }

    func testSummaryStatesTheShapeOfTheWork() {
        let workers = [
            child(startedAt: "2026-08-25T03:30:00Z", endedAt: "2026-08-25T03:34:12Z"),
            child(sessionId: "w2", startedAt: "2026-08-25T03:30:05Z", endedAt: "2026-08-25T03:32:00Z"),
        ]
        XCTAssertEqual(Subagents.summary(workers), "2 agents · 4m12s")
        XCTAssertEqual(Subagents.summary([child(endedAt: nil)]), "1 agent")
    }

    func testLabelPrefersTitleThenPrompt() {
        XCTAssertEqual(Subagents.label(for: child(title: "Harden the container")), "Harden the container")
        XCTAssertEqual(
            Subagents.label(for: child(firstUserMessagePreview: "You are one of ~22 agents")),
            "You are one of ~22 agents"
        )
        XCTAssertEqual(Subagents.label(for: child(sessionId: "abcdef1234")), "abcdef12")
    }
}
