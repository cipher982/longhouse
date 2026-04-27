import Foundation
import Testing
@testable import Longhouse

struct SessionLiveActivityModelsTests {
    @Test
    func decodesServerContentStatePayloadKeys() throws {
        let payload = """
        {
          "presenceState": "running",
          "displayPhase": "Running bash",
          "activeTool": "bash",
          "updatedAt": 1777140000,
          "isAttention": false
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "running")
        #expect(state.displayPhase == "Running bash")
        #expect(state.activeTool == "bash")
        #expect(state.updatedAt == 1_777_140_000)
        #expect(state.isAttention == false)
    }

    @Test
    func decodesNullActiveToolFromServerPayload() throws {
        let payload = """
        {
          "presenceState": "needs_user",
          "displayPhase": "Waiting on you",
          "activeTool": null,
          "updatedAt": 1777140001,
          "isAttention": true
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "needs_user")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == true)
    }

    @Test
    func contentStateCanonicalizesLegacyShellLabels() throws {
        let json = """
        {
          "id": "session-shell",
          "provider": "claude",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Run checks",
          "summary_title": "Run Checks",
          "presence_state": "running",
          "presence_tool": "bash",
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Running bash",
          "active_tool": "bash",
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true,
            "display_label": "Live on this Mac",
            "display_detail": "Longhouse can send prompts into this live session.",
            "display_tone": "success"
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.displayPhase == "Running Shell")
        #expect(state.activeTool == "Shell")
    }
}
