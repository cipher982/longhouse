import Foundation
import Testing
@testable import Longhouse

struct LonghouseAPITests {
    @Test
    func sessionWorkspaceURLIncludesLimitAndBranchMode() throws {
        let baseURL = try #require(URL(string: "https://david010.longhouse.ai"))

        let url = LonghouseAPI.sessionWorkspaceURL(
            baseURL: baseURL,
            id: "session-1",
            limit: 200,
            branchMode: "head"
        )
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.scheme == "https")
        #expect(components.host == "david010.longhouse.ai")
        #expect(components.path == "/api/timeline/sessions/session-1/workspace")
        #expect(components.queryItems == [
            URLQueryItem(name: "limit", value: "200"),
            URLQueryItem(name: "branch_mode", value: "head"),
        ])
    }

    @Test
    func sessionMobileTailURLIncludesTailPagingFields() throws {
        let baseURL = try #require(URL(string: "https://david010.longhouse.ai"))

        let url = LonghouseAPI.sessionMobileTailURL(
            baseURL: baseURL,
            id: "session-1",
            limit: 50,
            offset: 100,
            branchMode: "head",
            snapshotEventId: 42
        )
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.path == "/api/timeline/sessions/session-1/mobile-tail")
        #expect(components.queryItems == [
            URLQueryItem(name: "limit", value: "50"),
            URLQueryItem(name: "offset", value: "100"),
            URLQueryItem(name: "branch_mode", value: "head"),
            URLQueryItem(name: "snapshot_event_id", value: "42"),
        ])
    }

    @Test
    func sessionWorkspaceStreamURLSkipsInitialSnapshotByDefault() throws {
        let baseURL = try #require(URL(string: "https://david010.longhouse.ai"))

        let url = SessionWorkspaceStream.streamURL(baseURL: baseURL, sessionId: "session-1")
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.path == "/api/timeline/sessions/session-1/workspace/stream")
        #expect(components.queryItems == [
            URLQueryItem(name: "skip_initial", value: "true"),
        ])
    }

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
