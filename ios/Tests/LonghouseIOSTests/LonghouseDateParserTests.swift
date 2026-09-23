import XCTest
@testable import Longhouse

final class LonghouseDateParserTests: XCTestCase {
    private func formatterDate(_ string: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return fractional.date(from: string) ?? plain.date(from: string)
    }

    func testFastPathAgreesWithISO8601Formatter() throws {
        let inputs = [
            "2026-09-23T14:33:36Z",
            "2026-09-23T14:33:36.9Z",
            "2026-09-23T14:33:36.944Z",
            "2026-09-23T14:33:36.944814Z",
            "2026-09-23T14:33:36.944814+00:00",
            "2026-09-23T10:33:36.944-04:00",
            "2026-09-23T20:03:36+0530",
            "2024-02-29T23:59:59.999Z",
            "2000-02-29T12:00:00Z",
            "2000-03-01T00:00:00Z",
            "1970-01-01T00:00:00Z",
            "2026-01-01T00:00:00.5-12:00",
        ]
        for input in inputs {
            let fast = try XCTUnwrap(LonghouseDateParser.parseInternetDateTime(input), input)
            let reference = try XCTUnwrap(formatterDate(input), input)
            // The formatter keeps milliseconds; the fast path keeps every digit.
            XCTAssertEqual(fast.timeIntervalSince1970, reference.timeIntervalSince1970, accuracy: 0.001, input)
        }
    }

    func testFastPathDeclinesOtherShapesAndParseKeepsFormatterBehavior() {
        let inputs = [
            "", "2026-09-23", "2026-09-23T14:33:36", "2026-09-23 14:33:36Z",
            "2026-13-01T00:00:00Z", "2026-09-23T14:33:36.Z", "2026-09-23T14:33:36Zjunk",
            "2026-02-30T00:00:00Z", "2026-02-29T00:00:00Z", "2026-04-31T00:00:00Z", "1900-02-29T00:00:00Z",
            "not a date at all, but long", "2026-09-23T14:33:60Z",
        ]
        for input in inputs {
            XCTAssertNil(LonghouseDateParser.parseInternetDateTime(input), input)
            XCTAssertEqual(LonghouseDateParser.parse(input), formatterDate(input), input)
        }
    }
}
