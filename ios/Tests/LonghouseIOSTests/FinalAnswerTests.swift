import XCTest
@testable import Longhouse

/// Mirrors web/src/lib/sessionWorkspace/__tests__/finalAnswer.test.ts. The two
/// renderings must stay byte-identical: the same session is read on both.
final class FinalAnswerTests: XCTestCase {
    func testRecognizesSchemaConstrainedReturnTool() {
        XCTAssertTrue(ToolTiers.isFinalAnswerTool("StructuredOutput"))
        XCTAssertTrue(ToolTiers.isFinalAnswerTool("structuredoutput"))
        XCTAssertFalse(ToolTiers.isFinalAnswerTool("Bash"))
        XCTAssertFalse(ToolTiers.isFinalAnswerTool("Write"))
    }

    func testLoneStringFieldIsTheWholeAnswer() {
        XCTAssertEqual(
            FinalAnswer.format(.object(["report": .string("Everything landed.")])),
            "Everything landed."
        )
    }

    func testSortedKeysAndBulletedStringLists() {
        let value = JSONValue.object([
            "done": .array([.string("Dropped capabilities."), .string("Documented residual risk.")]),
            "changed": .array([.string("Dockerfile")]),
        ])
        XCTAssertEqual(
            FinalAnswer.format(value),
            "**changed**\n\n- Dockerfile\n\n**done**\n\n- Dropped capabilities.\n- Documented residual risk."
        )
    }

    func testScalarsRenderInline() {
        XCTAssertEqual(
            FinalAnswer.format(.object(["confident": .bool(true), "findings": .int(3)])),
            "**confident**\n\ntrue\n\n**findings**\n\n3"
        )
    }

    func testNestedValuesFallBackToKeySortedFencedJSON() {
        let value = JSONValue.object([
            "verdict": .string("real"),
            "evidence": .object(["line": .int(12), "file": .string("a.py")]),
        ])
        XCTAssertEqual(
            FinalAnswer.format(value),
            "**evidence**\n\n```json\n{\n  \"file\": \"a.py\",\n  \"line\": 12\n}\n```\n\n**verdict**\n\nreal"
        )
    }

    func testNullAndEmptyFieldsAreSkipped() {
        let value = JSONValue.object([
            "notes": .null,
            "skipped": .array([]),
            "summary": .string("Done."),
            "ok": .bool(true),
        ])
        XCTAssertEqual(FinalAnswer.format(value), "**ok**\n\ntrue\n\n**summary**\n\nDone.")
    }

    func testStringPayloads() {
        XCTAssertEqual(FinalAnswer.format(.string("{\"answer\": \"42\"}")), "42")
        XCTAssertEqual(FinalAnswer.format(.string("just prose")), "just prose")
    }

    func testEmptyPayloadKeepsTheToolRow() {
        XCTAssertNil(FinalAnswer.format(nil))
        XCTAssertNil(FinalAnswer.format(.null))
        XCTAssertNil(FinalAnswer.format(.object([:])))
        XCTAssertNil(FinalAnswer.format(.string("")))
        XCTAssertNil(FinalAnswer.format(.object(["summary": .string("   ")])))
    }
}
