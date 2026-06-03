import Foundation
import Testing
@testable import Longhouse

struct LonghouseAPITests {
    @Test
    func workspaceSuggestionsURLUsesCookieAuthTimelinePath() throws {
        // Regression guard: the launch sheet authenticates with the browser
        // cookie, so this MUST hit /api/timeline/*, NOT the device-token-gated
        // /api/agents/* sibling. Pointing it at /api/agents/* 401s on device.
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))

        let url = LonghouseAPI.workspaceSuggestionsURL(baseURL: baseURL, deviceId: "cinder", limit: 12)
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.host == "demo.longhouse.ai")
        #expect(components.path == "/api/timeline/machines/cinder/workspaces")
        #expect(!components.path.contains("/agents/"))
        #expect(components.queryItems == [URLQueryItem(name: "limit", value: "12")])
    }

    @Test
    func sessionWorkspaceURLIncludesLimitAndBranchMode() throws {
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))

        let url = LonghouseAPI.sessionWorkspaceURL(
            baseURL: baseURL,
            id: "session-1",
            limit: 200,
            branchMode: "head"
        )
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.scheme == "https")
        #expect(components.host == "demo.longhouse.ai")
        #expect(components.path == "/api/timeline/sessions/session-1/workspace")
        #expect(components.queryItems == [
            URLQueryItem(name: "limit", value: "200"),
            URLQueryItem(name: "branch_mode", value: "head"),
        ])
    }

    @Test
    func sessionMobileTailURLIncludesTailPagingFields() throws {
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))

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
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))

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
    func launchErrorParsingAcceptsCodeField() throws {
        let data = try #require("""
        {
          "detail": {
            "code": "machine_offline",
            "message": "Machine is offline"
          }
        }
        """.data(using: .utf8))

        let error = LonghouseAPI.parseLaunchError(statusCode: 409, data: data)

        guard case let .structured(status, code, message)? = error else {
            Issue.record("expected structured error, got \(String(describing: error))")
            return
        }
        #expect(status == 409)
        #expect(code == "machine_offline")
        #expect(message == "Machine is offline")
    }

    @Test
    func unknownLaunchStateDoesNotFailDecode() throws {
        let data = try #require("""
        {
          "session_id": "abc",
          "launch_state": "new_future_state",
          "launch_error_code": null,
          "launch_error_message": null
        }
        """.data(using: .utf8))

        let decoded = try JSONDecoder.snakeCase.decode(RemoteSessionLaunchResponse.self, from: data)
        #expect(decoded.launchState == .unknown)
    }

    @Test
    func knownLaunchStatesDecodeFromBackendContract() throws {
        let cases: [(String, RemoteLaunchState)] = [
            ("launching", .launching),
            ("live", .live),
            ("launching_unknown", .launchingUnknown),
            ("launch_failed", .launchFailed),
            ("launch_orphaned", .launchOrphaned),
        ]

        for (rawState, expected) in cases {
            let data = try #require("""
            {
              "session_id": "abc",
              "launch_state": "\(rawState)",
              "launch_error_code": null,
              "launch_error_message": null
            }
            """.data(using: .utf8))

            let decoded = try JSONDecoder.snakeCase.decode(RemoteSessionLaunchResponse.self, from: data)
            #expect(decoded.launchState == expected)
        }
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
        #expect(LonghouseAPI.compactWorkspacePath("/Users/example/git/zerg/longhouse") == "~/git/zerg/longhouse")
        #expect(LonghouseAPI.compactWorkspacePath("/var/app-data/longhouse") == "/var/app-data/longhouse")
    }

    @Test
    func workspaceSuggestionsResponseDecodesSnakeCase() throws {
        let json = """
        {
          "device_id": "cinder",
          "workspaces": [
            {
              "path": "/Users/example/git/zerg/longhouse",
              "label": "longhouse (main)",
              "git_repo": "git@github.com:cipher982/longhouse.git",
              "git_branch": "main",
              "score": 22590.0,
              "last_used_at": "2026-06-03T00:00:00Z",
              "session_count": 422
            },
            {
              "path": "/Users/example",
              "label": "~",
              "git_repo": null,
              "git_branch": null,
              "score": 5310.0,
              "last_used_at": null,
              "session_count": 120
            }
          ]
        }
        """
        let decoded = try JSONDecoder.snakeCase.decode(
            WorkspaceSuggestionsResponse.self,
            from: Data(json.utf8)
        )
        #expect(decoded.deviceId == "cinder")
        #expect(decoded.workspaces.count == 2)
        let first = decoded.workspaces[0]
        #expect(first.path == "/Users/example/git/zerg/longhouse")
        #expect(first.label == "longhouse (main)")
        #expect(first.gitRepo == "git@github.com:cipher982/longhouse.git")
        #expect(first.gitBranch == "main")
        #expect(first.sessionCount == 422)
        #expect(decoded.workspaces[1].gitRepo == nil)
    }

    @Test
    func machineDirectoryEntryDefaultProviderPrefersCodex() throws {
        let json = """
        {
          "device_id": "cinder",
          "machine_name": "cinder",
          "online": true,
          "control_channel_status": "connected",
          "supports": ["codex.launch", "claude.launch"],
          "can_launch_codex": true,
          "launchable_providers": ["claude", "codex", "opencode"],
          "launch_blocked_by": null,
          "last_seen_at": null,
          "engine_build": "dev"
        }
        """
        let entry = try JSONDecoder.snakeCase.decode(MachineDirectoryEntry.self, from: Data(json.utf8))
        #expect(entry.launchableProviders == ["claude", "codex", "opencode"])
        #expect(entry.defaultProvider == "codex")
        #expect(entry.isLaunchable)
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
