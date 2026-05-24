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
    func structuredErrorParsingAlsoAcceptsCodeField() throws {
        let data = try #require("""
        {
          "detail": {
            "code": "send_failed",
            "message": "Session is not managed_local"
          }
        }
        """.data(using: .utf8))

        let error = LonghouseAPI.parseStructuredError(statusCode: 502, data: data)

        guard case let .structured(status, code, message)? = error else {
            Issue.record("expected structured error, got \(String(describing: error))")
            return
        }
        #expect(status == 502)
        #expect(code == "send_failed")
        #expect(message == "Session is not managed_local")
    }

    @Test
    func launchErrorParsingAcceptsErrorCodeField() throws {
        let data = try #require("""
        {
          "detail": {
            "error_code": "cwd_not_found",
            "message": "Directory does not exist"
          }
        }
        """.data(using: .utf8))

        let error = LonghouseAPI.parseLaunchError(statusCode: 400, data: data)

        guard case let .structured(status, code, message)? = error else {
            Issue.record("expected structured error, got \(String(describing: error))")
            return
        }
        #expect(status == 400)
        #expect(code == "cwd_not_found")
        #expect(message == "Directory does not exist")
    }

    @Test
    func machineDirectoryEntryUsesExplicitLaunchCapabilityFields() throws {
        let data = try #require("""
        {
          "machines": [
            {
              "device_id": "cinder",
              "machine_name": "cinder",
              "online": true,
              "control_channel_status": "connected",
              "supports": [],
              "can_launch_codex": true,
              "launch_blocked_by": null,
              "last_seen_at": "2026-05-24T00:00:00Z",
              "engine_build": "dev"
            },
            {
              "device_id": "offline",
              "machine_name": "offline",
              "online": true,
              "control_channel_status": "disconnected",
              "supports": ["codex.launch"],
              "can_launch_codex": false,
              "launch_blocked_by": "control_down",
              "last_seen_at": null,
              "engine_build": null
            }
          ]
        }
        """.data(using: .utf8))

        let decoded = try JSONDecoder.snakeCase.decode(MachineDirectoryResponse.self, from: data)

        #expect(decoded.machines[0].isLaunchable)
        #expect(!decoded.machines[1].isLaunchable)
        #expect(decoded.machines[1].launchBlockedBy == "control_down")
    }

    @Test
    func compactWorkspacePathReplacesUserHomePrefix() {
        #expect(LonghouseAPI.compactWorkspacePath("/Users/davidrose/git/zerg/longhouse") == "~/git/zerg/longhouse")
        #expect(LonghouseAPI.compactWorkspacePath("/var/app-data/longhouse") == "/var/app-data/longhouse")
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
