import Foundation
import Testing
@testable import Longhouse

struct LonghouseAPITests {
    @Test
    func structuredErrorParsingDecodesHTTPExceptionDetail() throws {
        let data = try #require("""
        {
          "detail": {
            "error_code": "turn_ended",
            "message": "Active turn already ended."
          }
        }
        """.data(using: .utf8))

        let error = LonghouseAPI.parseStructuredError(statusCode: 409, data: data)

        guard case let .structured(status, code, message)? = error else {
            Issue.record("expected structured error, got \(String(describing: error))")
            return
        }
        #expect(status == 409)
        #expect(code == "turn_ended")
        #expect(message == "Active turn already ended.")
    }

    @Test
    func structuredErrorParsingIgnoresUnstructuredDetail() throws {
        let data = try #require("""
        {
          "detail": "Session is busy."
        }
        """.data(using: .utf8))

        #expect(LonghouseAPI.parseStructuredError(statusCode: 409, data: data) == nil)
    }
}
