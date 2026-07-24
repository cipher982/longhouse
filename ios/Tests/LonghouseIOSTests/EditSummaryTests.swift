import XCTest
@testable import Longhouse

/// Edit-shape detection, diff stats, and the failure preview
/// (`docs/specs/transcript-action-visibility.md`). Must stay behaviorally
/// identical to `web/src/lib/__tests__/editSummary.test.ts`.
final class EditSummaryTests: XCTestCase {
    private func editEvent(_ input: [String: JSONValue]) -> SessionEvent {
        SessionEvent(
            id: "1",
            role: "assistant",
            contentText: nil,
            toolName: "Edit",
            toolInputJSON: input,
            toolOutputText: nil,
            toolCallId: "e1",
            toolCallState: .completed,
            toolPresentation: nil,
            timestamp: "2026-04-18T18:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    func testReplaceCountsViaLineDiffAndShowsOnlyBasename() {
        let stat = EditSummary.stat(for: editEvent([
            "file_path": .string("a/b/c/timelineModel.ts"),
            "old_string": .string("a\nb\nc"),
            "new_string": .string("a\nB\nc"),
        ]))
        XCTAssertEqual(stat.added, 1)
        XCTAssertEqual(stat.removed, 1)
        XCTAssertEqual(stat.filePath, "a/b/c/timelineModel.ts")
        XCTAssertEqual(EditSummary.format(stat), "timelineModel.ts +1 −1")
    }

    func testWriteIsAllAddedAndLoneOldStringIsAllRemoved() {
        let created = EditSummary.stat(for: editEvent([
            "file_path": .string("n.ts"), "content": .string("1\n2\n3"),
        ]))
        XCTAssertEqual(EditSummary.format(created), "n.ts +3 −0")

        let deleted = EditSummary.stat(for: editEvent([
            "file_path": .string("d.ts"), "old_string": .string("1\n2"),
        ]))
        XCTAssertEqual(EditSummary.format(deleted), "d.ts +0 −2")
    }

    func testApplyPatchCountsHunksAndIgnoresFileHeaders() {
        let stat = EditSummary.stat(for: editEvent([
            "file_path": .string("p.ts"),
            "patch": .string("--- a/p.ts\n+++ b/p.ts\n@@\n-old\n+new\n+extra"),
        ]))
        XCTAssertEqual(stat.added, 2)
        XCTAssertEqual(stat.removed, 1)
    }

    func testUnknownShapeNamesTheFileButNeverFabricatesAStat() {
        let stat = EditSummary.stat(for: editEvent([
            "file_path": .string("mystery.ts"), "mode": .string("rewrite"),
        ]))
        XCTAssertFalse(stat.hasStat)
        XCTAssertEqual(EditSummary.format(stat), "mystery.ts")
    }

    func testNoRecoverableInputRendersNothing() {
        XCTAssertNil(EditSummary.format(EditSummary.stat(for: editEvent([:]))))
    }

    func testOversizedReplaceSkipsTheLCSEntirely() {
        // The guard must fire *before* the quadratic table is allocated, so a
        // diff this large has to be cheap rather than merely un-rendered.
        let count = Int(Double(EditSummary.diffCellBudget).squareRoot()) + 10
        let oldStr = (0..<count).map { "old \($0)" }.joined(separator: "\n")
        let newStr = (0..<count).map { "new \($0)" }.joined(separator: "\n")

        let started = Date()
        let stat = EditSummary.stat(for: editEvent([
            "file_path": .string("huge.ts"),
            "old_string": .string(oldStr),
            "new_string": .string(newStr),
        ]))
        let elapsed = Date().timeIntervalSince(started)

        XCTAssertFalse(stat.hasStat)
        XCTAssertEqual(EditSummary.format(stat), "huge.ts")
        XCTAssertLessThan(elapsed, 1.0)
    }

    /// Regression: gating diffs on "the input has a text-ish key" instead of on
    /// the edit category let ordinary tools masquerade as file creations and
    /// replaced their Input block with a bogus diff, which broke transcript
    /// rendering for tool-bearing fixtures.
    func testNonEditToolsAreNotTreatedAsEdits() {
        let shell = SessionEvent(
            id: "20", role: "assistant", contentText: nil, toolName: "Bash",
            toolInputJSON: ["command": .string("make test"), "content": .string("a\nb")],
            toolOutputText: nil, toolCallId: "s1", toolCallState: .completed,
            toolPresentation: nil, timestamp: "2026-04-18T18:00:00Z",
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        )
        XCTAssertFalse(TimelineBuilder.isEditInteraction(shell))

        let edit = editEvent(["file_path": .string("a.ts"), "old_string": .string("x"), "new_string": .string("y")])
        XCTAssertTrue(TimelineBuilder.isEditInteraction(edit))
    }

    func testFailurePreviewKeepsHeadAndTail() {
        let body = (0..<40).map { "line \($0)" }.joined(separator: "\n")
        let call = SessionEvent(
            id: "10", role: "assistant", contentText: nil, toolName: "Bash",
            toolInputJSON: ["command": .string("make test")], toolOutputText: nil,
            toolCallId: "f1", toolCallState: .completed, toolPresentation: nil,
            timestamp: "2026-04-18T18:00:00Z", inActiveContext: true,
            isHeadBranch: true, inputOrigin: nil
        )
        let result = SessionEvent(
            id: "11", role: "tool", contentText: nil, toolName: "Bash",
            toolInputJSON: nil,
            toolOutputText: "Wall time: 1.0 seconds\nProcess exited with code 2\nOutput:\n\(body)",
            toolCallId: "f1", toolCallState: nil, toolPresentation: nil,
            timestamp: "2026-04-18T18:00:05Z", inActiveContext: true,
            isHeadBranch: true, inputOrigin: nil
        )

        XCTAssertTrue(TimelineBuilder.isFailed(call: call, result: result))
        let preview = TimelineBuilder.failurePreview(call: call, result: result)
        // Head is kept as well as tail: a stack trace's heading must survive.
        XCTAssertNotNil(preview)
        XCTAssertTrue(preview?.contains("Wall time") == true)
        XCTAssertTrue(preview?.contains("line 39") == true)
        XCTAssertTrue(preview?.contains("more lines") == true)
        XCTAssertFalse(preview?.contains("line 20") == true)
    }

    func testHugeSingleLineFailureIsCharacterBounded() {
        let call = SessionEvent(
            id: "12", role: "assistant", contentText: nil, toolName: "Bash",
            toolInputJSON: nil, toolOutputText: nil, toolCallId: "f2",
            toolCallState: .completed, toolPresentation: nil,
            timestamp: "2026-04-18T18:00:00Z", inActiveContext: true,
            isHeadBranch: true, inputOrigin: nil
        )
        let result = SessionEvent(
            id: "13", role: "tool", contentText: nil, toolName: "Bash",
            toolInputJSON: nil,
            toolOutputText: "{\"ok\": false, \"error\": \"\(String(repeating: "x", count: 20_000))\"}",
            toolCallId: "f2", toolCallState: nil, toolPresentation: nil,
            timestamp: "2026-04-18T18:00:05Z", inActiveContext: true,
            isHeadBranch: true, inputOrigin: nil
        )

        let preview = TimelineBuilder.failurePreview(call: call, result: result)
        XCTAssertNotNil(preview)
        // A one-line megabyte error must not escape via the line bound.
        XCTAssertLessThanOrEqual(
            preview?.count ?? 0,
            TimelineBuilder.failurePreviewMaxChars + 20
        )
    }
}
